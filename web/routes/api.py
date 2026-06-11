"""Routes JSON + WebSocket de la interfaz web local.

Endpoints
---------

- ``POST /api/start``  — lanza el pipeline en background, devuelve 202.
- ``POST /api/cancel`` — cancela el job activo (idempotente).
- ``GET  /api/state``  — snapshot del estado para reconstrucción inicial
  de la UI (sin esperar al WS).
- ``WS   /api/ws``     — stream en vivo: replay del buffer + nuevos eventos.

Decisiones
----------

**Task de fondo + referencia fuerte**: :func:`start_pipeline` se lanza con
``asyncio.create_task`` y la task se guarda en :data:`_background_tasks` (un
set module-level). Sin esa referencia, el GC de Python puede recolectar la
task a mitad de ejecución — es una sutileza documentada de asyncio
(https://docs.python.org/3/library/asyncio-task.html#asyncio.create_task).
El ``add_done_callback`` la libera al terminar.

**Done callback**: :func:`_task_done` saca la task del set y filtra sus
excepciones para evitar el "task exception was never retrieved" de asyncio.
Casos benignos que silenciamos: la task terminó OK, o terminó con
:class:`web.runner.JobAlreadyRunning` (race teórica entre dos POST /start
en el mismo tick — la otra request ya está procesando el job).

Cualquier OTRA excepción la logueamos con :mod:`logging` para dejar rastro
en stdout del server. En la práctica casi no debería pasar:
:func:`start_pipeline` tiene su propio ``except Exception`` que captura
bugs del runner y los traduce a ``state.mark_error`` antes de terminar la
task limpiamente. Pero si algo se escapa (un bug en ese catch-all, una
``BaseException`` no derivada de ``Exception``), el log de api.py es la
red de seguridad final — y deja constancia aunque el state ya esté
marcado.

**Race /start**: chequeamos ``state.status == "running"`` antes del
``create_task`` para devolver 409 al cliente. Si dos requests llegaran en el
mismo tick del event loop antes de que ninguna alcance ``start_job``, ambas
verían status=idle y se crearían dos tasks; la segunda fallaría con
``JobAlreadyRunning`` dentro de su :func:`start_pipeline`, absorbida por el
done callback. En web local de 1 usuario esa race es esencialmente imposible.

**WebSocket cleanup**: :meth:`JobState.subscribe` es un async generator con
un ``finally`` que desregistra la cola de ``_subscribers``. Si el cliente
desconecta y NO llamamos ``await gen.aclose()``, ese ``finally`` nunca corre
y queda una cola huérfana que se llena con cada evento futuro (memory leak
hasta el shutdown). El handler hace ``aclose`` en SU propio finally,
cubriendo todos los caminos de salida: desconexión normal del cliente, error
en ``send_json``, cierre del server, o cualquier otra excepción.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from core.models import validate_models_dir
from web.runner import JobAlreadyRunning, cancel_pipeline, start_pipeline
from web.state import state


router = APIRouter()

_logger = logging.getLogger(__name__)


def _normalize_path(raw: str) -> Path:
    """Limpia un path que llega del frontend antes de validarlo.

    Casos comunes que producen "no encontrado" sin esto:
    - Finder al copiar/pegar a veces mete comillas (``"..."`` o ``'...'``).
    - El usuario pega con un espacio extra al inicio o al final.
    - Pega ``~/...`` y espera que se expanda al home (igual que en shell).

    Devuelve un :class:`Path` normalizado. NO chequea existencia — eso lo
    hace cada handler según corresponda.
    """
    s = raw.strip().strip('"').strip("'").strip()
    return Path(s).expanduser() if s else Path("")


# Set de referencias fuertes a las tasks de pipeline en vuelo. Sin esto el
# GC puede recolectar una task viva (asyncio docs). El done callback la
# saca del set al terminar, así no acumulamos refs zombies.
_background_tasks: set[asyncio.Task[None]] = set()


def _task_done(task: asyncio.Task[None]) -> None:
    """Done callback de la task de pipeline:

    1. Libera la referencia fuerte (la task puede recolectarse al fin).
    2. Filtra excepciones: silencia las benignas (éxito o
       :class:`JobAlreadyRunning` por race entre dos /start) y loguea
       cualquier otra para dejar rastro.

    :func:`start_pipeline` tiene su propio ``except Exception`` que traduce
    bugs del runner a ``state.mark_error`` antes de terminar la task
    limpiamente, así que normalmente no llega nada inesperado acá. El log
    es una red de seguridad por si algo escapa (un bug del catch-all
    mismo, una ``BaseException`` no derivada de ``Exception``, etc.).
    """
    _background_tasks.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is None or isinstance(exc, JobAlreadyRunning):
        return  # éxito o caso benigno conocido
    _logger.error("pipeline task falló de forma inesperada", exc_info=exc)


class StartRequest(BaseModel):
    """Body de ``POST /api/start``. Strings de path + flags de la web.

    Los flags ``flat`` y ``no_lossless`` del CLI **no** se exponen acá:
    desde la web siempre organizamos por género solamente (la energía
    queda en el tag, no en carpetas) y sin la subcarpeta intermedia
    ``Lossless/``. El CLI los conserva para quien los necesite.
    """
    source: str
    models_dir: str
    dest: str
    simulate: bool = False
    move: bool = False


def _validate_paths_relationship(source: Path, dest: Path) -> str | None:
    """Chequea que origen y destino sean carpetas distintas y no anidadas.

    Las tres relaciones problemáticas:
    - ``dest == source`` → organize procesaría dentro del propio source.
    - ``dest`` dentro de ``source`` → los archivos del dest cuentan como
      "ya organizados" y se excluyen, pero igual ensucia el árbol fuente.
    - ``source`` dentro de ``dest`` → ``collect_audio_files`` excluye todo
      lo que está bajo ``dest``, así que la fase organize procesa 0
      archivos sin error visible (caso silencioso, el más confuso).

    Devuelve un mensaje listo para mostrar al usuario, o ``None`` si OK.
    """
    try:
        s = source.resolve(strict=False)
    except OSError:
        s = source.absolute()
    try:
        d = dest.resolve(strict=False)
    except OSError:
        d = dest.absolute()

    if s == d:
        return "el destino no puede ser la misma carpeta que el origen."
    # ``X in Y.parents`` quiere decir: X es ancestro de Y → Y está adentro
    # de X. Cuidado con no invertir los mensajes — Path.parents tiene la
    # semántica al revés de como suele leerse en castellano.
    if s in d.parents:
        return "el destino está dentro del origen — elegí una carpeta fuera de él."
    if d in s.parents:
        return "el origen está dentro del destino — esto haría que organize procese 0 archivos."
    return None


@router.post("/start", status_code=202)
async def start(req: StartRequest) -> dict[str, Any]:
    """Lanza el pipeline en background y responde 202 con el snapshot inicial.

    Validaciones (HTTP 400):

    - ``source``: tiene que ser una carpeta existente.
    - ``models_dir``: tiene que ser una carpeta existente.
    - ``dest``: si existe, tiene que ser carpeta. Si no existe, organize
      la crea durante la corrida (paridad con la CLI).

    Si ya hay un job corriendo: HTTP 409.
    """
    # Normalización (strip de espacios/comillas, expand de ``~``). Sin esto,
    # un path pegado desde Finder con comillas o con tilde falla por
    # "no existe" aunque el usuario haya escrito la ruta correcta.
    source = _normalize_path(req.source)
    models_dir = _normalize_path(req.models_dir)
    dest = _normalize_path(req.dest)

    if not source.is_dir():
        raise HTTPException(
            status_code=400,
            detail=f"source no es una carpeta existente: {source}",
        )
    if not models_dir.is_dir():
        raise HTTPException(
            status_code=400,
            detail=f"models_dir no es una carpeta existente: {models_dir}",
        )
    if dest.exists() and not dest.is_dir():
        raise HTTPException(
            status_code=400,
            detail=f"dest existe pero no es carpeta: {dest}",
        )

    # Relación origen ↔ destino. Si dest está adentro de source (o source
    # adentro de dest, o son la misma), el organize procesa 0 archivos sin
    # error visible — el caso silencioso más confuso.
    rel_err = _validate_paths_relationship(source, dest)
    if rel_err:
        raise HTTPException(status_code=400, detail=rel_err)

    # Chequeo preemptivo: devolver 409 al cliente directo. La race teórica
    # con otro /start en el mismo tick queda absorbida por _task_done
    # (ver docstring del módulo).
    if state.status == "running":
        raise HTTPException(
            status_code=409,
            detail="ya hay un job corriendo",
        )

    # La web no expone --flat ni --no-lossless: organizamos siempre por
    # género (la energía queda en el tag) y sin la subcarpeta intermedia
    # ``Lossless/``. Decisión cerrada, no hace falta opt-in por request.
    task = asyncio.create_task(
        start_pipeline(
            source=source,
            models_dir=models_dir,
            dest=dest,
            simulate=req.simulate,
            flat=True,
            no_lossless=True,
            move=req.move,
        ),
    )
    _background_tasks.add(task)
    task.add_done_callback(_task_done)

    return {"status": "started", "state": state.snapshot()}


@router.get("/defaults")
async def get_defaults() -> dict[str, Any]:
    """Sugerencias para pre-llenar el form de setup.

    Detecta paths que probablemente sirvan en este sistema:
    - ``models_dir``: primero ``~/essentia_models``, si no ``./models``.
      Solo lo sugerimos si la carpeta tiene al menos un ``.pb`` adentro
      (carpeta vacía no es útil).
    - ``dest``: una sugerencia local en ``~/Music/Organizada`` (la
      carpeta no tiene que existir; organize la crea).
    - ``source``: vacío — el usuario tiene que decir cuál es su
      biblioteca, no podemos adivinar.

    El frontend prellena SOLO los campos que estén vacíos, así si volvés
    al setup tras un error no te pisa lo que ya tipeaste.
    """
    home = Path.home()

    models_dir = ""
    for candidate in (home / "essentia_models", Path("models").resolve()):
        if candidate.is_dir() and any(candidate.glob("*.pb")):
            models_dir = str(candidate)
            break

    dest = str(home / "Music" / "Organizada")

    return {
        "source": "",
        "models_dir": models_dir,
        "dest": dest,
    }


class CheckPathRequest(BaseModel):
    """Body de ``POST /api/check-path``. ``kind`` cambia la semántica:

    - ``"dir"`` (default): la carpeta tiene que existir.
    - ``"models"``: además, tiene que contener los 5 archivos de Essentia.
    - ``"dest"``: puede no existir todavía (organize la crea), pero no
      puede ser un archivo regular.
    """
    path: str
    kind: str = "dir"


@router.post("/check-path")
async def check_path(req: CheckPathRequest) -> dict[str, Any]:
    """Valida un path en vivo. El frontend lo llama mientras el usuario
    tipea (debounced) para mostrar ✓/✗ al lado del input.

    Siempre 200 — el resultado va en el body como ``{ok, reason, ...}``,
    así el frontend no tiene que distinguir errores HTTP de paths inválidos
    (que son flujo normal mientras el usuario escribe).
    """
    raw = req.path or ""
    if not raw.strip():
        return {"ok": False, "reason": "vacío"}

    path = _normalize_path(raw)
    resolved = str(path)

    if req.kind == "dest":
        # Puede no existir todavía. Solo cazamos el caso "es un archivo".
        if path.exists() and not path.is_dir():
            return {"ok": False, "path": resolved, "reason": "es un archivo, no una carpeta"}
        return {"ok": True, "path": resolved, "exists": path.exists()}

    if req.kind == "models":
        if not path.is_dir():
            return {
                "ok": False,
                "path": resolved,
                "reason": "no existe" if not path.exists() else "no es carpeta",
                "missing": [],
            }
        missing = validate_models_dir(path)
        return {
            "ok": len(missing) == 0,
            "path": resolved,
            "missing": missing,
            "reason": None if not missing else f"faltan {len(missing)} archivo(s) de modelos",
        }

    # default: "dir"
    if path.is_dir():
        return {"ok": True, "path": resolved}
    return {
        "ok": False,
        "path": resolved,
        "reason": "no existe" if not path.exists() else "no es carpeta",
    }


@router.get("/browse")
async def browse(path: str = "") -> dict[str, Any]:
    """Listado de subcarpetas para el explorador del setup.

    Devuelve:
    - ``path``: ruta absoluta donde estamos parados.
    - ``parent``: ruta del padre (o ``None`` si estamos en la raíz).
    - ``breadcrumb``: ``[{name, path}, ...]`` para mostrar el camino.
    - ``entries``: subcarpetas (no archivos, no ocultas) ordenadas por
      nombre case-insensitive.

    Si el path pedido no existe o no es carpeta, hace fallback al home
    del usuario — así el modal no queda en estado roto si el input tenía
    basura.
    """
    p = _normalize_path(path) if path else Path.home()

    if not p.is_dir():
        p = Path.home()

    try:
        p = p.resolve(strict=False)
    except OSError:
        p = p.absolute()

    # Subcarpetas visibles, ordenadas.
    entries: list[dict[str, str]] = []
    try:
        for child in sorted(p.iterdir(), key=lambda x: x.name.lower()):
            if child.name.startswith("."):
                continue
            try:
                if not child.is_dir():
                    continue
            except OSError:
                continue  # symlink roto / permiso denegado en stat
            entries.append({"name": child.name, "path": str(child)})
    except (PermissionError, OSError):
        entries = []

    # Breadcrumb: cada componente del path como hop navegable.
    breadcrumb: list[dict[str, str]] = []
    for i in range(len(p.parts)):
        accum = Path(*p.parts[: i + 1])
        name = p.parts[i] if i > 0 else "/"
        breadcrumb.append({"name": name, "path": str(accum)})

    # Padre: ``None`` solo en la raíz (donde ``parent == self``).
    parent = str(p.parent) if p.parent != p else None

    return {
        "path": str(p),
        "parent": parent,
        "breadcrumb": breadcrumb,
        "entries": entries,
    }


@router.post("/cancel")
async def cancel() -> dict[str, Any]:
    """Cancela el job activo. Idempotente: no-op si no hay job o ya terminó.

    Devuelve el snapshot tras la cancelación para que el cliente actualice
    la UI sin tener que volver a pedir ``/state``.
    """
    await cancel_pipeline()
    return state.snapshot()


@router.get("/state")
async def get_state() -> dict[str, Any]:
    """Snapshot completo del estado. La UI lo usa al cargar /progress o
    /results para reconstruirse antes (o en lugar) de suscribirse al WS.
    """
    return state.snapshot()


@router.websocket("/ws")
async def websocket_events(ws: WebSocket) -> None:
    """Stream de eventos del job: replay del buffer + nuevos en vivo.

    El ``await gen.aclose()`` del ``finally`` es lo que dispara el cleanup
    del suscriptor en :meth:`JobState.subscribe` — sin él queda una cola
    huérfana acumulando eventos hasta el shutdown. Cubrimos todos los
    caminos de salida: desconexión normal del cliente, error en
    ``send_json``, cierre del server, o cualquier otra excepción.

    :class:`WebSocketDisconnect` lo capturamos explícitamente porque es
    flujo normal (el usuario cerró la pestaña / navegó); no queremos que
    FastAPI lo loguee como error.
    """
    await ws.accept()
    gen = state.subscribe()
    try:
        async for event in gen:
            await ws.send_json(event)
    except WebSocketDisconnect:
        # Cliente cerró el WS — flujo esperado, no es error.
        pass
    finally:
        # Dispara el ``finally`` del async generator (state.subscribe), que
        # remueve nuestra cola de ``_subscribers``. Sin esto: leak.
        await gen.aclose()
