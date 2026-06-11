"""Routes HTML del frontend: las 3 pantallas del flujo del pipeline.

Cada handler renderiza un template Jinja2 vía
``request.app.state.templates`` (configurado en :mod:`web.main`). El
contexto pasa solo ``step`` (1, 2 o 3) para que :file:`base.html` resalte
el indicador de paso en el header.

Los datos en vivo del job NO viajan por estas routes — las pantallas se
suscriben al WebSocket de :mod:`web.routes.api` y reconstruyen su estado
desde ``GET /api/state``. Acá solo se entrega el shell HTML inicial.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse


router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    """Pantalla 1: configurar el job (carpetas + flags) y arrancarlo."""
    return request.app.state.templates.TemplateResponse(
        request, "index.html", {"step": 1},
    )


@router.get("/progress", response_class=HTMLResponse)
async def progress(request: Request) -> HTMLResponse:
    """Pantalla 2: progreso en vivo del job vía WebSocket."""
    return request.app.state.templates.TemplateResponse(
        request, "progress.html", {"step": 2},
    )


@router.get("/results", response_class=HTMLResponse)
async def results(request: Request) -> HTMLResponse:
    """Pantalla 3: resultados, histograma de energía y sliders de umbrales."""
    return request.app.state.templates.TemplateResponse(
        request, "results.html", {"step": 3},
    )
