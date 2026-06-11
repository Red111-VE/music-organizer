"""Montaje de la app FastAPI de la interfaz web local.

Responsabilidades
-----------------

- Factory :func:`create_app` que arma una instancia limpia (útil para tests
  y para el comando ``serve``, que la lanza vía uvicorn).
- Montar ``/static`` apuntando a :file:`web/static/` con :class:`StaticFiles`.
- Configurar :class:`Jinja2Templates` apuntando a :file:`web/templates/`,
  expuesto a las routes vía ``app.state.templates`` (patrón idiomático de
  FastAPI — evita el import circular ``main`` ↔ ``routes``).
- Incluir los dos routers: :mod:`web.routes.pages` (HTML, sin prefijo) y
  :mod:`web.routes.api` (JSON + WebSocket, bajo ``/api``). Hoy son stubs;
  se llenan en los componentes 5 y 6 del plan.
- Lifespan handler: al shutdown, llamar :meth:`JobState.close_subscribers`
  para que los WebSockets terminen limpio (el centinela ``None`` rompe el
  ``async for`` en :meth:`JobState.subscribe`). Sin esto, uvicorn esperaría
  el timeout de gracia antes de matar las tareas suscritas.

Decisiones
----------

**Paths absolutos**: :data:`_WEB_DIR` se deriva de ``Path(__file__).parent``,
así uvicorn (o un test) puede lanzar la app desde cualquier CWD sin que
:class:`StaticFiles` o :class:`Jinja2Templates` se rompan buscando paths
relativos.

**Factory + módulo-level ``app``**: la factory permite a los tests crear
instancias aisladas (``create_app()``). El ``app = create_app()`` al final
del módulo permite el uvicorn idiomático ``uvicorn web.main:app``. Las dos
formas conviven sin conflicto: la factory es pura, y la instancia de módulo
es solo una llamada más a la factory.

**Lifespan vs ``on_event``**: usamos el context manager moderno
(``lifespan=...``). ``@app.on_event("shutdown")`` está deprecado en FastAPI
desde la 0.93.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from web.routes import api, pages
from web.state import state


# Directorios resueltos al cargar el módulo. Absolutos para tolerar
# cualquier CWD desde el que se lance la app.
_WEB_DIR = Path(__file__).resolve().parent
_STATIC_DIR = _WEB_DIR / "static"
_TEMPLATES_DIR = _WEB_DIR / "templates"


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Ciclo de vida de la app. Sin trabajo de startup; al shutdown cierra
    las suscripciones del state.

    Sin :meth:`JobState.close_subscribers`, las corrutinas suscritas vía
    :meth:`JobState.subscribe` quedarían bloqueadas en ``await queue.get()``
    al cierre del server, y uvicorn esperaría el timeout de gracia antes de
    matarlas. El centinela ``None`` hace que cada ``async for`` rompa su
    loop y el handler del WebSocket pueda cerrar limpio.
    """
    # ``app`` se recibe por contrato del lifespan; no lo usamos hoy.
    del app
    yield
    state.close_subscribers()


def create_app() -> FastAPI:
    """Construye una instancia fresca de la app FastAPI con sus routers,
    static, templates y lifespan.

    Pensada para llamarse una vez en producción (vía el ``app = create_app()``
    de módulo, que uvicorn levanta) y N veces en tests (cada uno con su
    instancia aislada).
    """
    app = FastAPI(
        title="music-organizer",
        description=(
            "Interfaz web local para taggear y organizar bibliotecas "
            "musicales con análisis de género y energía."
        ),
        version="0.1.0",
        lifespan=_lifespan,
    )

    # StaticFiles: CSS, JS, imágenes. Path absoluto.
    app.mount(
        "/static",
        StaticFiles(directory=_STATIC_DIR),
        name="static",
    )

    # Templates compartidos. Las routes acceden vía
    # ``request.app.state.templates`` — patrón FastAPI idiomático que evita
    # el import circular ``main`` ↔ ``routes`` (las routes no necesitan
    # importar de main para renderizar HTML).
    app.state.templates = Jinja2Templates(directory=_TEMPLATES_DIR)

    # Routers. ``pages`` sirve HTML en la raíz (``/``, ``/progress``,
    # ``/results``); ``api`` agrupa JSON + WebSocket bajo ``/api``. Hoy
    # ambos son stubs; los rellenamos en los próximos componentes.
    app.include_router(pages.router)
    app.include_router(api.router, prefix="/api")

    return app


# Instancia de módulo para ``uvicorn web.main:app``. Los tests prefieren
# llamar a :func:`create_app` directamente para tener instancias aisladas.
app = create_app()
