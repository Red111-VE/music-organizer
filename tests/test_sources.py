"""Tests para :mod:`core.sources`: URL → texto de tracklist.

Tres capas:

1. Lógica pura (detección de fuente, extracción de IDs, elección del mejor
   comentario) — directo, sin red.
2. Flujos de fetch — monkeypatcheando :func:`core.sources.http_get_json`.
3. :func:`core.sources.http_get_json` REAL contra un servidor HTTP
   loopback — regresiones empíricas de los fallos que ``urllib`` NO
   envuelve en ``URLError`` (``RemoteDisconnected``, ``IncompleteRead``,
   bodies no-UTF-8) y que en una versión previa escapaban como traceback
   crudo en vez de :class:`SourceError`.
"""

from __future__ import annotations

import http.server
import json
import socket
import threading
from collections.abc import Iterator
from typing import Any

import pytest

from core import sources
from core.sources import (
    SourceError,
    detect_source,
    extract_mixcloud_path,
    extract_youtube_id,
    fetch_tracklist_text,
    pick_best_comment,
)


# Tracklist sintético con 4 tracks parseables — supera MIN_TRACKS (3).
_GOOD_TRACKLIST = """\
01. Chris Stussy - All Night Long
02. A-Trak - Sway (LiTek Remix)
03. Prunk - La Manija
04. Locklead - Mr. Madder
"""

# Tracklist más largo (5 tracks) para tests de "gana el más completo".
_LONGER_TRACKLIST = """\
01. Uno - Track
02. Dos - Track
03. Tres - Track
04. Cuatro - Track
05. Cinco - Track
"""

# Texto sin tracks parseables.
_NO_TRACKLIST = "Set grabado en vivo. Gracias por escuchar!\nSuscribite!"

# Descripción típica de YouTube: 3 líneas de links sociales con " - ".
# En una versión previa esto pasaba MIN_TRACKS y tapaba el tracklist real.
_SOCIAL_LINKS_DESC = """\
Sigueme en mis redes!
Instagram - https://instagram.com/djx
Facebook - https://facebook.com/djx
Beatport - https://www.beatport.com/artist/djx
"""


# --------------------------------------------------------------------------- #
# detect_source
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "url,expected",
    [
        pytest.param("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "youtube", id="youtube-watch"),
        pytest.param("https://youtu.be/dQw4w9WgXcQ", "youtube", id="youtu-be"),
        pytest.param("https://m.youtube.com/watch?v=dQw4w9WgXcQ", "youtube", id="youtube-mobile"),
        pytest.param("https://music.youtube.com/watch?v=dQw4w9WgXcQ", "youtube", id="youtube-music"),
        pytest.param("youtube.com/watch?v=dQw4w9WgXcQ", "youtube", id="sin-esquema"),
        pytest.param(
            "https://www.youtube.com:443/watch?v=dQw4w9WgXcQ", "youtube", id="con-puerto",
        ),
        pytest.param(
            "//www.youtube.com/watch?v=dQw4w9WgXcQ", "youtube", id="scheme-relative",
        ),
        pytest.param("https://www.mixcloud.com/user/show/", "mixcloud", id="mixcloud"),
        pytest.param("mixcloud.com/user/show/", "mixcloud", id="mixcloud-sin-esquema"),
        pytest.param("https://mixcloud.com:443/user/show/", "mixcloud", id="mixcloud-con-puerto"),
        pytest.param("https://soundcloud.com/artist/set", None, id="soundcloud-no-soportado"),
        pytest.param("https://example.com/x", None, id="dominio-cualquiera"),
    ],
)
def test_detect_source(url: str, expected: str | None) -> None:
    assert detect_source(url) == expected


# --------------------------------------------------------------------------- #
# extract_youtube_id
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "url,expected",
    [
        pytest.param(
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "dQw4w9WgXcQ", id="watch",
        ),
        pytest.param(
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=123&list=PL1",
            "dQw4w9WgXcQ", id="watch-con-params",
        ),
        pytest.param(
            "https://www.youtube.com/watch/?v=dQw4w9WgXcQ",
            "dQw4w9WgXcQ", id="watch-trailing-slash",
        ),
        pytest.param(
            "https://www.youtube.com:443/watch?v=dQw4w9WgXcQ",
            "dQw4w9WgXcQ", id="watch-con-puerto",
        ),
        pytest.param(
            "//www.youtube.com/watch?v=dQw4w9WgXcQ",
            "dQw4w9WgXcQ", id="scheme-relative",
        ),
        pytest.param(
            "https://youtu.be/dQw4w9WgXcQ",
            "dQw4w9WgXcQ", id="corto",
        ),
        pytest.param(
            "https://youtu.be/dQw4w9WgXcQ?t=42",
            "dQw4w9WgXcQ", id="corto-con-timestamp",
        ),
        pytest.param(
            "https://youtu.be/dQw4w9Wg%2DcQ",
            "dQw4w9Wg-cQ", id="corto-con-pct-encoding",
        ),
        pytest.param(
            "https://www.youtube.com/live/dQw4w9WgXcQ",
            "dQw4w9WgXcQ", id="live",
        ),
        pytest.param(
            "https://www.youtube.com/shorts/dQw4w9WgXcQ",
            "dQw4w9WgXcQ", id="shorts",
        ),
        pytest.param(
            "https://www.youtube.com/embed/dQw4w9WgXcQ",
            "dQw4w9WgXcQ", id="embed",
        ),
        pytest.param(
            "youtube.com/watch?v=dQw4w9WgXcQ",
            "dQw4w9WgXcQ", id="sin-esquema",
        ),
        # Inválidos
        pytest.param("https://www.youtube.com/watch", None, id="watch-sin-v"),
        pytest.param("https://www.youtube.com/watch?v=corto", None, id="id-corto"),
        pytest.param("https://www.youtube.com/@canal", None, id="pagina-de-canal"),
        pytest.param("https://vimeo.com/12345", None, id="otro-dominio"),
        # IDs Unicode: str.isalnum() los aceptaba; el regex ASCII no.
        pytest.param("https://youtu.be/" + "ñ" * 11, None, id="id-unicode-rechazado"),
    ],
)
def test_extract_youtube_id(url: str, expected: str | None) -> None:
    assert extract_youtube_id(url) == expected


# --------------------------------------------------------------------------- #
# extract_mixcloud_path
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "url,expected",
    [
        pytest.param(
            "https://www.mixcloud.com/spartacus/party-time/",
            "spartacus/party-time", id="estandar",
        ),
        pytest.param(
            "https://mixcloud.com/user/show",
            "user/show", id="sin-trailing-slash",
        ),
        pytest.param(
            "https://www.mixcloud.com/user/show/?utm_source=widget",
            "user/show", id="con-query",
        ),
        pytest.param(
            "https://www.mixcloud.com:443/user/show/",
            "user/show", id="con-puerto",
        ),
        # Inválidos
        pytest.param("https://www.mixcloud.com/user/", None, id="solo-perfil"),
        pytest.param("https://www.mixcloud.com/discover/tech-house/", None, id="pagina-discover"),
        pytest.param(
            "https://www.mixcloud.com/user/show/extra/", None, id="tres-segmentos",
        ),
        pytest.param("https://youtube.com/watch?v=x", None, id="otro-dominio"),
    ],
)
def test_extract_mixcloud_path(url: str, expected: str | None) -> None:
    assert extract_mixcloud_path(url) == expected


# --------------------------------------------------------------------------- #
# pick_best_comment
# --------------------------------------------------------------------------- #

def test_pick_best_comment_elige_el_de_mas_tracks() -> None:
    comments = ["temazo el del minuto 34!!", _GOOD_TRACKLIST, _LONGER_TRACKLIST]
    best = pick_best_comment(comments)
    assert best is not None
    assert "Cinco" in best  # ganó el de 5 tracks


def test_pick_best_comment_exige_minimo() -> None:
    """Comentarios con 1-2 líneas con guiones no son un tracklist."""
    comments = [
        "mi favorito: Artist - Title",
        "van dos: A - B\ny C - D",
    ]
    assert pick_best_comment(comments) is None


def test_pick_best_comment_vacio() -> None:
    assert pick_best_comment([]) is None


# --------------------------------------------------------------------------- #
# fetch_tracklist_text — YouTube (HTTP monkeypatcheado)
# --------------------------------------------------------------------------- #

def _youtube_videos_response(description: str, title: str = "Mi Set") -> dict[str, Any]:
    return {"items": [{"snippet": {"title": title, "description": description}}]}


def _youtube_comments_response(*texts: str) -> dict[str, Any]:
    return {
        "items": [
            {"snippet": {"topLevelComment": {"snippet": {"textOriginal": t}}}}
            for t in texts
        ]
    }


def _youtube_fake(
    description: str, *comments: str,
) -> tuple[Any, list[str]]:
    """Fake de http_get_json para los flujos de YouTube. Devuelve
    ``(fake, calls)`` — ``calls`` registra las URLs consultadas."""
    calls: list[str] = []

    def fake(url: str) -> dict[str, Any]:
        calls.append(url)
        if "youtube/v3/videos" in url:
            return _youtube_videos_response(description)
        assert "youtube/v3/commentThreads" in url
        return _youtube_comments_response(*comments)

    return fake, calls


def test_youtube_descripcion_con_tracklist(monkeypatch: pytest.MonkeyPatch) -> None:
    fake, calls = _youtube_fake(_GOOD_TRACKLIST)  # sin comentarios
    monkeypatch.setattr(sources, "http_get_json", fake)
    result = fetch_tracklist_text(
        "https://youtu.be/dQw4w9WgXcQ", youtube_api_key="K",
    )
    assert result.origin == "youtube:descripcion"
    assert result.title == "Mi Set"
    assert "Chris Stussy" in result.text
    # Los comentarios SIEMPRE se consultan (para comparar) — 2 llamadas.
    assert len(calls) == 2


def test_youtube_links_sociales_no_ganan_al_comentario(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REGRESIÓN del bug major: la descripción con links sociales
    ('Instagram - https://...') parseaba 3 "tracks" y tapaba el comentario
    fijado con el tracklist real, sin siquiera consultarlo."""
    fake, _ = _youtube_fake(_SOCIAL_LINKS_DESC, "buen set!", _GOOD_TRACKLIST)
    monkeypatch.setattr(sources, "http_get_json", fake)
    result = fetch_tracklist_text(
        "https://youtu.be/dQw4w9WgXcQ", youtube_api_key="K",
    )
    assert result.origin == "youtube:comentario"
    assert "Chris Stussy" in result.text


def test_youtube_comentario_mas_completo_gana(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Descripción con tracklist parcial (4) vs comentario completo (5):
    gana el que más tracks parsea."""
    fake, _ = _youtube_fake(_GOOD_TRACKLIST, _LONGER_TRACKLIST)
    monkeypatch.setattr(sources, "http_get_json", fake)
    result = fetch_tracklist_text(
        "https://youtu.be/dQw4w9WgXcQ", youtube_api_key="K",
    )
    assert result.origin == "youtube:comentario"
    assert "Cinco" in result.text


def test_youtube_empate_gana_descripcion(monkeypatch: pytest.MonkeyPatch) -> None:
    """A igual cantidad de tracks, la descripción (palabra del autor del
    video) le gana al comentario."""
    fake, _ = _youtube_fake(_GOOD_TRACKLIST, _GOOD_TRACKLIST)
    monkeypatch.setattr(sources, "http_get_json", fake)
    result = fetch_tracklist_text(
        "https://youtu.be/dQw4w9WgXcQ", youtube_api_key="K",
    )
    assert result.origin == "youtube:descripcion"


def test_youtube_sin_tracklist_en_ningun_lado(monkeypatch: pytest.MonkeyPatch) -> None:
    fake, _ = _youtube_fake(_NO_TRACKLIST, "fuego!", "el drop del 34")
    monkeypatch.setattr(sources, "http_get_json", fake)
    with pytest.raises(SourceError, match="no tiene un tracklist parseable"):
        fetch_tracklist_text("https://youtu.be/dQw4w9WgXcQ", youtube_api_key="K")


def test_youtube_comentarios_deshabilitados_no_es_fatal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """403 en commentThreads (comentarios off) no debe crashear: el error
    final es el de "sin tracklist", no el HTTP."""
    def fake_get(url: str) -> dict[str, Any]:
        if "youtube/v3/videos" in url:
            return _youtube_videos_response(_NO_TRACKLIST)
        raise SourceError("La API respondió 403: comments disabled")

    monkeypatch.setattr(sources, "http_get_json", fake_get)
    with pytest.raises(SourceError, match="no tiene un tracklist parseable"):
        fetch_tracklist_text("https://youtu.be/dQw4w9WgXcQ", youtube_api_key="K")


def test_youtube_video_inexistente(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sources, "http_get_json", lambda url: {"items": []},
    )
    with pytest.raises(SourceError, match="privado o"):
        fetch_tracklist_text("https://youtu.be/dQw4w9WgXcQ", youtube_api_key="K")


def test_youtube_sin_key_no_toca_la_red(monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(url: str) -> dict[str, Any]:
        raise AssertionError("no debería tocar la red sin key")

    monkeypatch.setattr(sources, "http_get_json", explode)
    with pytest.raises(SourceError, match="API key gratuita"):
        fetch_tracklist_text("https://youtu.be/dQw4w9WgXcQ")


# --------------------------------------------------------------------------- #
# fetch_tracklist_text — Mixcloud (HTTP monkeypatcheado)
# --------------------------------------------------------------------------- #

def test_mixcloud_descripcion(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(url: str) -> dict[str, Any]:
        assert url == "https://api.mixcloud.com/user/show/"
        return {
            "name": "Show 42",
            "description": _GOOD_TRACKLIST,
            "sections": [],
        }

    monkeypatch.setattr(sources, "http_get_json", fake_get)
    result = fetch_tracklist_text("https://www.mixcloud.com/user/show/")
    assert result.origin == "mixcloud:descripcion"
    assert result.title == "Show 42"


def test_mixcloud_path_unicode_se_encodea(monkeypatch: pytest.MonkeyPatch) -> None:
    """REGRESIÓN del bug major: un path con acentos (como lo copia
    Firefox/Safari, %-decodeado) crasheaba con UnicodeEncodeError en
    http.client antes de tocar la red."""
    captured: list[str] = []

    def fake_get(url: str) -> dict[str, Any]:
        captured.append(url)
        return {"name": "S", "description": _GOOD_TRACKLIST, "sections": []}

    monkeypatch.setattr(sources, "http_get_json", fake_get)
    result = fetch_tracklist_text("https://www.mixcloud.com/café-dj/show/")
    assert result.origin == "mixcloud:descripcion"
    assert captured == ["https://api.mixcloud.com/caf%C3%A9-dj/show/"]


def test_mixcloud_path_ya_encodeado_no_se_doble_encodea(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[str] = []

    def fake_get(url: str) -> dict[str, Any]:
        captured.append(url)
        return {"name": "S", "description": _GOOD_TRACKLIST, "sections": []}

    monkeypatch.setattr(sources, "http_get_json", fake_get)
    fetch_tracklist_text("https://www.mixcloud.com/caf%C3%A9-dj/show/")
    assert captured == ["https://api.mixcloud.com/caf%C3%A9-dj/show/"]


def test_mixcloud_sections_ganan_a_descripcion(monkeypatch: pytest.MonkeyPatch) -> None:
    """Si sections viene poblado (hoy casi nunca), es la mejor fuente."""
    sections = [
        {"track": {"name": f"Track {i}", "artist": {"name": f"Artist {i}"}}}
        for i in range(4)
    ]
    monkeypatch.setattr(
        sources, "http_get_json",
        lambda url: {"name": "S", "description": _GOOD_TRACKLIST, "sections": sections},
    )
    result = fetch_tracklist_text("https://www.mixcloud.com/user/show/")
    assert result.origin == "mixcloud:sections"
    assert "Artist 0 - Track 0" in result.text


@pytest.mark.parametrize(
    "sections",
    [
        pytest.param(
            [{"start_time": 0}, {"track": {"name": "Solo Titulo"}}],
            id="entries-incompletas",
        ),
        pytest.param(
            ["solo-un-string", {"track": "ID"}],
            id="entry-y-track-string",
        ),
        pytest.param(
            [{"track": {"name": "T", "artist": "A-como-string"}}],
            id="artist-string",
        ),
        pytest.param(
            [{"track": {"name": None, "artist": {"name": "A"}}}],
            id="name-null-no-produce-linea-None",
        ),
        pytest.param({"no": "es-lista"}, id="sections-dict"),
        pytest.param("ni-siquiera-lista", id="sections-string"),
    ],
)
def test_mixcloud_sections_hostiles_no_crashean(
    monkeypatch: pytest.MonkeyPatch, sections: Any,
) -> None:
    """REGRESIÓN del bug major: shapes no-dict en sections crasheaban con
    AttributeError crudo. Ahora cualquier shape inesperado se salta y se
    cae a la descripción."""
    monkeypatch.setattr(
        sources, "http_get_json",
        lambda url: {"name": "S", "description": _GOOD_TRACKLIST, "sections": sections},
    )
    result = fetch_tracklist_text("https://www.mixcloud.com/user/show/")
    assert result.origin == "mixcloud:descripcion"


def test_mixcloud_sin_tracklist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sources, "http_get_json",
        lambda url: {"name": "S", "description": _NO_TRACKLIST, "sections": []},
    )
    with pytest.raises(SourceError, match="no tiene un tracklist parseable"):
        fetch_tracklist_text("https://www.mixcloud.com/user/show/")


# --------------------------------------------------------------------------- #
# fetch_tracklist_text — URLs no soportadas
# --------------------------------------------------------------------------- #

def test_soundcloud_error_especifico() -> None:
    with pytest.raises(SourceError, match="SoundCloud no tiene API"):
        fetch_tracklist_text("https://soundcloud.com/artist/set")


def test_soundcloud_shortener_tambien() -> None:
    with pytest.raises(SourceError, match="SoundCloud no tiene API"):
        fetch_tracklist_text("https://on.soundcloud.com/abc123")


def test_soundcloud_en_query_no_confunde() -> None:
    """'soundcloud.com' en el query de otra URL no debe disparar el
    mensaje específico de SoundCloud — el check es por hostname."""
    with pytest.raises(SourceError, match="URL no soportada"):
        fetch_tracklist_text("https://example.com/?ref=soundcloud.com")


def test_url_generica_no_soportada() -> None:
    with pytest.raises(SourceError, match="URL no soportada"):
        fetch_tracklist_text("https://example.com/lo-que-sea")


def test_youtube_url_de_canal_falla_claro() -> None:
    with pytest.raises(SourceError, match="No pude extraer el ID"):
        fetch_tracklist_text(
            "https://www.youtube.com/@algundj", youtube_api_key="K",
        )


# --------------------------------------------------------------------------- #
# http_get_json REAL contra servidor loopback
#
# Regresión empírica de los fallos que urllib NO envuelve en URLError y
# que escapaban como traceback crudo: RemoteDisconnected, IncompleteRead
# y bodies no-UTF-8. También: JSON inválido, JSON no-dict, y extracción
# del mensaje de error del body de Google.
# --------------------------------------------------------------------------- #

class _FaultyHandler(http.server.BaseHTTPRequestHandler):
    """Servidor de fallos: cada path simula un modo de ruptura real."""

    def do_GET(self) -> None:  # noqa: N802 — nombre fijado por BaseHTTPRequestHandler
        if self.path == "/ok":
            self._send(200, b'{"name": "ok"}')
        elif self.path == "/bad-json":
            self._send(200, b"esto no es json")
        elif self.path == "/non-dict":
            self._send(200, b"[1, 2, 3]")
        elif self.path == "/not-utf8":
            self._send(200, b"\xff\xfe\x9c\x00")
        elif self.path == "/google-error":
            body = json.dumps(
                {"error": {"code": 403, "message": "comments are disabled"}}
            ).encode()
            self._send(403, body)
        elif self.path == "/truncated":
            # Content-Length mayor que el body real → IncompleteRead en
            # el cliente (conexión flaky típica).
            self.send_response(200)
            self.send_header("Content-Length", "9999")
            self.end_headers()
            self.wfile.write(b'{"a"')
        elif self.path == "/disconnect":
            # Cerrar sin responder → RemoteDisconnected en el cliente.
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.connection.close()
        else:
            self._send(404, b"{}")

    def _send(self, code: int, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        pass  # silenciar el log por request en la salida de pytest


class _QuietServer(http.server.ThreadingHTTPServer):
    def handle_error(self, request: Any, client_address: Any) -> None:
        pass  # /disconnect rompe el socket a propósito; no ensuciar stderr


@pytest.fixture(scope="module")
def faulty_server() -> Iterator[str]:
    server = _QuietServer(("127.0.0.1", 0), _FaultyHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def test_http_ok(faulty_server: str) -> None:
    assert sources.http_get_json(f"{faulty_server}/ok") == {"name": "ok"}


@pytest.mark.parametrize(
    "path",
    [
        pytest.param("/bad-json", id="json-invalido"),
        pytest.param("/non-dict", id="json-no-dict"),
        pytest.param("/not-utf8", id="body-no-utf8"),
        pytest.param("/truncated", id="body-truncado-IncompleteRead"),
        pytest.param("/disconnect", id="cierre-sin-respuesta-RemoteDisconnected"),
    ],
)
def test_http_fallos_se_traducen_a_source_error(
    faulty_server: str, path: str,
) -> None:
    """NINGÚN fallo de red/parseo escapa como traceback crudo."""
    with pytest.raises(SourceError):
        sources.http_get_json(f"{faulty_server}{path}")


def test_http_error_extrae_mensaje_de_google(faulty_server: str) -> None:
    with pytest.raises(SourceError, match="403.*comments are disabled"):
        sources.http_get_json(f"{faulty_server}/google-error")
