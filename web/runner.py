"""Orquestador del subprocess del pipeline. Lanza :mod:`web.worker`, parsea
sus eventos JSONL y los empuja al :mod:`web.state`.

Responsabilidades
-----------------

- Lanzar el worker como subprocess con :func:`asyncio.create_subprocess_exec`,
  usando ``sys.executable -m web.worker`` (mismo intérprete y mismo venv que
  el servidor — ``"python"`` desnudo puede no existir o apuntar a otra cosa).
- Leer ``stdout`` línea por línea de forma async, parsear cada línea como
  JSON, y llamar :meth:`web.state.JobState.record_event` por cada evento.
- Tolerar basura en stdout: líneas vacías, no-JSON (warnings de Essentia/TF
  que escapen del filtro), strings JSON pero no-dict. Se ignoran sin romper
  el loop — solo los dicts JSON cuentan como eventos.
- Detectar muerte abrupta del worker (SIGKILL, OOM, crash antes del primer
  ``emit``): si el stream cierra y el state sigue en ``running``, marca
  ``error`` con el exit code + tail de stderr para diagnóstico.

Decisiones de diseño
--------------------

**Subprocess huérfano vs desconexión**: si el cliente cierra la pestaña, el
WebSocket se cae pero el subprocess SIGUE corriendo. El análisis de una
biblioteca grande puede durar ~90 min — no queremos matarlo porque el
usuario se distrajo. El estado vive en memoria del servidor; al recargar la
pestaña, la UI se reconstruye desde :meth:`JobState.snapshot` y reaparece el
progreso. La cancelación es **explícita** (botón Cancelar →
:func:`cancel_pipeline`), nunca implícita por desconexión.

**Un solo job a la vez**: consistente con el state singleton. Si
:func:`start_pipeline` se llama mientras hay otro job corriendo, lanza
:class:`JobAlreadyRunning`. Las routes lo traducen a un 409 para el cliente.

**Race state vs subprocess al cancelar**: :func:`cancel_pipeline` marca el
state como ``cancelled`` **antes** de mandar ``SIGTERM``. Así cuando el loop
de lectura en :func:`start_pipeline` vea el EOF y consulte ``state.status``,
ya no estará en ``running`` y no sobrescribirá con ``error("proceso terminó
inesperadamente")``. Esta ordenación es lo que garantiza que un cancel
explícito produzca status=``cancelled``, no status=``error``.

**Stderr a archivo, no a pipe**: el subprocess se lanza con ``stderr`` apuntando
a un :class:`tempfile.NamedTemporaryFile`, no a ``asyncio.subprocess.PIPE``.
Razón: el buffer de un pipe es chico (~64 KB en macOS/Linux). Essentia y
TensorFlow escupen miles de warnings a stderr durante el análisis; si el
runner los lee solo *después* de ``process.wait()`` (no en paralelo), el
worker bloquea escribiendo a un pipe lleno → ``wait()`` nunca retorna →
deadlock. Un archivo no tiene ese límite, así que el worker nunca se bloquea.
El stderr solo se usa para diagnóstico post-mortem (tail cuando el proceso
muere inesperadamente), así que perder el streaming en tiempo real no cuesta
nada. El archivo se borra en el ``finally`` aunque haya excepciones.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

from web.state import state


class JobAlreadyRunning(RuntimeError):
    """Levantada por :func:`start_pipeline` cuando ``state.status`` ya es
    ``running``. Las routes la traducen a un HTTP 409.
    """


# Referencia al subprocess en curso. ``None`` cuando no hay job activo.
# :func:`cancel_pipeline` la usa para mandarle ``SIGTERM``; :func:`start_pipeline`
# la setea al lanzar y la limpia en el ``finally``. Como todo corre en un
# único event loop, basta una variable de módulo — no hace falta lock.
_current_process: asyncio.subprocess.Process | None = None


async def start_pipeline(
    source: Path,
    models_dir: Path,
    dest: Path,
    *,
    simulate: bool = False,
    flat: bool = False,
    no_lossless: bool = False,
    move: bool = False,
) -> None:
    """Lanza el pipeline en un subprocess y alimenta el state con sus eventos.

    Bloquea hasta que el worker termina (exit limpio, crash o kill).
    Pensada para correr como task de asyncio en background — la route HTTP
    que lanza el job hace ``asyncio.create_task(start_pipeline(...))`` y le
    contesta al cliente inmediatamente.

    Rechaza con :class:`JobAlreadyRunning` si ya hay un job corriendo. La
    detección es ``state.status == "running"`` (singleton, sin race en el
    event loop).
    """
    global _current_process

    if state.status == "running":
        raise JobAlreadyRunning("ya hay un job corriendo")

    # Snapshot de opciones para la UI antes de lanzar. ``start_job`` resetea
    # el state — cualquier suscriptor ve la transición running de inmediato,
    # antes de que llegue el primer evento del worker (~10–15 s después,
    # cuando arranca a cargar Essentia).
    options = {
        "source": str(source),
        "models_dir": str(models_dir),
        "dest": str(dest),
        "simulate": simulate,
        "flat": flat,
        "no_lossless": no_lossless,
        "move": move,
    }
    state.start_job(options)

    # ``sys.executable`` garantiza el mismo intérprete que está corriendo
    # el servidor (mismo venv, mismas dependencias). ``"python"`` desnudo
    # puede no existir en macOS sin pyenv, o apuntar al Python del sistema
    # que no tiene Essentia instalada.
    args = [
        sys.executable, "-m", "web.worker",
        str(source),
        "--models", str(models_dir),
        "--dest", str(dest),
    ]
    if simulate:
        args.append("--simulate")
    if flat:
        args.append("--flat")
    if no_lossless:
        args.append("--no-lossless")
    if move:
        args.append("--move")

    # Archivo temporal para el stderr del worker. ``delete=False`` porque
    # macOS/Linux no nos dejan re-leer el archivo en algunos flujos si se
    # auto-borra al cerrar; lo borramos a mano en el ``finally``. Modo
    # binario: los warnings de TF/Essentia pueden traer bytes raros y
    # ``errors="replace"`` al decodificar al final maneja eso sin sorpresas.
    stderr_file = tempfile.NamedTemporaryFile(
        mode="w+b",
        suffix=".stderr.log",
        prefix="music-organizer-worker-",
        delete=False,
    )
    stderr_path = stderr_file.name
    process: asyncio.subprocess.Process | None = None

    try:
        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=stderr_file,
            )
        except OSError as e:
            # No se pudo lanzar el subprocess (intérprete no encontrado, fork
            # falló, etc.). El job nunca arrancó; reportamos como error y
            # dejamos que el ``finally`` cierre + borre el tempfile.
            state.mark_error(f"no se pudo lanzar el worker: {e}")
            return

        _current_process = process

        try:
            # ``process.stdout`` es ``None`` solo si no pediste ``PIPE`` —
            # acá lo pedimos siempre, así que asertamos para mypy.
            assert process.stdout is not None

            async for raw in process.stdout:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    # Basura en stdout: log no-JSON, etc. No rompemos el loop —
                    # solo los eventos JSON válidos cuentan. (En la práctica
                    # esto es raro: los warnings van a stderr, que ya está
                    # redirigido al tempfile aparte.)
                    continue
                if isinstance(event, dict):
                    state.record_event(event)

            # Stream cerrado. Cosechamos el child. Si el worker emitió ``done``
            # o ``error`` ya, el state transicionó y ``mark_error`` abajo es
            # no-op (idempotente cuando status != "running").
            exit_code = await process.wait()

            # Si el state sigue en ``running``, el subprocess murió sin emitir
            # un evento final: SIGKILL externo, OOM, crash del intérprete antes
            # de los imports, segfault de TF, etc. Lo reportamos con el tail
            # de stderr para que la UI pueda mostrar algo accionable.
            #
            # Si el state ya está ``cancelled`` (por :func:`cancel_pipeline`,
            # que marca ANTES de mandar SIGTERM), saltamos esta rama — no
            # queremos pisar ``cancelled`` con ``error``.
            if state.status == "running":
                tail = ""
                try:
                    # El subprocess escribió vía un fd duplicado; nuestro handle
                    # nunca avanzó su posición. ``seek(0)`` por las dudas.
                    stderr_file.seek(0)
                    tail = stderr_file.read().decode(
                        "utf-8", errors="replace"
                    ).strip()
                except OSError:
                    tail = ""
                tail_msg = f" — stderr: {tail[-500:]}" if tail else ""
                state.mark_error(
                    f"el proceso terminó inesperadamente "
                    f"(exit code {exit_code}){tail_msg}"
                )
        except Exception as e:  # noqa: BLE001 — boundary: bug inesperado en el pipeline
            # Cualquier excepción no prevista del loop de lectura / wait /
            # diagnóstico (bug en ``record_event``, fallo interno de asyncio,
            # decoder roto, etc.) cae acá. Sin este except, la excepción
            # escaparía hasta la task de fondo, donde ``_task_done`` la
            # consumiría en silencio — el state quedaría en ``running`` para
            # siempre, la UI con la barra congelada y el usuario sin poder
            # lanzar otro job (409 perpetuo).
            #
            # ``mark_error`` es idempotente (no-op si status != "running"),
            # así que si el worker ya emitió ``done`` antes de que esto
            # explote, no pisamos un done legítimo.
            #
            # NO re-lanzamos: el state ya quedó marcado; re-lanzar solo
            # dispararía el "task exception was never retrieved" que el
            # except del done callback en :mod:`web.routes.api` justamente
            # quiere evitar. Que muera acá con el state correctamente
            # registrado. ``BaseException`` (CancelledError,
            # KeyboardInterrupt) NO la atrapamos a propósito — esas tienen
            # que propagarse para que asyncio y el usuario puedan parar.
            state.mark_error(
                f"error inesperado en el pipeline: {type(e).__name__}: {e}"
            )
    finally:
        # Solo limpiamos la referencia si seguimos siendo el process activo.
        # En el caso raro de que alguien haya arrancado otro job mientras
        # tanto (no debería pasar, ``JobAlreadyRunning`` lo bloquea, pero
        # defensivo), no pisamos su referencia.
        if process is not None and _current_process is process:
            _current_process = None

        # Cleanup del tempfile siempre, haya o no excepción. ``close()``
        # libera nuestro fd; ``unlink`` borra el archivo del disco. Ambos
        # tolerantes a fallos: si el archivo ya no existe (Windows / cleanup
        # externo / crash a mitad), no rompemos.
        try:
            stderr_file.close()
        except OSError:
            pass
        try:
            os.unlink(stderr_path)
        except OSError:
            pass


async def cancel_pipeline() -> None:
    """Cancela el job actual: marca el state, manda ``SIGTERM`` al subprocess,
    espera hasta 3 s y manda ``SIGKILL`` si no responde.

    No-op si no hay proceso corriendo o si el job ya terminó. Idempotente:
    llamar cancel dos veces seguidas es seguro.

    **Orden importante**: :meth:`JobState.mark_cancelled` se llama ANTES de
    ``process.terminate()``. Cuando el loop de lectura en
    :func:`start_pipeline` vea el EOF subsiguiente y consulte
    ``state.status``, ya estará en ``cancelled`` y no sobrescribirá con
    ``error("proceso terminó inesperadamente")``. Esta ordenación es lo
    que diferencia un cancel explícito de una muerte abrupta.

    ``SIGTERM`` primero (no ``SIGKILL``) para darle al worker la chance de
    cerrar limpio. Para el pipeline actual no hay cleanup pendiente —
    Essentia/TF mueren con el process — así que el terminate suele ser
    instantáneo, pero el patrón es prudente.
    """
    process = _current_process

    # Sin proceso o ya terminó: solo marcamos el state si quedó colgado en
    # ``running`` por alguna razón. ``mark_cancelled`` es idempotente.
    if process is None or process.returncode is not None:
        state.mark_cancelled()
        return

    # Marcamos cancelled ANTES de mandar SIGTERM (ver docstring).
    state.mark_cancelled()

    try:
        process.terminate()
    except ProcessLookupError:
        # Race: el subprocess murió entre el chequeo y el terminate. El
        # state ya está marcado; nada más que hacer.
        return

    try:
        await asyncio.wait_for(process.wait(), timeout=3.0)
    except asyncio.TimeoutError:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        # No esperamos otro ``wait()`` acá — :func:`start_pipeline` ya
        # está awaiteando ``process.wait()`` en su propio flow, y va a
        # cosechar el child cuando SIGKILL surta efecto.
