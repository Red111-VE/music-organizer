"""Parser de tracklists pegados: texto sucio → tracks estructurados.

Primer componente del resolver de tracklists (v0.3.x). Convierte el texto
que un DJ pega (descripción de YouTube, comentario, foro, mensaje) en una
lista de :class:`ParsedTrack` lista para resolver contra catálogos.

Formatos tolerados por línea (combinables entre sí):

- Numeración:      ``1. Artist - Title`` / ``01) ...`` / ``3: ...``
- Timestamps:      ``[00:00] Artist - Title`` / ``1:23:45 ...`` / al final
- Prefijo "w/":    ``w/ Artist - Title`` (track mezclado encima — cuenta igual)
- Sello al final:  ``Artist - Title [PIV]`` (se elimina; no ayuda al matching)
- Remix:           ``Artist - Title (Someone Remix)`` → campo ``remix``.
  ``(Original Mix)`` se descarta — es la convención de Beatport y NO aparece
  en Deezer/iTunes; dejarla rompería el matching.
- IDs:             ``ID - ID`` / ``Artist - ID`` / ``ID`` solo → ``is_id=True``
- Separadores:     `` - `` (hyphen), `` – `` (en dash), `` — `` (em dash).
  Siempre con espacios alrededor: ``AC-DC - Back in Black`` no se parte mal.

Líneas sin separador artista-título que tampoco son un ID se consideran
ruido (headers tipo "TRACKLIST:", líneas de adorno) y se descartan.

También se descarta el **ruido social**: líneas cuyo artista o título
contiene una URL o un email (``Instagram - https://...``, ``Booking -
dj@agency.com``). Son el patrón universal de las descripciones de YouTube
y, como tienen `` - ``, sin este filtro contarían como tracks — lo que
rompería el rol de *detector* de este parser (3 links sociales parecerían
un tracklist válido). Contrapartida asumida: un track con link de descarga
en la misma línea (``Artist - Title (DL: https://...)``) también se
descarta — ese patrón es muchísimo más raro que los links sociales.

Este parser cumple doble función: además de parsear el input directo del
usuario, sirve como *detector* de tracklists — :mod:`core.sources` lo usa
para elegir, entre los comentarios de un video de YouTube, el que más
líneas de track parseables contiene.

**Sin IA**: parser determinístico puro (regex + heurísticas). Decisión de
diseño v1 — costo cero, sin API keys. Si en el futuro se añade
normalización LLM (BYOK), entraría como pre-procesador opcional ANTES de
este parser, nunca en reemplazo.

Lógica pura de strings: sin red, sin filesystem, sin dependencias. Importar
este módulo es gratis (mismo principio que :mod:`core.enricher`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class ParsedTrack:
    """Una línea de tracklist ya estructurada, lista para el resolver.

    ``raw`` conserva la línea original (va al CSV de reporte para que el
    usuario pueda auditar el parseo). ``is_id`` marca tracks no
    identificados — el resolver los salta y van directo al tracker de
    unreleased.
    """
    raw: str
    line_no: int
    artist: str = ""
    title: str = ""
    remix: str = ""
    is_id: bool = False

    @property
    def query(self) -> str:
        """String de búsqueda para los catálogos: ``artist title [remix]``.

        El remix se incluye porque distingue versiones ("Sway (LiTek
        Remix)" vs "Sway (Mason Collective Remix)") — son tracks distintos
        para un DJ.
        """
        parts = [self.artist, self.title]
        if self.remix:
            parts.append(self.remix)
        return " ".join(p for p in parts if p)


# --------------------------------------------------------------------------- #
# Regexes de limpieza
# --------------------------------------------------------------------------- #

# Timestamp al inicio: [00:00], (1:23:45), 02:34 — con o sin corchetes/
# paréntesis, seguido opcionalmente de un separador suelto.
_RE_TS_PREFIX = re.compile(
    r"^[\[\(]?\d{1,2}:\d{2}(?::\d{2})?[\]\)]?\s*[-–—.]?\s*"
)

# Timestamp al final: "Artist - Title 1:23:45" (menos común pero existe).
_RE_TS_SUFFIX = re.compile(r"\s+[\[\(]?\d{1,2}:\d{2}(?::\d{2})?[\]\)]?\s*$")

# Numeración al inicio: "1.", "01)", "3:", "12 -". El separador tras el
# número es obligatorio — un artista que empieza con dígitos ("24K") no
# debe perder su nombre.
_RE_TRACKNUM = re.compile(r"^\d{1,3}\s*[.):\-]\s+")

# Prefijo "w/" — track mezclado encima de otro. Se cuenta como track normal.
_RE_WITH = re.compile(r"^w/\s*", re.IGNORECASE)

# Sello discográfico al final: "[PIV]", "[Hot Creations]". Se elimina:
# mete ruido en la búsqueda y los catálogos no indexan por sello.
_RE_LABEL_SUFFIX = re.compile(r"\s*\[[^\]]*\]\s*$")

# Paréntesis de remix AL FINAL del título. La keyword interna es lo que lo
# distingue de un paréntesis cualquiera — "(feat. X)" NO es remix y se queda
# en el título (Deezer/iTunes suelen incluir el feat en el title).
_RE_REMIX_PAREN = re.compile(
    r"\s*\(([^)]*\b(?:remix|mix|edit|dub|version|bootleg|rework|"
    r"refix|flip|vip|remake)\b[^)]*)\)\s*$",
    re.IGNORECASE,
)

# "(Original Mix)" es ruido de nomenclatura Beatport: no es una versión
# distinta y casi nunca aparece así en Deezer/iTunes.
_RE_ORIGINAL_MIX = re.compile(r"^original\s+mix$", re.IGNORECASE)

_RE_MULTISPACE = re.compile(r"\s+")

# Ruido social: URLs y emails. Una línea de track real nunca los tiene en
# artista/título; las descripciones de YouTube están llenas de
# "Instagram - https://..." que sin este filtro parsean como tracks.
# El email exige no-espacios alrededor de la @ — "Live @ Printworks"
# (un título real) no matchea.
_RE_NOISE = re.compile(r"https?://|www\.|\S+@\S+\.\S+", re.IGNORECASE)

# Separadores artista-título reconocidos, SIEMPRE con espacios alrededor.
_SEPARATORS = (" - ", " – ", " — ")

# Tokens que significan "track no identificado".
_ID_TOKENS = frozenset({"id", "???", "unknown", "unreleased", "untitled"})


# --------------------------------------------------------------------------- #
# API pública
# --------------------------------------------------------------------------- #

def parse_tracklist(text: str) -> list[ParsedTrack]:
    """Parsea un tracklist pegado completo. Devuelve solo las líneas que
    son tracks (headers, separadores de adorno y líneas vacías se
    descartan en silencio).

    ``line_no`` es 1-based sobre el texto original — permite rastrear cada
    track parseado a su línea fuente.
    """
    tracks: list[ParsedTrack] = []
    for i, line in enumerate(text.splitlines(), start=1):
        track = parse_line(line, line_no=i)
        if track is not None:
            tracks.append(track)
    return tracks


def parse_line(line: str, line_no: int = 0) -> ParsedTrack | None:
    """Parsea una línea. Devuelve ``None`` si la línea no es un track.

    Una línea cuenta como track si, después de limpiar numeración y
    timestamps, tiene un separador `` - `` artista-título, o si es un
    ID reconocible (``ID``, ``ID - ID``, ``???``).
    """
    raw = line.strip()
    if not raw:
        return None

    cleaned = _strip_prefixes(raw)
    cleaned = _RE_TS_SUFFIX.sub("", cleaned).strip()
    if not cleaned:
        return None

    artist, title = _split_artist_title(cleaned)

    if title is None:
        # Sin separador. ¿Es un "ID" solo en la línea?
        if cleaned.lower() in _ID_TOKENS:
            return ParsedTrack(
                raw=raw, line_no=line_no, artist="", title="ID", is_id=True,
            )
        return None  # header / adorno / ruido

    # Limpiar el título: sello al final primero, después remix.
    title = _RE_LABEL_SUFFIX.sub("", title).strip()
    remix = ""
    match = _RE_REMIX_PAREN.search(title)
    if match:
        candidate = match.group(1).strip()
        title = _RE_REMIX_PAREN.sub("", title).strip()
        if not _RE_ORIGINAL_MIX.match(candidate):
            remix = candidate

    artist = _RE_MULTISPACE.sub(" ", artist).strip()
    title = _RE_MULTISPACE.sub(" ", title).strip()

    # Ruido social: "Instagram - https://...", "Booking - dj@x.com".
    # No son tracks aunque tengan separador.
    if _RE_NOISE.search(artist) or _RE_NOISE.search(title):
        return None

    is_id = artist.lower() in _ID_TOKENS or title.lower() in _ID_TOKENS

    return ParsedTrack(
        raw=raw,
        line_no=line_no,
        artist=artist,
        title=title,
        remix=remix,
        is_id=is_id,
    )


# --------------------------------------------------------------------------- #
# Helpers internos
# --------------------------------------------------------------------------- #

def _strip_prefixes(s: str) -> str:
    """Quita numeración, timestamps y "w/" del inicio, en cualquier orden.

    Loop hasta estabilizar: cubre combinaciones tipo ``01. [00:00] w/ A - B``
    sin asumir un orden fijo entre los tres.
    """
    prev = None
    while prev != s:
        prev = s
        s = _RE_TRACKNUM.sub("", s)
        s = _RE_TS_PREFIX.sub("", s)
        s = _RE_WITH.sub("", s)
        s = s.lstrip()
    return s


def _split_artist_title(s: str) -> tuple[str, str | None]:
    """Parte ``artist - title`` por el PRIMER separador con espacios.

    Devuelve ``(artist, title)`` o ``(s, None)`` si no hay separador.
    Partir por el primero (no el último) asume la convención universal
    "artista primero"; un título con guiones internos queda intacto:
    ``A - B - C`` → ``("A", "B - C")``.
    """
    best_idx: int | None = None
    best_sep = ""
    for sep in _SEPARATORS:
        idx = s.find(sep)
        if idx != -1 and (best_idx is None or idx < best_idx):
            best_idx = idx
            best_sep = sep
    if best_idx is None:
        return s, None
    return s[:best_idx].strip(), s[best_idx + len(best_sep):].strip()
