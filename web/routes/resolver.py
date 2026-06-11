"""Routes del resolver de tracklists (4ª pantalla de la web).

A diferencia del pipeline (subprocess + WebSocket — un análisis dura
~90 min), resolver un tracklist son segundos por track: acá el patrón es
**un endpoint por track** y el frontend itera secuencialmente, pintando
cada fila al llegar. Progreso en vivo sin WS, sin estado en el server,
sin subprocess. Si el usuario cierra la pestaña, simplemente deja de
iterar — no hay job huérfano que cancelar.

Endpoints (montados bajo ``/api/resolver``):

- ``POST /parse``     — texto pegado o URL → tracks parseados.
- ``POST /track``     — resuelve UN track (Deezer → iTunes → YouTube).
  Anota los no-encontrados limpios en el tracker (mismas reglas que el
  CLI: con nombre y sin errores de proveedor).
- ``GET  /unreleased``— entradas del tracker, para la sección "en
  seguimiento".
- ``POST /recheck``   — re-consulta todo el tracker; devuelve los que
  salieron y cuántos quedan.

**Sync a propósito**: los handlers son ``def`` (no ``async def``) — la
resolución usa red sincrónica + ``time.sleep`` del throttle, y FastAPI
corre los handlers sync en su threadpool, así el event loop (que atiende
el WebSocket del pipeline) nunca se bloquea.

La key de YouTube se lee del env ``YOUTUBE_API_KEY`` del proceso del
server — la misma que usa el CLI. Sin key: Deezer/iTunes igual funcionan.

``STORE_PATH`` es module-level para que los tests apunten el tracker a un
tmp y JAMÁS toquen ``~/.music-organizer``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from core.resolver import TrackResolution, resolve_track
from core.sources import SourceError, detect_source, fetch_tracklist_text
from core.tracklist import ParsedTrack, parse_tracklist
from core.unreleased import (
    UnreleasedStore,
    UnreleasedStoreError,
    recheck_store,
)


router = APIRouter()

# Path del tracker. None = default (~/.music-organizer/unreleased.json).
# Los tests lo monkeypatchean a un tmp_path.
STORE_PATH: Path | None = None


def _youtube_key() -> str | None:
    return os.environ.get("YOUTUBE_API_KEY") or None


# --------------------------------------------------------------------------- #
# Parse (texto pegado o URL)
# --------------------------------------------------------------------------- #

class ParseRequest(BaseModel):
    text: str


@router.post("/parse")
def parse(req: ParseRequest) -> dict[str, Any]:
    """Texto pegado o URL → tracks estructurados.

    Una sola línea que parece URL se trata como link (mismo criterio que
    el CLI); todo lo demás es texto de tracklist. Los errores de fuente
    (key faltante, video sin tracklist, SoundCloud) salen como 400 con el
    mensaje accionable de :class:`SourceError` — el frontend lo muestra
    tal cual.
    """
    text = req.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="El input está vacío.")

    origin = "texto pegado"
    looks_like_url = "\n" not in text and (
        detect_source(text) is not None
        or text.lower().startswith(("http://", "https://", "www."))
    )
    if looks_like_url:
        try:
            result = fetch_tracklist_text(text, youtube_api_key=_youtube_key())
        except SourceError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        text = result.text
        origin = result.origin + (f" — «{result.title}»" if result.title else "")

    tracks = parse_tracklist(text)
    if not tracks:
        raise HTTPException(
            status_code=400,
            detail="No encontré líneas de track parseables. Formato "
                   "esperado por línea: «Artista - Título».",
        )

    return {
        "origin": origin,
        "tracks": [
            {
                "raw": t.raw,
                "line_no": t.line_no,
                "artist": t.artist,
                "title": t.title,
                "remix": t.remix,
                "is_id": t.is_id,
            }
            for t in tracks
        ],
    }


# --------------------------------------------------------------------------- #
# Resolver un track
# --------------------------------------------------------------------------- #

class TrackRequest(BaseModel):
    raw: str = ""
    line_no: int = 0
    artist: str = ""
    title: str = ""
    remix: str = ""
    is_id: bool = False


@router.post("/track")
def resolve_one(req: TrackRequest) -> dict[str, Any]:
    """Resuelve UN track. El frontend llama esto en serie, track por
    track — cada respuesta pinta una fila. El throttle de proveedores
    vive en core y aplica igual que en el CLI.
    """
    track = ParsedTrack(
        raw=req.raw,
        line_no=req.line_no,
        artist=req.artist,
        title=req.title,
        remix=req.remix,
        is_id=req.is_id,
    )
    resolution = resolve_track(track, youtube_api_key=_youtube_key())
    _maybe_track_unreleased(resolution)
    return _serialize_resolution(resolution)


def _maybe_track_unreleased(resolution: TrackResolution) -> None:
    """Anota en el tracker con las MISMAS reglas que el CLI: solo
    no-encontrados con nombre y sin errores de proveedor (un corte de red
    no debe contaminar el tracker). Errores del tracker no rompen la
    resolución — acá es secundario."""
    if resolution.status != "no-encontrado" or resolution.notes:
        return
    try:
        store = UnreleasedStore(STORE_PATH)
        store.add(resolution.track)  # nuevo, o times_seen++ si ya estaba
        store.save()
    except UnreleasedStoreError:
        pass


def _serialize_resolution(resolution: TrackResolution) -> dict[str, Any]:
    best = None
    if resolution.best is not None:
        best = {
            "provider": resolution.best.provider,
            "artist": resolution.best.artist,
            "title": resolution.best.title,
            "url": resolution.best.url,
            "score": resolution.best.score,
        }
    return {
        "status": resolution.status,
        "best": best,
        "youtube_search_url": resolution.youtube_search_url,
        "notes": resolution.notes,
    }


# --------------------------------------------------------------------------- #
# Tracker de unreleased
# --------------------------------------------------------------------------- #

@router.get("/unreleased")
def unreleased_list() -> dict[str, Any]:
    """Las entradas del tracker, para la sección "en seguimiento"."""
    try:
        store = UnreleasedStore(STORE_PATH)
    except UnreleasedStoreError as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    return {
        "tracks": [
            {
                "artist": e.artist,
                "title": e.title,
                "remix": e.remix,
                "first_seen": e.first_seen,
                "last_checked": e.last_checked,
                "times_seen": e.times_seen,
            }
            for e in store.entries
        ],
    }


@router.post("/recheck")
def recheck() -> dict[str, Any]:
    """Re-consulta todo el tracker (sincrónico — con el throttle de
    Deezer son ~0.3 s por entrada; el frontend muestra spinner). Devuelve
    los que salieron y cuántos quedan en seguimiento."""
    try:
        store = UnreleasedStore(STORE_PATH)
    except UnreleasedStoreError as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

    found = recheck_store(store, youtube_api_key=_youtube_key())
    return {
        "found": [
            {
                "artist": entry.artist,
                "title": entry.title,
                "remix": entry.remix,
                "match": _serialize_resolution(resolution),
            }
            for entry, resolution in found
        ],
        "remaining": len(store),
    }
