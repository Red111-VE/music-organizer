"""Estado en memoria del job activo de la interfaz web local.

Web local, 1 usuario, 1 job a la vez — no hace falta un registry de múltiples
jobs. Un singleton de módulo (:data:`state`) basta. Esta arquitectura no
soporta procesar dos carpetas en paralelo; si el usuario inicia un job nuevo,
el anterior se descarta.

**Concurrencia**

Todo vive en el event loop de asyncio. Tanto el runner (que lee stdout del
worker async vía :mod:`asyncio.subprocess`) como los WebSockets corren como
tareas en el mismo loop. Las mutaciones de estado
(:meth:`JobState.record_event`, :meth:`JobState.start_job`,
:meth:`JobState.mark_cancelled`, :meth:`JobState.mark_error`) son
**síncronas y sin ``await``**, así que son atómicas respecto a otras
corrutinas — no hace falta ``asyncio.Lock``. La única operación async es
:meth:`JobState.subscribe`, que cede control en ``await queue.get()`` pero
hace su snapshot inicial del buffer en un bloque síncrono (sin solapamiento
posible con ``record_event``).

No usa threads. Si en el futuro se necesita un thread (ej. decoding paralelo
de audio), habría que añadir un lock o pasar las actualizaciones por
``loop.call_soon_threadsafe``.

**Reconexión**

El estado mantiene un buffer circular de los últimos N eventos.
:meth:`JobState.subscribe` snapshotea ese buffer al conectarse y luego
streamea los eventos nuevos a medida que llegan — atómico vía un bloque
síncrono entre snapshot y registración de la cola. Así un cliente que
reconecte ve cada evento exactamente una vez, en orden, sin huecos.

Para clientes que prefieren no streamear, :meth:`JobState.snapshot` expone
el estado derivado completo (status, fase, progreso, tracks, etc.) — la UI
puede reconstruirse desde ahí sin depender del log de eventos.

**Singleton**

:data:`state` es la instancia importada por el resto de :mod:`web`. La
clase :class:`JobState` se puede instanciar libremente en tests sin tocar
este singleton.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, AsyncIterator, Literal


JobStatus = Literal["idle", "running", "done", "error", "cancelled"]
Phase = Literal["tag", "enrich", "organize"]

_PHASES: tuple[Phase, ...] = ("tag", "enrich", "organize")

# Buffer circular para replay en reconexión. 2000 cubre un job de ~650
# pistas (3 fases × 650 progress + meta ≈ 1955 eventos). Bibliotecas más
# grandes van a perder eventos viejos del buffer; la UI debe poder
# reconstruirse desde el snapshot derivado (:meth:`JobState.snapshot`) sin
# depender del log completo.
_DEFAULT_BUFFER = 2000


@dataclass
class PhaseProgress:
    """Progreso de una fase del pipeline. ``total`` se conoce al recibir el
    evento ``phase`` (al inicio de cada fase); ``done`` se incrementa con
    cada ``progress``.
    """
    done: int = 0
    total: int = 0


@dataclass
class TrackPoint:
    """Datos de una pista exitosa de la fase tag, acumulados para el
    histograma en vivo. La UI los va dibujando a medida que llegan; la
    pantalla de resultados los reusa para los sliders de umbrales.
    """
    file: str
    genre: str
    energy_level: str
    arousal: float
    confidence: float


class JobState:
    """Contenedor del único job activo, con pub/sub para WebSockets.

    Mutaciones son síncronas (sin ``await``) — atómicas respecto a las
    corrutinas que leen. Suscripción vía :meth:`subscribe` (async generator).
    """

    def __init__(self, buffer_size: int = _DEFAULT_BUFFER) -> None:
        self._buffer_size = buffer_size
        # Las colas son ilimitadas: un cliente lento puede acumular eventos
        # en memoria pero nunca bloquea al runner. Para web local con 1
        # cliente esto es seguro; para multi-cliente habría que añadir un
        # límite + estrategia de drop o backpressure.
        self._subscribers: list[asyncio.Queue[dict[str, Any] | None]] = []
        self._reset()

    def _reset(self) -> None:
        """Inicializa el estado al valor ``idle``. Lo llaman ``__init__`` y
        :meth:`start_job`. NO toca ``_subscribers`` — un cliente conectado
        a un job que termina y arranca uno nuevo sigue recibiendo eventos
        del nuevo job.
        """
        self.status: JobStatus = "idle"
        self.phase: Phase | None = None
        self.phases: dict[Phase, PhaseProgress] = {
            p: PhaseProgress() for p in _PHASES
        }
        self.tracks: list[TrackPoint] = []
        self.events: deque[dict[str, Any]] = deque(maxlen=self._buffer_size)
        self.result: dict[str, Any] | None = None
        self.error: str | None = None
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self.options: dict[str, Any] = {}

    # --- API pública -------------------------------------------------------

    def start_job(self, options: dict[str, Any]) -> None:
        """Resetea el estado y marca el job como ``running``.

        ``options`` es el dict con los parámetros con los que se lanzó
        (``source``, ``dest``, ``models``, ``simulate``, ``flat``, etc.) — la
        UI los muestra en la pantalla de progreso y la de resultados. La
        estructura es opaca para state; el runner decide qué meter.

        No emite un evento sintético: el worker manda su propio ``start``
        (que pasa por :meth:`record_event`) y eso notifica a los
        suscriptores. Si por alguna razón el worker no arrancara, los
        suscriptores verían el estado ``running`` solo cuando consulten
        :meth:`snapshot`.
        """
        self._reset()
        self.status = "running"
        self.started_at = time.time()
        self.options = dict(options)

    def record_event(self, event: dict[str, Any]) -> None:
        """Aplica un evento (del worker o sintético) al estado, lo bufferea
        y lo publica a los suscriptores.

        Síncrono y sin ``await``: garantiza que el estado nunca queda en un
        estado intermedio cuando otra corrutina lee :meth:`snapshot` o
        invoca :meth:`subscribe`.

        Eventos manejados con cambio de estado: ``start``, ``phase``,
        ``progress``, ``done``, ``error``, ``cancelled``. Eventos
        informativos sin cambio (``models_loading``, ``models_loaded``) y
        eventos desconocidos van solo a buffer + broadcast — así el
        protocolo del worker puede extenderse sin romper a los consumers.
        """
        kind = event.get("event")

        if kind == "start":
            # Idempotente: ``start_job`` ya seteó status=running antes de
            # lanzar el worker. Acá lo confirmamos por si se reusa
            # ``record_event`` en otro flujo (ej. tests).
            self.status = "running"
        elif kind == "phase":
            phase = event.get("phase")
            if phase in self.phases:
                self.phase = phase  # type: ignore[assignment]
                self.phases[phase].total = int(event.get("total", 0))
                self.phases[phase].done = 0
        elif kind == "progress":
            phase = event.get("phase")
            if phase in self.phases:
                self.phases[phase].done = int(
                    event.get("done", self.phases[phase].done)
                )
                # Acumular para el histograma: SOLO en fase tag y SOLO en
                # éxito. Errores no traen arousal/genre; los progress de
                # enrich y organize no agregan datos al histograma (que se
                # construye sobre la salida del análisis).
                if (
                    phase == "tag"
                    and event.get("status") == "ok"
                    and "arousal" in event
                ):
                    self.tracks.append(TrackPoint(
                        file=event.get("file", ""),
                        genre=event.get("genre", ""),
                        energy_level=event.get("energy_level", ""),
                        arousal=float(event["arousal"]),
                        confidence=float(event.get("confidence", 0.0)),
                    ))
        elif kind == "done":
            self.status = "done"
            self.finished_at = time.time()
            self.result = {
                "tag_ok": event.get("tag_ok"),
                "tag_err": event.get("tag_err"),
                "report": event.get("report"),
            }
        elif kind == "error":
            self.status = "error"
            self.finished_at = time.time()
            self.error = str(event.get("message", "error desconocido"))
        elif kind == "cancelled":
            self.status = "cancelled"
            self.finished_at = time.time()
            self.error = str(event.get("message", "cancelado"))
        # models_loading / models_loaded / desconocidos: sin cambio de estado.

        self.events.append(event)
        self._broadcast(event)

    def mark_cancelled(self, reason: str = "cancelado por el usuario") -> None:
        """Marca el job como cancelado (el runner lo invoca cuando el
        usuario para la corrida o cuando el subprocess es killeado).

        El worker no emite ``cancelled`` por sí mismo — el cancellation
        event siempre se origina acá. Idempotente: si el job ya terminó,
        no hace nada.
        """
        if self.status != "running":
            return
        self.record_event({"event": "cancelled", "message": reason})

    def mark_error(self, message: str) -> None:
        """Marca el job como error desde fuera del worker (ej. el subprocess
        murió sin haber emitido un evento ``error``: SIGKILL, OOM, panic).

        Si el worker ya emitió su propio error, su ``record_event`` ya
        transicionó a ``error`` y este método no hace nada (idempotente).
        """
        if self.status != "running":
            return
        self.record_event({"event": "error", "message": message})

    def snapshot(self) -> dict[str, Any]:
        """Vista read-only del estado, lista para serializar a JSON.

        Incluye los últimos N eventos del buffer para que un cliente recién
        conectado pueda reconstruir la UI sin esperar nuevos eventos. Los
        ``tracks`` van enteros (no se truncan al buffer) — son los datos
        del histograma y la pantalla de resultados los necesita todos.

        Copia las colecciones internas: el caller no puede mutar el estado
        a través del snapshot.
        """
        return {
            "status": self.status,
            "phase": self.phase,
            "phases": {
                name: {"done": p.done, "total": p.total}
                for name, p in self.phases.items()
            },
            "tracks": [
                {
                    "file": t.file,
                    "genre": t.genre,
                    "energy_level": t.energy_level,
                    "arousal": t.arousal,
                    "confidence": t.confidence,
                }
                for t in self.tracks
            ],
            "events": list(self.events),
            "result": self.result,
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "options": dict(self.options),
        }

    async def subscribe(self) -> AsyncIterator[dict[str, Any]]:
        """Streamea eventos a un cliente. Replaya el buffer al conectar y
        luego cede eventos nuevos a medida que llegan.

        Uso típico (FastAPI WebSocket)::

            async for event in state.subscribe():
                await ws.send_json(event)

        **Atomicidad del catch-up**: el snapshot del buffer y la registración
        de la cola ocurren en un bloque síncrono (sin ``await``), así que
        ningún evento nuevo puede llegar entre ambos. Garantiza orden y
        unicidad: cada evento se ve exactamente una vez. Los que estaban en
        el buffer al subscribirse llegan vía ``buffered``; los publicados
        después llegan vía la cola.

        Termina cuando :meth:`close_subscribers` envía el centinela
        ``None``. El consumidor también puede terminar saliendo del
        ``async for`` (cierre del WebSocket): el ``finally`` desregistra la
        cola.
        """
        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        # Snapshot + registración: atómico (sin ``await`` entre ambos).
        # Eventos publicados después del ``append`` van a la cola; los
        # previos ya están en ``buffered``. Sin solapamiento posible.
        buffered = list(self.events)
        self._subscribers.append(queue)
        try:
            for event in buffered:
                yield event
            while True:
                event = await queue.get()
                if event is None:
                    break
                yield event
        finally:
            try:
                self._subscribers.remove(queue)
            except ValueError:
                pass  # cleanup race: ya fue removida

    def close_subscribers(self) -> None:
        """Envía el centinela ``None`` a todas las colas para que los
        suscriptores terminen el ``async for``. Útil al shutdown del server
        o si se quiere forzar el cierre de todas las conexiones.
        """
        for queue in list(self._subscribers):
            queue.put_nowait(None)

    # --- Interno -----------------------------------------------------------

    def _broadcast(self, event: dict[str, Any]) -> None:
        """Publica un evento a cada suscriptor. ``put_nowait`` nunca falla
        en colas sin límite, así que el broadcast es siempre síncrono y
        nunca bloquea al productor (runner).
        """
        for queue in self._subscribers:
            queue.put_nowait(event)


# Singleton del módulo. Web local de 1 usuario: un solo job a la vez.
# El runner, los routes y el WebSocket lo importan de aquí. Tests pueden
# instanciar ``JobState()`` directamente sin tocar este singleton.
state = JobState()
