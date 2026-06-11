"""Tests para :mod:`core.resolver`: scoring, proveedores y cascada.

El scoring usa rapidfuzz REAL (las aserciones son relacionales — "el
candidato correcto puntúa más que el incorrecto", "supera/no supera el
umbral" — no valores exactos, que cambiarían con la versión de rapidfuzz).
Los proveedores y la cascada se prueban monkeypatcheando
``resolver.http_get_json``; el throttle se anula con un fixture autouse
para que la suite no duerma.
"""

from __future__ import annotations

from typing import Any

import pytest

from core import resolver
from core.resolver import (
    OK_THRESHOLD,
    WEAK_THRESHOLD,
    normalize,
    resolve_track,
    score_candidate,
    youtube_search_url,
)
from core.tracklist import ParsedTrack


@pytest.fixture(autouse=True)
def _no_throttle(monkeypatch: pytest.MonkeyPatch) -> None:
    """Anula los intervalos de rate-limit — los tests no duermen."""
    for provider in list(resolver._INTERVALS):
        monkeypatch.setitem(resolver._INTERVALS, provider, 0.0)


def _track(
    artist: str = "Chris Stussy",
    title: str = "All Night Long",
    remix: str = "",
    is_id: bool = False,
) -> ParsedTrack:
    return ParsedTrack(
        raw=f"{artist} - {title}", line_no=1,
        artist=artist, title=title, remix=remix, is_id=is_id,
    )


# --------------------------------------------------------------------------- #
# normalize y helpers puros
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "raw,expected",
    [
        pytest.param("Música Electrónica", "musica electronica", id="acentos"),
        pytest.param("A-Trak", "a trak", id="guion"),
        pytest.param("Sway (LiTek Remix)", "sway litek remix", id="parens"),
        pytest.param("  DOS   espacios  ", "dos espacios", id="espacios"),
        pytest.param("Café & Croissant", "cafe croissant", id="ampersand"),
    ],
)
def test_normalize(raw: str, expected: str) -> None:
    assert normalize(raw) == expected


def test_split_parens() -> None:
    base, extras = resolver._split_parens("Sway (LiTek Remix) [Mixed]")
    assert base == "Sway"
    assert extras == "LiTek Remix Mixed"


@pytest.mark.parametrize(
    "remix,core",
    [
        pytest.param("LiTek Remix", "litek", id="remix-de-tercero"),
        pytest.param("Dompe's Acid Edit", "dompe s acid", id="edit-con-nombre"),
        pytest.param("Extended Mix", "", id="generico-puro"),
        pytest.param("Original Mix", "", id="original"),
        pytest.param("Club Mix", "", id="club"),
    ],
)
def test_remix_core(remix: str, core: str) -> None:
    assert resolver._remix_core(remix) == core


# --------------------------------------------------------------------------- #
# score_candidate — matriz relacional
# --------------------------------------------------------------------------- #

def test_score_match_exacto_es_ok() -> None:
    track = _track()
    score = score_candidate(track, "Chris Stussy", "All Night Long")
    assert score >= OK_THRESHOLD


def test_score_track_distinto_es_bajo() -> None:
    track = _track()
    score = score_candidate(track, "Prunk", "La Manija")
    assert score < WEAK_THRESHOLD


def test_score_artista_subset_tolerado() -> None:
    """"Locklead & Doppelgang" buscado, catálogo lista solo "Locklead"."""
    track = _track(artist="Locklead & Doppelgang", title="Mr. Madder")
    score = score_candidate(track, "Locklead", "Mr. Madder")
    assert score >= OK_THRESHOLD


def test_score_remix_correcto_gana_al_original_y_al_remix_equivocado() -> None:
    """El núcleo del remix discrimina: buscado el LiTek Remix, el match
    correcto debe superar tanto al original a secas como al remix de otro
    (cuya similitud cruda da ~80 por la palabra "remix" compartida)."""
    track = _track(artist="A-Trak", title="Sway", remix="LiTek Remix")

    right = score_candidate(track, "A-Trak", "Sway (LiTek Remix)")
    plain = score_candidate(track, "A-Trak", "Sway")
    wrong = score_candidate(track, "A-Trak", "Sway (Mason Collective Remix)")

    assert right >= OK_THRESHOLD
    assert plain < OK_THRESHOLD
    assert wrong < OK_THRESHOLD
    assert right > plain
    assert right > wrong


def test_score_version_generica_es_neutral() -> None:
    """Query con "(Extended Mix)": el candidato sin sufijo también vale —
    los catálogos lo traen con o sin él."""
    track = _track(title="All Night Long", remix="Extended Mix")
    plain = score_candidate(track, "Chris Stussy", "All Night Long")
    suffixed = score_candidate(track, "Chris Stussy", "All Night Long (Extended Mix)")
    assert plain >= OK_THRESHOLD
    assert suffixed >= OK_THRESHOLD


def test_score_sin_remix_penaliza_remix_de_tercero() -> None:
    """Buscado el original, un "(X Remix)" es un derivado, no el track."""
    track = _track(title="Fame", artist="Cassius")
    original = score_candidate(track, "Cassius", "Fame")
    remix = score_candidate(track, "Cassius", "Fame (Mercury Remix)")
    assert original >= OK_THRESHOLD
    assert remix < OK_THRESHOLD
    assert original > remix


def test_score_sin_remix_no_penaliza_extended() -> None:
    """"(Extended Mix)" no es un remix de tercero: misma versión, más
    larga — para un DJ suele ser incluso preferible."""
    track = _track(title="Fame", artist="Cassius")
    extended = score_candidate(track, "Cassius", "Fame (Extended Mix)")
    assert extended >= OK_THRESHOLD


def test_score_mixed_penalizado_frente_a_standalone() -> None:
    """iTunes devuelve cortes "[Mixed]" de compilados — la versión
    standalone debe rankear arriba."""
    track = _track(artist="Franz Ferdinand", title="Hooked", remix="Ben Sterling Remix")
    standalone = score_candidate(track, "Franz Ferdinand", "Hooked (Ben Sterling Remix)")
    mixed = score_candidate(track, "Franz Ferdinand", "Hooked (Ben Sterling Remix) [Mixed]")
    assert standalone > mixed
    assert standalone >= OK_THRESHOLD


# --------------------------------------------------------------------------- #
# Proveedores (HTTP fake)
# --------------------------------------------------------------------------- #

def _deezer_response(*pairs: tuple[str, str]) -> dict[str, Any]:
    return {
        "data": [
            {
                "title": title,
                "artist": {"name": artist},
                "link": f"https://www.deezer.com/track/{i}",
            }
            for i, (artist, title) in enumerate(pairs)
        ]
    }


def _itunes_response(*pairs: tuple[str, str]) -> dict[str, Any]:
    return {
        "results": [
            {
                "artistName": artist,
                "trackName": title,
                "trackViewUrl": f"https://music.apple.com/track/{i}",
            }
            for i, (artist, title) in enumerate(pairs)
        ]
    }


def _youtube_response(*titles: str) -> dict[str, Any]:
    return {
        "items": [
            {
                "id": {"videoId": f"vid{i:08d}"},
                "snippet": {"title": t, "channelTitle": "Some Channel"},
            }
            for i, t in enumerate(titles)
        ]
    }


def test_deezer_query_avanzada_y_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sin remix: primero query avanzada; si viene vacía, la plana."""
    calls: list[str] = []

    def fake(url: str) -> dict[str, Any]:
        calls.append(url)
        if "artist%3A" in url:          # query avanzada (artist:"...")
            return {"data": []}
        return _deezer_response(("Chris Stussy", "All Night Long"))

    monkeypatch.setattr(resolver, "http_get_json", fake)
    cands = resolver._deezer_candidates(_track(), [])
    assert len(calls) == 2
    assert "artist%3A" in calls[0]
    assert len(cands) == 1
    assert cands[0].provider == "deezer"
    assert cands[0].score >= OK_THRESHOLD


def test_deezer_con_remix_va_directo_a_query_plana(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake(url: str) -> dict[str, Any]:
        calls.append(url)
        return _deezer_response(("A-Trak", "Sway (LiTek Remix)"))

    monkeypatch.setattr(resolver, "http_get_json", fake)
    track = _track(artist="A-Trak", title="Sway", remix="LiTek Remix")
    cands = resolver._deezer_candidates(track, [])
    assert len(calls) == 1
    assert "artist%3A" not in calls[0]
    assert cands[0].score >= OK_THRESHOLD


def test_deezer_error_anota_y_no_crashea(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.sources import SourceError

    def fake(url: str) -> dict[str, Any]:
        raise SourceError("Error de red consultando la fuente: timeout")

    monkeypatch.setattr(resolver, "http_get_json", fake)
    notes: list[str] = []
    cands = resolver._deezer_candidates(_track(), notes)
    assert cands == []
    assert notes and notes[0].startswith("deezer:")


def test_itunes_parsea_candidatos(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        resolver, "http_get_json",
        lambda url: _itunes_response(("Cassius", "Fame")),
    )
    cands = resolver._itunes_candidates(_track(artist="Cassius", title="Fame"), [])
    assert len(cands) == 1
    assert cands[0].provider == "itunes"
    assert cands[0].url.startswith("https://music.apple.com/")


def test_youtube_capea_el_score(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hasta un match perfecto de YouTube queda capeado: metadata no
    curada no vale un 100."""
    monkeypatch.setattr(
        resolver, "http_get_json",
        lambda url: _youtube_response("Chris Stussy - All Night Long"),
    )
    cands = resolver._youtube_candidates(_track(), "KEY", [])
    assert len(cands) == 1
    assert cands[0].score <= resolver._YOUTUBE_SCORE_CAP
    assert cands[0].url == "https://www.youtube.com/watch?v=vid00000000"


def test_youtube_items_sin_video_id_se_saltan(monkeypatch: pytest.MonkeyPatch) -> None:
    """search.list puede devolver canales/playlists si type= fallara —
    items sin videoId no producen candidatos."""
    response = {
        "items": [
            {"id": {"channelId": "ch1"}, "snippet": {"title": "Un canal"}},
            {"id": {"videoId": "vid00000001"}, "snippet": {"title": "Video real"}},
        ]
    }
    monkeypatch.setattr(resolver, "http_get_json", lambda url: response)
    cands = resolver._youtube_candidates(_track(), "KEY", [])
    assert len(cands) == 1
    assert cands[0].title == "Video real"


# --------------------------------------------------------------------------- #
# resolve_track — cascada
# --------------------------------------------------------------------------- #

def test_cascada_corta_en_deezer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Match confiable en Deezer → ni iTunes ni YouTube se consultan."""
    calls: list[str] = []

    def fake(url: str) -> dict[str, Any]:
        calls.append(url)
        assert "deezer.com" in url
        return _deezer_response(("Chris Stussy", "All Night Long"))

    monkeypatch.setattr(resolver, "http_get_json", fake)
    res = resolve_track(_track(), youtube_api_key="KEY")
    assert res.status == "ok"
    assert res.best is not None and res.best.provider == "deezer"
    assert all("deezer.com" in u for u in calls)


def test_cascada_cae_a_itunes(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake(url: str) -> dict[str, Any]:
        if "deezer.com" in url:
            return {"data": []}
        assert "itunes.apple.com" in url
        return _itunes_response(("Chris Stussy", "All Night Long"))

    monkeypatch.setattr(resolver, "http_get_json", fake)
    res = resolve_track(_track())
    assert res.status == "ok"
    assert res.best is not None and res.best.provider == "itunes"


def test_cascada_youtube_solo_con_key(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake(url: str) -> dict[str, Any]:
        calls.append(url)
        if "deezer.com" in url or "itunes.apple.com" in url:
            return {"data": [], "results": []}
        return _youtube_response("Chris Stussy - All Night Long (Full Track)")

    monkeypatch.setattr(resolver, "http_get_json", fake)

    # Sin key: YouTube no se consulta.
    res = resolve_track(_track())
    assert res.status == "no-encontrado"
    assert not any("googleapis.com" in u for u in calls)

    # Con key: se consulta y resuelve.
    res = resolve_track(_track(), youtube_api_key="KEY")
    assert res.best is not None and res.best.provider == "youtube"
    assert res.status == "ok"  # 88 de cap > umbral de 82


def test_no_encontrado_conserva_el_mejor_candidato(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Aunque nada supere WEAK, el mejor candidato se conserva — el CSV
    muestra "lo más cercano fue X" y el usuario decide."""
    def fake(url: str) -> dict[str, Any]:
        if "deezer.com" in url:
            return _deezer_response(("Otro Artista", "Otra Cancion"))
        return {"results": []}

    monkeypatch.setattr(resolver, "http_get_json", fake)
    res = resolve_track(_track())
    assert res.status == "no-encontrado"
    assert res.best is not None
    assert res.best.score < WEAK_THRESHOLD


def test_dudoso_entre_umbrales(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mismo track base sin el remix pedido: candidato visible pero
    marcado dudoso."""
    def fake(url: str) -> dict[str, Any]:
        if "deezer.com" in url:
            return _deezer_response(("A-Trak", "Sway"))  # sin el remix
        return {"results": []}

    monkeypatch.setattr(resolver, "http_get_json", fake)
    track = _track(artist="A-Trak", title="Sway", remix="LiTek Remix")
    res = resolve_track(track)
    assert res.status == "dudoso"
    assert res.best is not None
    assert WEAK_THRESHOLD <= res.best.score < OK_THRESHOLD


def test_id_no_toca_la_red(monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(url: str) -> dict[str, Any]:
        raise AssertionError("un track ID no debe generar consultas")

    monkeypatch.setattr(resolver, "http_get_json", explode)
    res = resolve_track(_track(artist="", title="ID", is_id=True))
    assert res.status == "id"
    assert res.best is None
    assert res.youtube_search_url == ""


def test_id_con_artista_da_link_de_busqueda(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        resolver, "http_get_json",
        lambda url: pytest.fail("sin red para IDs"),
    )
    res = resolve_track(_track(artist="Chris Stussy", title="ID", is_id=True))
    assert res.status == "id"
    assert "Chris+Stussy" in res.youtube_search_url


def test_id_puro_no_genera_link_inutil(monkeypatch: pytest.MonkeyPatch) -> None:
    """REGRESIÓN (e2e real): "ID - ID" tiene artist="ID" — truthy pero
    inútil; generaba search_query=ID en la tabla y el CSV."""
    monkeypatch.setattr(
        resolver, "http_get_json",
        lambda url: pytest.fail("sin red para IDs"),
    )
    res = resolve_track(_track(artist="ID", title="ID", is_id=True))
    assert res.status == "id"
    assert res.youtube_search_url == ""


def test_proveedores_caidos_no_abortan(monkeypatch: pytest.MonkeyPatch) -> None:
    """Todos los proveedores con error de red: no-encontrado + notes con
    el detalle (para distinguirlo de "no existe en catálogos")."""
    from core.sources import SourceError

    def fake(url: str) -> dict[str, Any]:
        raise SourceError("Error de red consultando la fuente: DNS")

    monkeypatch.setattr(resolver, "http_get_json", fake)
    res = resolve_track(_track(), youtube_api_key="KEY")
    assert res.status == "no-encontrado"
    assert res.best is None
    # Deezer anota sus 2 queries (avanzada + fallback plana) + itunes
    # + youtube. El fallback ante error es deliberado: un 500 transitorio
    # en la avanzada no debe regalar el track al throttle de iTunes.
    assert len(res.notes) == 4
    assert sum(1 for n in res.notes if n.startswith("deezer:")) == 2
    assert res.youtube_search_url  # el link manual queda siempre


def test_search_url_siempre_presente() -> None:
    url = youtube_search_url("A-Trak Sway LiTek Remix")
    assert url.startswith("https://www.youtube.com/results?")
    assert "A-Trak+Sway+LiTek+Remix" in url


# --------------------------------------------------------------------------- #
# Regresiones de la verificación adversarial + calibración real
# --------------------------------------------------------------------------- #

def test_score_remix_generico_penaliza_remix_de_tercero() -> None:
    """REGRESIÓN: pedir "(Extended Mix)" anulaba la penalización por remix
    de tercero — "Fame (Mercury Remix)" puntuaba 100 y salía ok."""
    track = _track(title="Fame", artist="Cassius", remix="Extended Mix")
    wrong = score_candidate(track, "Cassius", "Fame (Mercury Remix)")
    right = score_candidate(track, "Cassius", "Fame (Extended Mix)")
    plain = score_candidate(track, "Cassius", "Fame")
    assert wrong < OK_THRESHOLD
    assert right >= OK_THRESHOLD
    assert plain >= OK_THRESHOLD  # versión genérica: el original también vale


def test_score_feat_exacto_es_ok() -> None:
    """REGRESIÓN (calibración real): "Buggin' (feat. Jem Cooke)" contra el
    título EXACTO del catálogo daba 69.4 — los tokens del feat eran
    imposibles de matchear porque solo se partía el lado candidato."""
    track = _track(artist="Hot Since 82", title="Buggin' (feat. Jem Cooke)")
    exact = score_candidate(track, "Hot Since 82", "Buggin' (feat. Jem Cooke)")
    no_feat = score_candidate(track, "Hot Since 82", "Buggin'")
    assert exact >= OK_THRESHOLD
    assert no_feat >= OK_THRESHOLD


def test_score_parens_no_final_es_ok() -> None:
    """REGRESIÓN: "(It Goes Like) Nanana" idéntico daba 71.4."""
    track = _track(artist="Peggy Gou", title="(It Goes Like) Nanana")
    score = score_candidate(track, "Peggy Gou", "(It Goes Like) Nanana")
    assert score >= OK_THRESHOLD


def test_score_presencia_de_remix_no_infla() -> None:
    """REGRESIÓN: el boost de presencia levantaba "Stay (LiTek Remix)" a
    93.1 buscando "Sway (LiTek Remix)". Sin boost baja (~86 — limitación
    documentada: distinguir typos de títulos distintos a 1 carácter
    mataría la tolerancia a typos). El correcto siempre gana."""
    track = _track(artist="A-Trak", title="Sway", remix="LiTek Remix")
    right = score_candidate(track, "A-Trak", "Sway (LiTek Remix)")
    wrong = score_candidate(track, "A-Trak", "Stay (LiTek Remix)")
    assert right > wrong
    assert right >= 95  # el match exacto no se degradó con el cambio


def test_score_nucleo_colisiona_con_titulo_no_salva_al_original() -> None:
    """REGRESIÓN: con remix "Go Remix" (núcleo "go" = palabra del título),
    el original "Go" puntuaba 100 — la presencia se chequeaba contra el
    título completo. Ahora: sin paréntesis no hay versión → es el
    original → penalizado."""
    track = _track(artist="Moby", title="Go", remix="Go Remix")
    original = score_candidate(track, "Moby", "Go")
    right = score_candidate(track, "Moby", "Go (Go Remix)")
    assert original < OK_THRESHOLD
    assert right >= OK_THRESHOLD
    assert right > original


def test_score_live_no_pedida_pierde_contra_estudio() -> None:
    """REGRESIÓN (calibración real): "Âme - Rej" devolvía la Live Version
    (empate 100-100 resuelto por orden de Deezer). La live no pedida se
    degrada; pedirla explícitamente la recupera."""
    track = _track(artist="Ame", title="Rej")
    studio = score_candidate(track, "AME", "Rej")
    live = score_candidate(track, "AME", "Rej (Âme Live Version)")
    assert studio > live
    assert studio >= OK_THRESHOLD

    asked = _track(artist="Ame", title="Rej", remix="Live Version")
    live_wanted = score_candidate(asked, "AME", "Rej (Âme Live Version)")
    studio_unwanted = score_candidate(asked, "AME", "Rej")
    assert live_wanted > studio_unwanted


def test_score_candidato_remix_inline_estilo_deezer_es_ok() -> None:
    """REGRESIÓN (verificado en vivo): Deezer titula remixes inline sin
    paréntesis ("Innerbloom Lane 8 Remix") — el candidato caía penalizado
    como "original sin versión" y el track terminaba rescatado por el
    throttle lento de iTunes."""
    track = _track(artist="RUFUS DU SOL", title="Innerbloom", remix="Lane 8 Remix")
    inline = score_candidate(track, "RÜFÜS", "Innerbloom Lane 8 Remix")
    plain = score_candidate(track, "RÜFÜS DU SOL", "Innerbloom")
    assert inline >= OK_THRESHOLD
    assert plain < OK_THRESHOLD          # el original sigue penalizado
    assert inline > plain


def test_score_remix_inline_sin_parentesis_es_ok() -> None:
    """REGRESIÓN (calibración real): "You & Me Flume Remix" (formato de
    exports, sin paréntesis — el parser no separa el remix) caía a 58.8
    con el track correcto en la mano: doble penalización por tokens
    recortados + strict-remix. Ahora el full-vs-full recupera el match y
    los tokens del remix contenidos en el título anulan la penalización."""
    track = _track(artist="Disclosure", title="You & Me Flume Remix")
    score = score_candidate(track, "Disclosure", "You & Me (Flume Remix)")
    assert score >= OK_THRESHOLD


def test_score_piso_de_titulo() -> None:
    """REGRESIÓN (calibración real): artista 100% arrastraba títulos
    vagamente parecidos cerca de ok ("Home" → "Domine" 78). El piso de
    título impide que crucen el umbral de ok."""
    track = _track(artist="Charlotte de Witte", title="Home")
    score = score_candidate(track, "Charlotte De Witte", "Domine")
    assert score < OK_THRESHOLD


def test_normalize_translitera_lo_que_nfkd_no_descompone() -> None:
    """REGRESIÓN (calibración real): "Trentemoller" (ASCII, como lo
    escriben los tracklists) vs "Trentemøller" daba 58.3 de similitud —
    la ø no se descompone vía NFKD."""
    assert normalize("Trentemøller") == "trentemoller"
    track = _track(artist="Trentemoller", title="Moan")
    score = score_candidate(track, "Trentemøller", "Moan")
    assert score >= OK_THRESHOLD


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"data": [{"artist": "string-no-dict", "title": "X"}]}, id="deezer-artist-string"),
        pytest.param({"data": {"no": "es-lista"}}, id="deezer-data-dict"),
        pytest.param({"data": ["garbage", 42]}, id="deezer-items-no-dict"),
        pytest.param({"data": [{"title": None, "artist": {"name": None}, "link": None}]}, id="deezer-todo-null"),
    ],
)
def test_deezer_shapes_hostiles_no_crashean(
    monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any],
) -> None:
    """REGRESIÓN: 5 shapes hostiles escapaban como AttributeError/TypeError
    de resolve_track, violando su contrato de nunca lanzar."""
    def fake(url: str) -> dict[str, Any]:
        if "deezer.com" in url:
            return payload
        return {"results": []}

    monkeypatch.setattr(resolver, "http_get_json", fake)
    res = resolve_track(_track())  # no debe lanzar
    assert res.status in ("dudoso", "no-encontrado")
    # Campos null nunca producen el string "None" en el candidato.
    if res.best is not None:
        assert res.best.artist != "None"
        assert res.best.title != "None"
        assert res.best.url != "None"


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"results": {"no": "es-lista"}}, id="itunes-results-dict"),
        pytest.param({"results": ["garbage"]}, id="itunes-items-no-dict"),
    ],
)
def test_itunes_shapes_hostiles_no_crashean(
    monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any],
) -> None:
    def fake(url: str) -> dict[str, Any]:
        if "deezer.com" in url:
            return {"data": []}
        return payload

    monkeypatch.setattr(resolver, "http_get_json", fake)
    res = resolve_track(_track())
    assert res.status == "no-encontrado"


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"items": [{"id": "string", "snippet": {"title": "X"}}]}, id="youtube-id-string"),
        pytest.param({"items": [{"id": {"videoId": "v"}, "snippet": "string"}]}, id="youtube-snippet-string"),
        pytest.param({"items": {"no": "es-lista"}}, id="youtube-items-dict"),
    ],
)
def test_youtube_shapes_hostiles_no_crashean(
    monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any],
) -> None:
    def fake(url: str) -> dict[str, Any]:
        if "googleapis.com" in url:
            return payload
        return {"data": [], "results": []}

    monkeypatch.setattr(resolver, "http_get_json", fake)
    res = resolve_track(_track(), youtube_api_key="KEY")
    assert res.status == "no-encontrado"


def test_deezer_error_en_avanzada_no_corta_el_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REGRESIÓN: un SourceError en la query avanzada abortaba Deezer
    entero — la plana, que sí encontraba el track, nunca se intentaba."""
    from core.sources import SourceError

    calls: list[str] = []

    def fake(url: str) -> dict[str, Any]:
        calls.append(url)
        if "artist%3A" in url:
            raise SourceError("La API respondió 500")
        return _deezer_response(("Chris Stussy", "All Night Long"))

    monkeypatch.setattr(resolver, "http_get_json", fake)
    notes: list[str] = []
    cands = resolver._deezer_candidates(_track(), notes)
    assert len(calls) == 2          # avanzada (falló) + plana
    assert len(cands) == 1          # la plana rescató el track
    assert notes and notes[0].startswith("deezer:")


def test_deezer_remix_fallback_sin_remix(monkeypatch: pytest.MonkeyPatch) -> None:
    """REGRESIÓN (calibración real): la query plana con remix devolvía 0
    para los remixes de Innerbloom aunque Deezer los tiene — el fallback
    "artist title" a secas los rescata y el scorer elige la versión."""
    import urllib.parse

    calls: list[str] = []

    def fake(url: str) -> dict[str, Any]:
        calls.append(url)
        if "LiTek" in urllib.parse.unquote_plus(url):
            return {"data": []}     # la query con remix se ahoga
        return _deezer_response(
            ("A-Trak", "Sway"),
            ("A-Trak", "Sway (LiTek Remix)"),
        )

    monkeypatch.setattr(resolver, "http_get_json", fake)
    track = _track(artist="A-Trak", title="Sway", remix="LiTek Remix")
    cands = resolver._deezer_candidates(track, [])
    assert len(calls) == 2
    best = resolver._best(cands)
    assert best is not None
    assert best.title == "Sway (LiTek Remix)"   # el scorer eligió la versión
    assert best.score >= OK_THRESHOLD


def test_deezer_query_avanzada_sanea_comillas_tipograficas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake(url: str) -> dict[str, Any]:
        calls.append(url)
        return _deezer_response(("X", "Y"))

    monkeypatch.setattr(resolver, "http_get_json", fake)
    track = _track(artist="“Artista”", title="‘Titulo’")
    resolver._deezer_candidates(track, [])
    import urllib.parse as up
    decoded = up.unquote_plus(calls[0])
    assert "“" not in decoded and "”" not in decoded
    assert "‘" not in decoded and "’" not in decoded


def test_throttle_primera_llamada_no_duerme(monkeypatch: pytest.MonkeyPatch) -> None:
    """REGRESIÓN (teórica): con default 0.0, un reloj monotónico cerca de
    cero hacía dormir a la primera llamada sin motivo. Centinela None."""
    sleeps: list[float] = []
    fake_now = [0.5]   # monotonic() < interval

    monkeypatch.setitem(resolver._INTERVALS, "deezer", 3.0)
    monkeypatch.setattr(resolver, "_last_call", {})
    monkeypatch.setattr(resolver.time, "monotonic", lambda: fake_now[0])
    monkeypatch.setattr(resolver.time, "sleep", lambda s: sleeps.append(s))

    resolver._throttle("deezer")
    assert sleeps == []             # primera llamada: jamás duerme

    fake_now[0] = 1.5               # 1 s después
    resolver._throttle("deezer")
    assert len(sleeps) == 1
    assert sleeps[0] == pytest.approx(2.0)   # 3.0 - 1.0 transcurrido

    fake_now[0] = 10.0              # mucho después: no duerme
    resolver._throttle("deezer")
    assert len(sleeps) == 1
