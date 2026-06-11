"""Obtención de tracklists desde URLs: YouTube y Mixcloud → texto.

Segundo componente del resolver de tracklists (v0.3.x). Dado el link de un
set, intenta extraer el texto del tracklist para alimentar a
:func:`core.tracklist.parse_tracklist`.

Fuentes soportadas y sus reglas:

- **Mixcloud** — API pública, sin key (``api.mixcloud.com``). Camino
  principal: el campo ``description`` del cloudcast. El campo ``sections``
  (tracklist estructurado) existe en el JSON pero Mixcloud lo entrega vacío
  desde hace años por licencias — se aprovecha defensivamente si alguna vez
  viene poblado (verificado empíricamente: 10/10 cloudcasts con
  ``sections: []``).
- **YouTube** — requiere una API key **gratuita** de Google Cloud
  (10.000 unidades/día; cada consulta de este módulo cuesta 1 unidad,
  verificado contra la tabla oficial de cuotas). Se leen la descripción
  del video Y los comentarios más relevantes, y gana el texto con más
  líneas de track parseables — muchos sets tienen 3 líneas de links
  sociales en la descripción y el tracklist real en un comentario fijado,
  así que cortar temprano por la descripción elegiría basura. El
  "detector" de tracklists es el propio parser.
- **SoundCloud** — sin soporte: su API está cerrada a registros nuevos
  desde hace años y scrapear viola ToS. El error lo dice explícito y
  sugiere pegar el texto.

Política de errores: todo problema accionable por el usuario (key
faltante, video inexistente, sin tracklist encontrable) y todo fallo de
red/HTTP/parseo se lanza como :class:`SourceError` con mensaje listo para
mostrar. El CLI lo captura y lo presenta sin traceback. Esto incluye los
fallos de bajo nivel que ``urllib`` NO envuelve en ``URLError``
(``RemoteDisconnected``, ``IncompleteRead``, bodies no-UTF-8) — verificado
empíricamente con un servidor loopback que los tests replican.

Red con ``urllib`` (stdlib): cero dependencias nuevas. El único punto de
I/O es :func:`http_get_json` — los tests de flujo lo monkeypatchean y los
tests de red lo ejercitan contra un servidor local.
"""

from __future__ import annotations

import http.client
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from core.tracklist import parse_tracklist


class SourceError(Exception):
    """Error accionable por el usuario al obtener el tracklist de una URL.

    El mensaje está pensado para mostrarse directo en el CLI/web, sin
    traceback. Incluye siempre el siguiente paso sugerido (típicamente
    "pegá el texto directamente").
    """


@dataclass
class SourceResult:
    """Texto de tracklist obtenido de una URL, con su procedencia.

    ``origin`` permite al CLI decir de dónde salió el texto
    (``youtube:descripcion``, ``youtube:comentario``,
    ``mixcloud:descripcion``, ``mixcloud:sections``). ``title`` es el
    título del video/show cuando la fuente lo da — sirve para el header
    del reporte.
    """
    text: str
    origin: str
    title: str = ""


# Mínimo de líneas parseables para aceptar un texto como tracklist. Por
# debajo de esto se asume que la descripción/comentario habla de otra cosa
# (créditos, agradecimientos) y se sigue buscando.
MIN_TRACKS = 3

# Máximo de comentarios de YouTube a examinar. order=relevance pone los
# fijados/likeados primero, que es donde viven los tracklists.
_MAX_COMMENTS = 25

_USER_AGENT = "music-organizer/0.1 (+https://github.com/Red111-VE/music-organizer)"

_TIMEOUT_S = 10.0

# Primer segmento de path de Mixcloud que NO es un usuario (páginas del
# sitio). Un cloudcast real es siempre /<usuario>/<slug>/.
_MIXCLOUD_RESERVED = frozenset({
    "discover", "search", "live", "upload", "settings", "premium",
    "pro", "categories", "tag", "about", "jobs", "legal", "select",
})

_YOUTUBE_HOSTS = frozenset({
    "youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com",
})

_MIXCLOUD_HOSTS = frozenset({"mixcloud.com", "www.mixcloud.com"})

# IDs de video de YouTube: exactamente 11 chars del alfabeto base64-url.
# fullmatch con regex (no str.isalnum, que acepta letras/dígitos Unicode
# y dejaría pasar IDs basura que gastan cuota para nada).
_RE_YOUTUBE_ID = re.compile(r"[A-Za-z0-9_-]{11}")


# --------------------------------------------------------------------------- #
# Detección de fuente y extracción de identificadores (lógica pura)
# --------------------------------------------------------------------------- #

def detect_source(url: str) -> str | None:
    """Clasifica una URL: ``"youtube"``, ``"mixcloud"`` o ``None``.

    Tolera URLs sin esquema (``youtube.com/watch?v=...``) y scheme-relative
    (``//www.youtube.com/...``, lo que se copia de un ``src=`` de HTML).
    Compara contra ``hostname`` (no ``netloc``): ignora puerto explícito
    (``youtube.com:443``) y userinfo, y ya viene lowercaseado.
    """
    host = _hostname(url)
    if host in _YOUTUBE_HOSTS or host == "youtu.be":
        return "youtube"
    if host in _MIXCLOUD_HOSTS:
        return "mixcloud"
    return None


def extract_youtube_id(url: str) -> str | None:
    """Saca el ID de video (11 chars ``[A-Za-z0-9_-]``) de una URL de YouTube.

    Formatos: ``watch?v=ID`` (con o sin ``/`` final en el path),
    ``youtu.be/ID``, ``/live/ID``, ``/shorts/ID``, ``/embed/ID`` — con
    cualquier query extra (``&t=``, ``&list=``). Los segmentos de path se
    %-decodean igual que ``parse_qs`` decodea ``?v=`` — consistencia entre
    ambas formas.
    """
    parsed = _parse_url(url)
    host = parsed.hostname or ""
    candidate = ""

    if host == "youtu.be":
        path = urllib.parse.unquote(parsed.path)
        candidate = path.lstrip("/").split("/")[0]
    elif host in _YOUTUBE_HOSTS:
        path = urllib.parse.unquote(parsed.path)
        segments = [s for s in path.split("/") if s]
        if path in ("/watch", "/watch/"):
            query = urllib.parse.parse_qs(parsed.query)
            candidate = (query.get("v") or [""])[0]
        elif len(segments) == 2 and segments[0] in ("live", "shorts", "embed"):
            candidate = segments[1]

    return candidate if _RE_YOUTUBE_ID.fullmatch(candidate) else None


def extract_mixcloud_path(url: str) -> str | None:
    """Saca ``"usuario/slug"`` de una URL de cloudcast de Mixcloud.

    Un cloudcast es siempre ``mixcloud.com/<usuario>/<slug>/``; cualquier
    otra forma (página del sitio, perfil de usuario) devuelve ``None``.
    El path puede venir con Unicode crudo (Firefox/Safari muestran los
    paths %-decodeados al copiarlos) — :func:`_fetch_mixcloud` lo
    re-encodea al construir la URL de la API.
    """
    parsed = _parse_url(url)
    if (parsed.hostname or "") not in _MIXCLOUD_HOSTS:
        return None
    segments = [s for s in parsed.path.split("/") if s]
    if len(segments) != 2:
        return None
    if segments[0].lower() in _MIXCLOUD_RESERVED:
        return None
    return f"{segments[0]}/{segments[1]}"


def pick_best_comment(comments: list[str]) -> str | None:
    """Elige el comentario con más líneas de track parseables.

    El parser de :mod:`core.tracklist` actúa como detector: un comentario
    "qué temazo el del minuto 34" parsea 0 tracks; el comentario fijado
    con el tracklist completo parsea decenas. Se exige :data:`MIN_TRACKS`
    para no confundir un comentario con dos guiones casuales con un
    tracklist real.
    """
    best, count = _best_tracklist(comments)
    return best if count >= MIN_TRACKS else None


def _best_tracklist(texts: list[str]) -> tuple[str | None, int]:
    """El texto con más tracks parseables y su conteo. ``(None, 0)`` si
    la lista está vacía o ninguno parsea nada."""
    best: str | None = None
    best_count = 0
    for text in texts:
        count = len(parse_tracklist(text))
        if count > best_count:
            best_count = count
            best = text
    return best, best_count


# --------------------------------------------------------------------------- #
# Fetch principal
# --------------------------------------------------------------------------- #

def fetch_tracklist_text(
    url: str,
    *,
    youtube_api_key: str | None = None,
) -> SourceResult:
    """Obtiene el texto del tracklist desde una URL de YouTube o Mixcloud.

    Lanza :class:`SourceError` con mensaje accionable si la fuente no está
    soportada, falta la key de YouTube, el recurso no existe, o ninguna
    parte del contenido tiene un tracklist parseable.
    """
    source = detect_source(url)

    if source == "youtube":
        return _fetch_youtube(url, youtube_api_key)
    if source == "mixcloud":
        return _fetch_mixcloud(url)

    host = _hostname(url)
    if host == "soundcloud.com" or host.endswith(".soundcloud.com"):
        raise SourceError(
            "SoundCloud no tiene API pública (cerrada a registros nuevos "
            "desde hace años). Abrí el set, copiá la descripción y pegá "
            "el texto directamente."
        )
    raise SourceError(
        f"URL no soportada: {url}\n"
        "Fuentes soportadas: YouTube y Mixcloud. Para cualquier otra, "
        "pegá el texto del tracklist directamente."
    )


# --------------------------------------------------------------------------- #
# YouTube (Data API v3, key gratuita)
# --------------------------------------------------------------------------- #

def _fetch_youtube(url: str, api_key: str | None) -> SourceResult:
    video_id = extract_youtube_id(url)
    if video_id is None:
        raise SourceError(
            f"No pude extraer el ID de video de: {url}\n"
            "Formatos válidos: youtube.com/watch?v=..., youtu.be/..., "
            "youtube.com/live/..., youtube.com/shorts/..."
        )
    if not api_key:
        raise SourceError(
            "Para leer YouTube hace falta una API key gratuita de Google "
            "Cloud (10.000 consultas/día sin costo):\n"
            "  1. https://console.cloud.google.com → crear proyecto\n"
            "  2. Habilitar 'YouTube Data API v3' y crear una API key\n"
            "  3. export YOUTUBE_API_KEY=<tu-key>\n"
            "Alternativa sin key: abrí el video, copiá la descripción y "
            "pegá el texto directamente."
        )

    # 1) Descripción del video (videos.list, 1 unidad de cuota).
    data = http_get_json(
        "https://www.googleapis.com/youtube/v3/videos?"
        + urllib.parse.urlencode({
            "part": "snippet",
            "id": video_id,
            "key": api_key,
        })
    )
    items = data.get("items") or []
    if not items:
        raise SourceError(
            f"YouTube no encontró el video {video_id} — ¿es privado o "
            "fue eliminado?"
        )
    snippet = items[0].get("snippet") or {}
    title = str(snippet.get("title", ""))
    description = str(snippet.get("description", ""))
    desc_count = len(parse_tracklist(description))

    # 2) Comentarios (commentThreads.list, 1 unidad). SIEMPRE se consultan
    # y gana el texto con más tracks: una descripción con un tracklist
    # parcial (o con líneas que el parser no logró filtrar) no debe tapar
    # el comentario fijado con el tracklist completo. El empate lo gana la
    # descripción (es la palabra del autor del video). Si los comentarios
    # están deshabilitados la API da 403 — no fatal, llega lista vacía.
    comments = _fetch_youtube_comments(video_id, api_key)
    best_comment, comment_count = _best_tracklist(comments)

    if desc_count >= MIN_TRACKS and desc_count >= comment_count:
        return SourceResult(
            text=description, origin="youtube:descripcion", title=title,
        )
    if best_comment is not None and comment_count >= MIN_TRACKS:
        return SourceResult(
            text=best_comment, origin="youtube:comentario", title=title,
        )

    raise SourceError(
        f"El video «{title or video_id}» no tiene un tracklist parseable "
        f"ni en la descripción ni en los {len(comments)} comentarios más "
        "relevantes. Si el tracklist está en otro lado (web del artista, "
        "foro), copialo y pegá el texto directamente."
    )


def _fetch_youtube_comments(video_id: str, api_key: str) -> list[str]:
    """Top comentarios del video como texto plano. Lista vacía si los
    comentarios están deshabilitados o la consulta falla — el caller trata
    "sin comentarios" y "sin tracklist en comentarios" igual.

    ``textOriginal`` solo está garantizado para el autor autenticado según
    la doc; con API key suele venir igual, pero el fallback a
    ``textDisplay`` (que con ``textFormat=plainText`` también es texto
    plano) cubre el caso contrario.
    """
    try:
        data = http_get_json(
            "https://www.googleapis.com/youtube/v3/commentThreads?"
            + urllib.parse.urlencode({
                "part": "snippet",
                "videoId": video_id,
                "order": "relevance",
                "maxResults": str(_MAX_COMMENTS),
                "textFormat": "plainText",
                "key": api_key,
            })
        )
    except SourceError:
        return []

    comments: list[str] = []
    for item in data.get("items") or []:
        top = (
            (item.get("snippet") or {})
            .get("topLevelComment", {})
            .get("snippet", {})
        )
        text = str(top.get("textOriginal") or top.get("textDisplay") or "")
        if text:
            comments.append(text)
    return comments


# --------------------------------------------------------------------------- #
# Mixcloud (API pública, sin key)
# --------------------------------------------------------------------------- #

def _fetch_mixcloud(url: str) -> SourceResult:
    path = extract_mixcloud_path(url)
    if path is None:
        raise SourceError(
            f"No parece la URL de un cloudcast: {url}\n"
            "Formato esperado: mixcloud.com/<usuario>/<show>/"
        )

    # quote con safe="/%": re-encodea Unicode crudo (paths copiados de
    # Firefox/Safari vienen %-decodeados y http.client crashea con
    # request lines no-ASCII) SIN doble-encodear lo que ya viene como %XX.
    quoted = urllib.parse.quote(path, safe="/%")
    data = http_get_json(f"https://api.mixcloud.com/{quoted}/")
    title = str(data.get("name", ""))

    # sections: tracklist estructurado. Mixcloud lo entrega vacío desde
    # hace años (licencias), pero si alguna vez viene poblado es la mejor
    # fuente posible — texto sintético "artist - title" por línea.
    sections_text = _mixcloud_sections_to_text(data.get("sections"))
    if sections_text and len(parse_tracklist(sections_text)) >= MIN_TRACKS:
        return SourceResult(
            text=sections_text, origin="mixcloud:sections", title=title,
        )

    description = str(data.get("description", ""))
    if len(parse_tracklist(description)) >= MIN_TRACKS:
        return SourceResult(
            text=description, origin="mixcloud:descripcion", title=title,
        )

    raise SourceError(
        f"El show «{title or path}» no tiene un tracklist parseable en su "
        "descripción (Mixcloud oculta los tracklists estructurados por "
        "licencias). Si lo tenés de otra fuente, pegá el texto directamente."
    )


def _mixcloud_sections_to_text(sections: object) -> str:
    """Convierte las ``sections`` de Mixcloud a líneas ``artist - title``.

    Forma histórica de cada entry: ``{"track": {"name": ..., "artist":
    {"name": ...}}}``. Como Mixcloud entrega este campo vacío hace años,
    el shape real de un ``sections`` poblado es una suposición — por eso
    CADA nivel se valida con ``isinstance`` (entry no-dict, ``track``
    string, ``artist`` string, ``name: null``, o ``sections`` que no es
    lista: todo se salta sin crashear, nunca llega un ``AttributeError``
    al usuario).
    """
    if not isinstance(sections, list):
        return ""
    lines: list[str] = []
    for entry in sections:
        if not isinstance(entry, dict):
            continue
        track = entry.get("track")
        if not isinstance(track, dict):
            continue
        name = track.get("name")
        artist_obj = track.get("artist")
        artist_name = (
            artist_obj.get("name") if isinstance(artist_obj, dict) else None
        )
        title = name.strip() if isinstance(name, str) else ""
        artist = artist_name.strip() if isinstance(artist_name, str) else ""
        if artist and title:
            lines.append(f"{artist} - {title}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# HTTP (único punto de red — monkeypatcheable en tests)
# --------------------------------------------------------------------------- #

def http_get_json(url: str) -> dict[str, Any]:
    """GET + parseo JSON con timeout. Traduce TODO fallo de red/HTTP/parseo
    a :class:`SourceError` legible (extrayendo el mensaje de error de la
    API de Google cuando viene en el body).

    El orden de los except importa: ``HTTPError ⊂ URLError ⊂ OSError``.
    El catch ancho final cubre lo que ``urllib`` NO envuelve en
    ``URLError`` — verificado empíricamente: ``RemoteDisconnected`` e
    ``IncompleteRead`` (``http.client.HTTPException``) salen crudos de
    ``getresponse()``/``read()``, y un body no-UTF-8 da
    ``UnicodeDecodeError`` (``ValueError``, hermana de
    ``json.JSONDecodeError``). ``TimeoutError`` es ``OSError``.
    """
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_S) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = _extract_api_error(e)
        raise SourceError(
            f"La API respondió {e.code}{': ' + detail if detail else ''}"
        ) from e
    except urllib.error.URLError as e:
        raise SourceError(f"Error de red consultando la fuente: {e.reason}") from e
    except (OSError, http.client.HTTPException, ValueError) as e:
        raise SourceError(f"Respuesta inválida de la fuente: {e}") from e

    # Un 200 con body JSON válido pero no-objeto (lista, null, número)
    # rompería los .get() de los callers con AttributeError.
    if not isinstance(payload, dict):
        raise SourceError(
            "Respuesta inválida de la fuente: se esperaba un objeto JSON, "
            f"llegó {type(payload).__name__}"
        )
    return payload


def _extract_api_error(error: urllib.error.HTTPError) -> str:
    """Saca el mensaje legible del body de error de la API de Google
    (``{"error": {"message": ...}}``). String vacío si no se puede.
    """
    try:
        body = json.loads(error.read().decode("utf-8"))
        return str(body.get("error", {}).get("message", ""))
    except Exception:  # noqa: BLE001 — best effort: el code HTTP ya se reporta
        return ""


def _parse_url(url: str) -> urllib.parse.ParseResult:
    """``urlparse`` tolerante: completa el esquema si falta.

    - ``youtube.com/watch?v=x`` → ``https://youtube.com/watch?v=x``
      (sin esto el host caería en ``path``).
    - ``//www.youtube.com/...`` (scheme-relative, lo que se copia de un
      ``src=`` HTML) → ``https://www.youtube.com/...``.
    """
    candidate = url.strip()
    if candidate.startswith("//"):
        candidate = "https:" + candidate
    elif "://" not in candidate:
        candidate = "https://" + candidate
    return urllib.parse.urlparse(candidate)


def _hostname(url: str) -> str:
    """Hostname de una URL: lowercaseado, sin puerto ni userinfo. String
    vacío si no se puede parsear."""
    return _parse_url(url).hostname or ""
