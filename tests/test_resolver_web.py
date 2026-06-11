"""Tests para :mod:`web.routes.resolver`: los endpoints del resolver web.

TestClient de FastAPI con la red monkeypatcheada en los puntos que el
módulo importa (``web.routes.resolver.resolve_track`` /
``fetch_tracklist_text``) y el tracker SIEMPRE apuntando a ``tmp_path``
vía ``STORE_PATH`` — jamás se toca ``~/.music-organizer``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from core.resolver import Candidate, TrackResolution
from core.sources import SourceError, SourceResult
from core.tracklist import ParsedTrack
from web.main import create_app
from web.routes import resolver as resolver_routes


_TRACKLIST = """\
01. Chris Stussy - All Night Long
02. ID - ID
03. Artista Inventado - Track Inexistente
"""


def _fake_resolve(track: ParsedTrack, **kwargs: object) -> TrackResolution:
    if track.is_id:
        return TrackResolution(
            track=track, status="id", best=None, youtube_search_url="",
        )
    if track.artist == "Chris Stussy":
        return TrackResolution(
            track=track, status="ok",
            best=Candidate(
                provider="deezer", artist="Chris Stussy",
                title="All Night Long",
                url="https://www.deezer.com/track/1", score=100.0,
            ),
            youtube_search_url="https://www.youtube.com/results?search_query=x",
        )
    return TrackResolution(
        track=track, status="no-encontrado", best=None,
        youtube_search_url="https://www.youtube.com/results?search_query=y",
    )


@pytest.fixture()
def client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> TestClient:
    monkeypatch.setattr(resolver_routes, "resolve_track", _fake_resolve)
    monkeypatch.setattr(
        resolver_routes, "STORE_PATH", tmp_path / "unreleased.json",
    )
    return TestClient(create_app())


# --------------------------------------------------------------------------- #
# Página
# --------------------------------------------------------------------------- #

def test_pagina_resolver_renderiza(client: TestClient) -> None:
    r = client.get("/resolver")
    assert r.status_code == 200
    assert "resolverView()" in r.text
    assert "Resolver de tracklists" in r.text


def test_panel_embebido_en_la_principal(client: TestClient) -> None:
    """El resolver vive en `/` al LADO del análisis (grilla de dos
    columnas, partial compartido) — cada herramienta con su componente
    Alpine independiente y el script definido UNA sola vez."""
    html = client.get("/").text
    assert "setupForm()" in html                      # el análisis sigue
    assert "resolverView()" in html                   # el panel está
    assert html.count("function resolverView()") == 1
    assert "tools-grid" in html                       # layout lado a lado
    assert 'class="main main--wide"' in html or "main--wide" in html


def test_nav_link_activo_solo_en_resolver(client: TestClient) -> None:
    en_resolver = client.get("/resolver").text
    en_setup = client.get("/").text
    # El link existe en ambas; is-active solo en /resolver.
    assert 'href="/resolver"' in en_setup

    def link_class(html: str) -> str:
        for line in html.splitlines():
            if 'class="step step-link' in line:  # el anchor, no el CSS
                return line
        return ""

    assert "is-active" in link_class(en_resolver)
    assert "is-active" not in link_class(en_setup)


# --------------------------------------------------------------------------- #
# POST /api/resolver/parse
# --------------------------------------------------------------------------- #

def test_parse_texto(client: TestClient) -> None:
    r = client.post("/api/resolver/parse", json={"text": _TRACKLIST})
    assert r.status_code == 200
    data = r.json()
    assert data["origin"] == "texto pegado"
    assert len(data["tracks"]) == 3
    assert data["tracks"][0]["artist"] == "Chris Stussy"
    assert data["tracks"][1]["is_id"] is True


def test_parse_url(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_fetch(url: str, **kwargs: object) -> SourceResult:
        assert "youtu" in url
        return SourceResult(
            text=_TRACKLIST, origin="youtube:descripcion", title="Mi Set",
        )

    monkeypatch.setattr(resolver_routes, "fetch_tracklist_text", fake_fetch)
    r = client.post(
        "/api/resolver/parse", json={"text": "https://youtu.be/dQw4w9WgXcQ"},
    )
    assert r.status_code == 200
    data = r.json()
    assert "youtube:descripcion" in data["origin"]
    assert "Mi Set" in data["origin"]
    assert len(data["tracks"]) == 3


def test_parse_url_con_source_error(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_fetch(url: str, **kwargs: object) -> SourceResult:
        raise SourceError("Para leer YouTube hace falta una API key gratuita")

    monkeypatch.setattr(resolver_routes, "fetch_tracklist_text", fake_fetch)
    r = client.post(
        "/api/resolver/parse", json={"text": "https://youtu.be/dQw4w9WgXcQ"},
    )
    assert r.status_code == 400
    assert "API key gratuita" in r.json()["detail"]


def test_parse_vacio(client: TestClient) -> None:
    r = client.post("/api/resolver/parse", json={"text": "   "})
    assert r.status_code == 400


def test_parse_sin_tracks(client: TestClient) -> None:
    r = client.post(
        "/api/resolver/parse", json={"text": "hola\nesto no tiene tracks"},
    )
    assert r.status_code == 400
    assert "parseables" in r.json()["detail"]


# --------------------------------------------------------------------------- #
# POST /api/resolver/track
# --------------------------------------------------------------------------- #

def test_track_ok(client: TestClient) -> None:
    r = client.post("/api/resolver/track", json={
        "raw": "Chris Stussy - All Night Long", "line_no": 1,
        "artist": "Chris Stussy", "title": "All Night Long",
    })
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok"
    assert data["best"]["provider"] == "deezer"
    assert data["best"]["score"] == 100.0


def test_track_no_encontrado_va_al_tracker(
    client: TestClient, tmp_path: Path,
) -> None:
    r = client.post("/api/resolver/track", json={
        "raw": "x", "line_no": 1,
        "artist": "Artista Inventado", "title": "Track Inexistente",
    })
    assert r.status_code == 200
    assert r.json()["status"] == "no-encontrado"

    store_file = tmp_path / "unreleased.json"
    data = json.loads(store_file.read_text(encoding="utf-8"))
    assert [t["artist"] for t in data["tracks"]] == ["Artista Inventado"]


def test_track_fallo_de_red_no_contamina_tracker(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mismas reglas que el CLI: no-encontrado con notes (errores de
    proveedor) NO se anota."""
    def network_down(track: ParsedTrack, **kwargs: object) -> TrackResolution:
        return TrackResolution(
            track=track, status="no-encontrado", best=None,
            youtube_search_url="",
            notes=["deezer: Error de red"],
        )

    monkeypatch.setattr(resolver_routes, "resolve_track", network_down)
    r = client.post("/api/resolver/track", json={
        "raw": "x", "line_no": 1, "artist": "Real", "title": "Existe",
    })
    assert r.status_code == 200
    assert not (tmp_path / "unreleased.json").exists()


def test_track_id_no_va_al_tracker(client: TestClient, tmp_path: Path) -> None:
    r = client.post("/api/resolver/track", json={
        "raw": "ID - ID", "line_no": 1,
        "artist": "ID", "title": "ID", "is_id": True,
    })
    assert r.status_code == 200
    assert r.json()["status"] == "id"
    assert not (tmp_path / "unreleased.json").exists()


# --------------------------------------------------------------------------- #
# Tracker: GET /unreleased + POST /recheck
# --------------------------------------------------------------------------- #

def _seed_store(tmp_path: Path) -> Path:
    store_file = tmp_path / "unreleased.json"
    store_file.write_text(json.dumps({
        "version": 1,
        "tracks": [
            {"artist": "Chris Stussy", "title": "All Night Long",
             "first_seen": "2026-01-01", "last_checked": "2026-01-01",
             "times_seen": 2},
            {"artist": "Artista Inventado", "title": "Track Inexistente",
             "first_seen": "2026-01-01", "last_checked": "2026-01-01"},
        ],
    }))
    return store_file


def test_unreleased_lista(client: TestClient, tmp_path: Path) -> None:
    _seed_store(tmp_path)
    r = client.get("/api/resolver/unreleased")
    assert r.status_code == 200
    tracks = r.json()["tracks"]
    assert len(tracks) == 2
    assert tracks[0]["times_seen"] == 2


def test_unreleased_corrupto_500_accionable(
    client: TestClient, tmp_path: Path,
) -> None:
    (tmp_path / "unreleased.json").write_text("{{{corrupto")
    r = client.get("/api/resolver/unreleased")
    assert r.status_code == 500
    assert "corrupto" in r.json()["detail"]


def test_recheck(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """El recheck usa core.unreleased.resolve_track — se patchea allá."""
    _seed_store(tmp_path)
    from core import unreleased as unreleased_module
    monkeypatch.setattr(unreleased_module, "resolve_track", _fake_resolve)

    r = client.post("/api/resolver/recheck")
    assert r.status_code == 200
    data = r.json()
    assert len(data["found"]) == 1
    assert data["found"][0]["artist"] == "Chris Stussy"
    assert data["found"][0]["match"]["best"]["url"].startswith("https://www.deezer")
    assert data["remaining"] == 1

    # Persistido: el que salió ya no está.
    stored = json.loads((tmp_path / "unreleased.json").read_text())
    assert [t["artist"] for t in stored["tracks"]] == ["Artista Inventado"]
