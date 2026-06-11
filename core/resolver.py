"""Resolución de tracks contra catálogos públicos: Deezer, iTunes, YouTube.

Tercer componente del resolver de tracklists (v0.3.x). Toma los
:class:`core.tracklist.ParsedTrack` del parser y busca cada uno en
catálogos, devolviendo el mejor match con un nivel de confianza.

Cascada de proveedores (en orden, cortando temprano si hay match confiable):

1. **Deezer** — API pública sin key, rate generoso (50 req/5 s). Primera
   opción siempre. Sin remix: query avanzada (``artist:"X" track:"Y"`` —
   precisión quirúrgica, verificado empíricamente) con fallback a la
   plana. Con remix: query plana con remix y fallback a ``artist title``
   a secas — el buscador de Deezer se ahoga con strings largos
   artista-completo + remix (verificado: los remixes de Innerbloom no
   aparecían con la query completa pero sí sin el remix; el scorer
   discrimina la versión correcta entre los resultados).
2. **iTunes Search** — API pública sin key, pero rate recomendado de solo
   ~20 req/min: por eso es *fallback*, no primera opción. Solo se consulta
   cuando Deezer no dio un match confiable.
3. **YouTube** (si hay API key) — ``search.list`` cuesta **100 unidades**
   (vs 1 de las consultas de :mod:`core.sources`): con la cuota gratuita
   diaria de 10.000 alcanza para ~100 búsquedas. Por eso es el último
   recurso y su confianza se capea (metadata de videos no curada — el
   título lo escribe quien sube).

Para todo track no-ID se construye además ``youtube_search_url`` — un link
de búsqueda de YouTube que no cuesta nada ni necesita key. Es el "a mano"
final: aunque ningún catálogo lo tenga, un click lleva a buscarlo.

**Scoring** (:func:`score_candidate`, 0–100), calibrado contra las APIs
reales (QA de 23 resoluciones sobre tracks reales de house/tech-house):

- Artista por token set — tolera artistas extra ("Locklead & Doppelgang"
  matchea "Locklead").
- Título por token sort sobre los títulos **base de ambos lados** (los
  paréntesis de feat/subtítulos no penalizan: "Buggin' (feat. Jem Cooke)"
  matchea "Buggin'" a 100), con fallback al máximo contra los títulos
  completos — recupera el formato de exports "Title Artist Remix" sin
  paréntesis. token sort (no token set): el subset-scoring haría que
  "Sway" puntúe 100 contra "Sway (LiTek Remix)", verificado.
- El remix se compara por su **núcleo** (sin palabras genéricas: "LiTek
  Remix" → "litek") contra los paréntesis del candidato. Un candidato sin
  marcador de versión ES el original — no puede ser el remix pedido. La
  presencia del núcleo solo *evita* la penalización; no infla el score
  (inflar tapaba títulos base distintos del mismo remixer, verificado).
- Versiones degradantes no pedidas ("[Mixed]" de compilados, "Live") se
  penalizan suave: a igual score, el master de estudio gana.
- Piso de título: artista 100% no alcanza el estado ``ok`` con un título
  vagamente parecido ("Home" no debe dar ok con "Domine").

Limitación conocida (documentada, no resuelta): dos títulos base a un solo
carácter ("Stay"/"Sway") con el mismo remixer puntúan ~86 — distinguirlos
mataría la tolerancia a typos del tracklist, que es más frecuente.

Umbrales: ``>= OK_THRESHOLD`` → estado ``ok``; ``>= WEAK_THRESHOLD`` →
``dudoso`` (se muestra el candidato, marcado); debajo → ``no-encontrado``.
Los tracks ``is_id`` no gastan consultas: estado ``id`` directo.

Red vía :func:`core.sources.http_get_json` (compartido). Los fallos de un
proveedor NO abortan el track: se anotan en ``notes`` y la cascada sigue.
Todos los campos de payloads se validan con ``isinstance`` (mismo estándar
que ``_mixcloud_sections_to_text``): un proxy que devuelva HTML como 200 o
un cambio de shape del API no puede tirar la resolución del tracklist.
``rapidfuzz`` se importa lazy (patrón del proyecto).
"""

from __future__ import annotations

import re
import time
import unicodedata
import urllib.parse
from dataclasses import dataclass, field

from core.sources import SourceError, http_get_json
from core.tracklist import ParsedTrack


# Umbrales de confianza (escala 0-100 de rapidfuzz).
OK_THRESHOLD = 82.0
WEAK_THRESHOLD = 60.0

# La metadata de YouTube no es curada (el título lo escribe el uploader);
# un match "perfecto" ahí no vale lo mismo que uno de Deezer. El cap deja
# que un buen match siga alcanzando `ok` (88 > 82) pero nunca 100.
_YOUTUBE_SCORE_CAP = 88.0

# Piso de similitud de título: por debajo, el score total se capea bajo el
# umbral de ok aunque el artista dé 100 — un catálogo entero del artista
# correcto no convierte "Home" en "Domine".
_TITLE_FLOOR = 65.0
_TITLE_FLOOR_CAP = 78.0

# Máximo de candidatos a puntuar por proveedor.
_MAX_CANDIDATES = 8

# Intervalo mínimo entre llamadas por proveedor (rate limits publicados:
# Deezer 50 req/5s; iTunes ~20 req/min recomendado). Dict mutable para que
# los tests lo puedan poner en 0.
_INTERVALS: dict[str, float] = {
    "deezer": 0.15,
    "itunes": 3.1,
    "youtube": 0.2,
}
_last_call: dict[str, float] = {}

# Palabras genéricas de versión: se quitan del remix para quedarse con su
# núcleo identificador ("LiTek Remix" → "litek"; "Extended Mix" → "").
# "live" NO está: pedir una "Live Version" es pedir esa grabación
# específica, el núcleo debe conservarla.
_GENERIC_VERSION_WORDS = frozenset({
    "remix", "mix", "edit", "dub", "version", "bootleg", "rework",
    "refix", "flip", "vip", "remake", "extended", "original", "club",
    "radio", "instrumental",
})

# Keywords que marcan un remix "estricto" (de un tercero): un candidato
# con esto en el título NO es el track base. "Extended Mix"/"Club Mix"
# no están — son versiones del mismo track, aceptables para un DJ.
_STRICT_REMIX_WORDS = frozenset({
    "remix", "bootleg", "vip", "rework", "refix", "flip", "remake", "edit",
})

# Versiones que degradan el resultado si el usuario no las pidió: cortes
# de compilado mezclado ("[Mixed]", típico de iTunes) y grabaciones en
# vivo. ×0.85: a igual match, el master de estudio gana (verificado con
# "Âme - Rej", donde la Live Version empataba 100 con el original).
_DEGRADED_VERSION_WORDS = frozenset({"mixed", "live"})

# Paréntesis/corchetes del título de un candidato.
_RE_PARENS = re.compile(r"[(\[]([^)\]]*)[)\]]")

_RE_NON_ALNUM = re.compile(r"[^a-z0-9]+")

# Comillas que rompen la query avanzada de Deezer — incluye las
# tipográficas que aparecen al copiar texto de blogs/descripciones.
_RE_QUOTES = re.compile(r"[\"“”‘’]")

# Letras que NFKD NO descompone (no son base+diacrítico). Sin esto,
# "Trentemøller" normaliza a "trentem ller" y la grafía ASCII
# "Trentemoller" — la que escribe la mayoría de los tracklists — no
# matchea (verificado: 58.3 de similitud entre ambas).
_TRANSLIT = str.maketrans({
    "ø": "o", "Ø": "O", "đ": "d", "Đ": "D", "ł": "l", "Ł": "L",
    "æ": "ae", "Æ": "AE", "œ": "oe", "Œ": "OE", "ß": "ss",
    "ð": "d", "Ð": "D", "þ": "th", "Þ": "TH",
})


@dataclass
class Candidate:
    """Un resultado de catálogo ya puntuado contra el track buscado."""
    provider: str       # "deezer" | "itunes" | "youtube"
    artist: str
    title: str
    url: str
    score: float


@dataclass
class TrackResolution:
    """Resultado de resolver un track del tracklist.

    ``status``: ``ok`` (match confiable) / ``dudoso`` (hay candidato pero
    no alcanza el umbral) / ``no-encontrado`` / ``id`` (track no
    identificado en el tracklist original — no se busca).

    ``best`` se conserva aunque el estado sea ``no-encontrado`` (si hubo
    algún candidato): el CSV muestra "lo más cercano fue X (score N)" y el
    usuario decide. ``notes`` acumula fallos de proveedor (red caída,
    cuota agotada) — el reporte los muestra para que "no-encontrado por
    error de red" no se confunda con "no existe en catálogos".
    """
    track: ParsedTrack
    status: str
    best: Candidate | None
    youtube_search_url: str
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Normalización y scoring (lógica pura — testeable sin red)
# --------------------------------------------------------------------------- #

def normalize(s: str) -> str:
    """Normaliza para comparación difusa: translitera lo que NFKD no
    descompone (ø→o, æ→ae, ß→ss), quita acentos, minúsculas, sin
    puntuación, espacios colapsados. "Trentemøller (Live)" →
    "trentemoller live"."""
    decomposed = unicodedata.normalize("NFKD", s.translate(_TRANSLIT))
    ascii_only = "".join(c for c in decomposed if not unicodedata.combining(c))
    return _RE_NON_ALNUM.sub(" ", ascii_only.casefold()).strip()


def _split_parens(title: str) -> tuple[str, str]:
    """Separa un título en ``(base, extras)`` donde ``extras`` es el
    contenido concatenado de todos sus paréntesis/corchetes.
    "Sway (LiTek Remix) [Mixed]" → ("Sway", "LiTek Remix Mixed").
    Funciona con paréntesis en cualquier posición:
    "(It Goes Like) Nanana" → ("Nanana", "It Goes Like")."""
    extras = " ".join(m.group(1) for m in _RE_PARENS.finditer(title))
    base = _RE_PARENS.sub(" ", title)
    return base.strip(), extras.strip()


def _remix_core(remix: str) -> str:
    """Núcleo identificador del remix: sin palabras genéricas de versión.
    "LiTek Remix" → "litek"; "Extended Mix" → "" (genérico puro)."""
    words = [
        w for w in normalize(remix).split()
        if w not in _GENERIC_VERSION_WORDS
    ]
    return " ".join(words)


def _has_strict_remix(extras: str) -> bool:
    """¿Los extras del candidato nombran un remix de un tercero?"""
    return any(w in _STRICT_REMIX_WORDS for w in normalize(extras).split())


def score_candidate(track: ParsedTrack, cand_artist: str, cand_title: str) -> float:
    """Confianza 0-100 de que el candidato es el track buscado.

    Pesos: 45% artista, 55% título. Reglas (ver docstring del módulo para
    el porqué de cada una — todas calibradas contra casos reales):

    1. Título: base-vs-base (los feat/subtítulos entre paréntesis de
       AMBOS lados no penalizan) con fallback a completo-vs-completo
       (recupera "Title Artist Remix" inline de los exports).
    2. Remix con núcleo pedido: el candidato debe tenerlo en sus
       paréntesis; sin marcador de versión es el original → penalización.
       La presencia solo evita la penalización, nunca infla.
    3. Remix genérico pedido ("Extended Mix") o sin remix: un candidato
       que es remix de un tercero se penaliza — es un derivado, no el
       track. Excepción: si los tokens del remix del candidato ya están
       en el título buscado (remix inline sin paréntesis), no es un
       derivado distinto.
    4. Piso de título: artista perfecto no alcanza ``ok`` con título
       vagamente parecido.
    5. Versiones degradantes no pedidas (Mixed/Live): ×0.85 — a igual
       match gana el master de estudio.
    """
    from rapidfuzz import fuzz  # lazy: patrón del proyecto

    artist_score = fuzz.token_set_ratio(
        normalize(track.artist), normalize(cand_artist),
    )

    track_base, _ = _split_parens(track.title)
    cand_base, cand_extras = _split_parens(cand_title)
    title_score = max(
        fuzz.token_sort_ratio(normalize(track_base), normalize(cand_base)),
        fuzz.token_sort_ratio(normalize(track.title), normalize(cand_title)),
    )

    if track.remix:
        core = _remix_core(track.remix)
        if core:
            if cand_extras:
                # El núcleo se busca en los PARÉNTESIS del candidato (no
                # en el título completo: un núcleo que colisiona con una
                # palabra del título base — remix self-titled — salvaría
                # al original).
                presence = fuzz.token_set_ratio(core, normalize(cand_extras))
            else:
                # Candidato sin paréntesis: o es el original, o es un
                # remix titulado inline ("Innerbloom Lane 8 Remix" — el
                # formato de Deezer, verificado en vivo). El núcleo se
                # busca en los tokens del candidato que NO son del título
                # base; si está, también se compara título+remix
                # combinados contra el candidato completo.
                leftover = (
                    set(normalize(cand_title).split())
                    - set(normalize(track.title).split())
                )
                presence = (
                    fuzz.token_set_ratio(core, " ".join(sorted(leftover)))
                    if leftover else 0.0
                )
                if presence >= 85:
                    title_score = max(title_score, fuzz.token_sort_ratio(
                        normalize(f"{track.title} {track.remix}"),
                        normalize(cand_title),
                    ))
            if presence < 85:
                title_score *= 0.45
        elif _has_strict_remix(cand_extras):
            # Versión genérica pedida: el remix de un tercero sigue
            # siendo el track equivocado.
            title_score *= 0.5
    elif _has_strict_remix(cand_extras):
        extra_tokens = set(normalize(cand_extras).split())
        title_tokens = set(normalize(track.title).split())
        if not extra_tokens <= title_tokens:
            title_score *= 0.5

    score = 0.45 * artist_score + 0.55 * title_score

    if title_score < _TITLE_FLOOR:
        score = min(score, _TITLE_FLOOR_CAP)

    extra_words = set(normalize(cand_extras).split())
    wanted_words = set(normalize(track.remix).split())
    if (extra_words & _DEGRADED_VERSION_WORDS) - wanted_words:
        score *= 0.85

    return round(score, 1)


# --------------------------------------------------------------------------- #
# Throttle por proveedor
# --------------------------------------------------------------------------- #

def _throttle(provider: str) -> None:
    """Espera lo necesario para respetar el intervalo mínimo del proveedor.

    Síncrono a propósito: el resolver corre secuencial en el CLI con una
    barra de progreso. iTunes (~3 s entre llamadas) es el costo de usar
    su API gratuita educadamente — y solo se paga en los tracks que
    Deezer no resolvió.

    Centinela ``None`` (no ``0.0``) para la primera llamada: con default
    0.0, un reloj monotónico que arranque cerca de cero haría dormir a la
    primera llamada sin motivo.
    """
    interval = _INTERVALS.get(provider, 0.0)
    if interval <= 0:
        return
    last = _last_call.get(provider)
    if last is not None:
        elapsed = time.monotonic() - last
        if elapsed < interval:
            time.sleep(interval - elapsed)
    _last_call[provider] = time.monotonic()


# --------------------------------------------------------------------------- #
# Proveedores
# --------------------------------------------------------------------------- #

def _s(value: object) -> str:
    """String del payload o vacío. ``str(None)`` daría el string "None"
    (contaminaría candidatos y CSV); cualquier no-string es vacío."""
    return value if isinstance(value, str) else ""


def _deezer_candidates(track: ParsedTrack, notes: list[str]) -> list[Candidate]:
    """Busca en Deezer. Sin remix: query avanzada (precisión quirúrgica),
    fallback a plana. Con remix: plana con remix, fallback a ``artist
    title`` (el buscador se ahoga con strings largos; el scorer elige la
    versión correcta entre los resultados del fallback).

    Un fallo (SourceError) o una respuesta basura en una query NO corta
    el fallback a la siguiente — perder un candidato recuperable manda el
    track al throttle lento de iTunes sin necesidad.
    """
    queries: list[str] = []
    if track.remix:
        queries.append(track.query)
        queries.append(f"{track.artist} {track.title}".strip())
    else:
        artist = _RE_QUOTES.sub(" ", track.artist).strip()
        title = _RE_QUOTES.sub(" ", track.title).strip()
        if artist and title:
            queries.append(f'artist:"{artist}" track:"{title}"')
        queries.append(track.query)

    for q in queries:
        _throttle("deezer")
        try:
            data = http_get_json(
                "https://api.deezer.com/search?"
                + urllib.parse.urlencode({"q": q})
            )
        except SourceError as e:
            notes.append(f"deezer: {e}")
            continue
        items = data.get("data")
        if not isinstance(items, list):
            continue
        candidates: list[Candidate] = []
        for item in items[:_MAX_CANDIDATES]:
            if not isinstance(item, dict):
                continue
            artist_obj = item.get("artist")
            cand_artist = (
                _s(artist_obj.get("name")) if isinstance(artist_obj, dict) else ""
            )
            cand_title = _s(item.get("title"))
            candidates.append(Candidate(
                provider="deezer",
                artist=cand_artist,
                title=cand_title,
                url=_s(item.get("link")),
                score=score_candidate(track, cand_artist, cand_title),
            ))
        if candidates:
            return candidates
    return []


def _itunes_candidates(track: ParsedTrack, notes: list[str]) -> list[Candidate]:
    """Busca en iTunes Search (entity=song). Solo se llama como fallback —
    su rate recomendado (~20 req/min) lo hace caro en tiempo."""
    _throttle("itunes")
    try:
        data = http_get_json(
            "https://itunes.apple.com/search?"
            + urllib.parse.urlencode({
                "term": track.query,
                "media": "music",
                "entity": "song",
                "limit": str(_MAX_CANDIDATES),
            })
        )
    except SourceError as e:
        notes.append(f"itunes: {e}")
        return []
    results = data.get("results")
    if not isinstance(results, list):
        return []
    candidates: list[Candidate] = []
    for item in results[:_MAX_CANDIDATES]:
        if not isinstance(item, dict):
            continue
        cand_artist = _s(item.get("artistName"))
        cand_title = _s(item.get("trackName"))
        candidates.append(Candidate(
            provider="itunes",
            artist=cand_artist,
            title=cand_title,
            url=_s(item.get("trackViewUrl")),
            score=score_candidate(track, cand_artist, cand_title),
        ))
    return candidates


def _youtube_candidates(
    track: ParsedTrack, api_key: str, notes: list[str],
) -> list[Candidate]:
    """Busca videos en YouTube (search.list — 100 unidades de cuota, por
    eso es el último recurso). El score compara el query completo contra
    el título del video (token set: tolera la basura extra típica de
    títulos de YouTube) y se capea — metadata no curada."""
    from rapidfuzz import fuzz  # lazy

    _throttle("youtube")
    try:
        data = http_get_json(
            "https://www.googleapis.com/youtube/v3/search?"
            + urllib.parse.urlencode({
                "part": "snippet",
                "q": track.query,
                "type": "video",
                "maxResults": "5",
                "key": api_key,
            })
        )
    except SourceError as e:
        notes.append(f"youtube: {e}")
        return []

    items = data.get("items")
    if not isinstance(items, list):
        return []
    out: list[Candidate] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        id_obj = item.get("id")
        video_id = _s(id_obj.get("videoId")) if isinstance(id_obj, dict) else ""
        snippet = item.get("snippet")
        if not isinstance(snippet, dict):
            continue
        video_title = _s(snippet.get("title"))
        if not video_id or not video_title:
            continue
        raw = fuzz.token_set_ratio(normalize(track.query), normalize(video_title))
        out.append(Candidate(
            provider="youtube",
            artist=_s(snippet.get("channelTitle")),
            title=video_title,
            url=f"https://www.youtube.com/watch?v={video_id}",
            score=round(min(float(raw), _YOUTUBE_SCORE_CAP), 1),
        ))
    return out


# --------------------------------------------------------------------------- #
# Resolución
# --------------------------------------------------------------------------- #

def youtube_search_url(query: str) -> str:
    """Link de búsqueda de YouTube — costo cero, sin key, siempre
    disponible. El último "a mano" para tracks que ningún catálogo tiene."""
    return (
        "https://www.youtube.com/results?"
        + urllib.parse.urlencode({"search_query": query})
    )


def resolve_track(
    track: ParsedTrack,
    *,
    youtube_api_key: str | None = None,
) -> TrackResolution:
    """Resuelve un track contra la cascada Deezer → iTunes → YouTube.

    Corta temprano en cuanto un proveedor da un match ``>= OK_THRESHOLD``
    (no gasta consultas de los siguientes). Los tracks ``is_id`` no tocan
    la red. Nunca lanza por fallos de proveedor — quedan en ``notes``.
    """
    if track.is_id:
        # Si al menos el artista se conoce ("Artist - ID"), el link de
        # búsqueda con el artista es mejor que nada. Un "ID - ID" puro
        # tiene artist="ID" — truthy pero inútil como búsqueda.
        artist = track.artist
        useful_artist = bool(artist) and normalize(artist) not in (
            "id", "unknown", "untitled", "unreleased",
        )
        return TrackResolution(
            track=track,
            status="id",
            best=None,
            youtube_search_url=(
                youtube_search_url(artist) if useful_artist else ""
            ),
        )

    notes: list[str] = []
    search_url = youtube_search_url(track.query)

    candidates = _deezer_candidates(track, notes)
    best = _best(candidates)
    if best is not None and best.score >= OK_THRESHOLD:
        return TrackResolution(track, "ok", best, search_url, notes)

    candidates += _itunes_candidates(track, notes)
    best = _best(candidates)
    if best is not None and best.score >= OK_THRESHOLD:
        return TrackResolution(track, "ok", best, search_url, notes)

    if youtube_api_key:
        candidates += _youtube_candidates(track, youtube_api_key, notes)
        best = _best(candidates)
        if best is not None and best.score >= OK_THRESHOLD:
            return TrackResolution(track, "ok", best, search_url, notes)

    if best is not None and best.score >= WEAK_THRESHOLD:
        return TrackResolution(track, "dudoso", best, search_url, notes)
    return TrackResolution(track, "no-encontrado", best, search_url, notes)


def _best(candidates: list[Candidate]) -> Candidate | None:
    """El candidato de mayor score; empate lo gana el primero (los
    proveedores ya devuelven ordenado por su propia relevancia)."""
    if not candidates:
        return None
    return max(candidates, key=lambda c: c.score)
