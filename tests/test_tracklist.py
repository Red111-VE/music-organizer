"""Tests para :mod:`core.tracklist`: parser de tracklists pegados.

Lógica pura de strings — sin red, sin filesystem. La matriz de casos
cubre los formatos reales que aparecen en descripciones de YouTube,
comentarios fijados y posts de foros: numeración, timestamps, prefijo
"w/", sellos entre corchetes, remixes y todas las variantes de ID.
"""

from __future__ import annotations

import pytest

from core.tracklist import ParsedTrack, parse_line, parse_tracklist


# --------------------------------------------------------------------------- #
# parse_line: matriz artista/título/remix/is_id
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "line,artist,title,remix,is_id",
    [
        # === Básicos ========================================================
        pytest.param(
            "Chris Stussy - All Night Long",
            "Chris Stussy", "All Night Long", "", False,
            id="basico",
        ),
        pytest.param(
            "A-Trak - Sway",
            "A-Trak", "Sway", "", False,
            id="artista-con-guion-interno",
        ),
        pytest.param(
            "Locklead & Doppelgang - Mr. Madder",
            "Locklead & Doppelgang", "Mr. Madder", "", False,
            id="artistas-multiples",
        ),

        # === Numeración =====================================================
        pytest.param(
            "1. Artist - Title",
            "Artist", "Title", "", False,
            id="numero-punto",
        ),
        pytest.param(
            "01) Artist - Title",
            "Artist", "Title", "", False,
            id="numero-parentesis",
        ),
        pytest.param(
            "12: Artist - Title",
            "Artist", "Title", "", False,
            id="numero-dos-puntos",
        ),
        pytest.param(
            "7 - Artist - Title",
            "Artist", "Title", "", False,
            id="numero-guion",
        ),
        pytest.param(
            "24K - Gold",
            "24K", "Gold", "", False,
            id="artista-empieza-con-digitos-no-se-rompe",
        ),

        # === Timestamps =====================================================
        pytest.param(
            "[00:00] Artist - Title",
            "Artist", "Title", "", False,
            id="timestamp-corchetes",
        ),
        pytest.param(
            "02:34 Artist - Title",
            "Artist", "Title", "", False,
            id="timestamp-desnudo",
        ),
        pytest.param(
            "1:23:45 Artist - Title",
            "Artist", "Title", "", False,
            id="timestamp-con-horas",
        ),
        pytest.param(
            "(03:10) Artist - Title",
            "Artist", "Title", "", False,
            id="timestamp-parentesis",
        ),
        pytest.param(
            "Artist - Title 1:23:45",
            "Artist", "Title", "", False,
            id="timestamp-al-final",
        ),
        pytest.param(
            "01. [00:00] Artist - Title",
            "Artist", "Title", "", False,
            id="numero-y-timestamp-combinados",
        ),
        pytest.param(
            "[00:00] 01. Artist - Title",
            "Artist", "Title", "", False,
            id="timestamp-y-numero-orden-inverso",
        ),

        # === Prefijo w/ =====================================================
        pytest.param(
            "w/ Prunk - La Manija",
            "Prunk", "La Manija", "", False,
            id="prefijo-w",
        ),
        pytest.param(
            "02:34 w/ Prunk - La Manija",
            "Prunk", "La Manija", "", False,
            id="timestamp-mas-w",
        ),

        # === Sello al final =================================================
        pytest.param(
            "Chris Stussy - All Night Long [PIV]",
            "Chris Stussy", "All Night Long", "", False,
            id="sello-al-final",
        ),
        pytest.param(
            "Artist - Title [Hot Creations]",
            "Artist", "Title", "", False,
            id="sello-con-espacios",
        ),

        # === Remix ==========================================================
        pytest.param(
            "A-Trak - Sway (LiTek Remix)",
            "A-Trak", "Sway", "LiTek Remix", False,
            id="remix",
        ),
        pytest.param(
            "Artist - Title (Extended Mix)",
            "Artist", "Title", "Extended Mix", False,
            id="extended-mix",
        ),
        pytest.param(
            "Artist - Title (Dompe's Acid Edit)",
            "Artist", "Title", "Dompe's Acid Edit", False,
            id="edit",
        ),
        pytest.param(
            "Artist - Title (Radio Version)",
            "Artist", "Title", "Radio Version", False,
            id="version",
        ),
        pytest.param(
            "Artist - Title (Original Mix)",
            "Artist", "Title", "", False,
            id="original-mix-se-descarta",
        ),
        pytest.param(
            "Artist - Title (X Remix) [Label]",
            "Artist", "Title", "X Remix", False,
            id="remix-y-sello",
        ),
        pytest.param(
            "Artist - Title (feat. MC Flow)",
            "Artist", "Title (feat. MC Flow)", "", False,
            id="feat-se-queda-en-titulo",
        ),

        # === Separadores alternativos ======================================
        pytest.param(
            "Artist – Title",
            "Artist", "Title", "", False,
            id="en-dash",
        ),
        pytest.param(
            "Artist — Title",
            "Artist", "Title", "", False,
            id="em-dash",
        ),
        pytest.param(
            "Artist - Title - Part 2",
            "Artist", "Title - Part 2", "", False,
            id="guion-interno-en-titulo",
        ),

        # === IDs ============================================================
        pytest.param(
            "ID - ID",
            "ID", "ID", "", True,
            id="id-id",
        ),
        pytest.param(
            "03. ID - ID",
            "ID", "ID", "", True,
            id="id-numerado",
        ),
        pytest.param(
            "Artist - ID",
            "Artist", "ID", "", True,
            id="artista-conocido-titulo-id",
        ),
        pytest.param(
            "??? - ???",
            "???", "???", "", True,
            id="interrogantes",
        ),
    ],
)
def test_parse_line(
    line: str, artist: str, title: str, remix: str, is_id: bool,
) -> None:
    track = parse_line(line)
    assert track is not None, f"esperaba track para {line!r}"
    assert track.artist == artist
    assert track.title == title
    assert track.remix == remix
    assert track.is_id == is_id
    assert track.raw == line.strip()


@pytest.mark.parametrize(
    "line",
    [
        pytest.param("", id="vacia"),
        pytest.param("   ", id="solo-espacios"),
        pytest.param("TRACKLIST:", id="header"),
        pytest.param("Tracklist", id="header-sin-puntos"),
        pytest.param("-----", id="separador-adorno"),
        pytest.param("🔥🔥🔥", id="emojis"),
        pytest.param("gracias por escuchar!", id="texto-libre"),
    ],
)
def test_parse_line_descarta_ruido(line: str) -> None:
    assert parse_line(line) is None


@pytest.mark.parametrize(
    "line",
    [
        pytest.param("Instagram - https://instagram.com/djx", id="instagram"),
        pytest.param("Facebook - https://facebook.com/djx", id="facebook"),
        pytest.param("Beatport - https://www.beatport.com/artist/djx", id="beatport"),
        pytest.param("Web - www.djx.com", id="www-sin-esquema"),
        pytest.param("Booking - dj@agency.com", id="email-de-booking"),
        pytest.param("https://linktr.ee/djx - todos mis links", id="url-como-artista"),
    ],
)
def test_parse_line_descarta_ruido_social(line: str) -> None:
    """REGRESIÓN: las líneas de links sociales/emails tienen `` - `` y
    parseaban como tracks — 3 de ellas bastaban para que una descripción
    de YouTube le ganara al comentario con el tracklist real."""
    assert parse_line(line) is None


def test_parse_line_arroba_con_espacios_no_es_email() -> None:
    """Un título real tipo "Live @ Printworks" no debe caer en el filtro
    de emails (la @ con espacios alrededor no matchea)."""
    track = parse_line("Artist - Live @ Printworks")
    assert track is not None
    assert track.title == "Live @ Printworks"


def test_parse_line_id_solo() -> None:
    """Un "ID" solo en la línea es un track no identificado, no ruido."""
    track = parse_line("ID")
    assert track is not None
    assert track.is_id is True
    assert track.title == "ID"
    assert track.artist == ""


# --------------------------------------------------------------------------- #
# parse_tracklist: integración multi-línea
# --------------------------------------------------------------------------- #

def test_parse_tracklist_completo() -> None:
    """Tracklist realista: headers, numeración, timestamps, IDs, ruido."""
    text = """\
TRACKLIST:

01. [00:00] Chris Stussy - All Night Long [PIV]
02. [05:12] A-Trak - Sway (LiTek Remix)
03. ID - ID
04. w/ Prunk - La Manija
05. Locklead - Mr. Madder (Extended Mix)

gracias por escuchar!
"""
    tracks = parse_tracklist(text)
    assert len(tracks) == 5

    assert tracks[0].artist == "Chris Stussy"
    assert tracks[0].title == "All Night Long"
    assert tracks[0].line_no == 3

    assert tracks[1].remix == "LiTek Remix"

    assert tracks[2].is_id is True
    assert tracks[2].line_no == 5

    assert tracks[3].artist == "Prunk"

    assert tracks[4].remix == "Extended Mix"
    assert tracks[4].line_no == 7


def test_parse_tracklist_vacio() -> None:
    assert parse_tracklist("") == []
    assert parse_tracklist("\n\n\n") == []


# --------------------------------------------------------------------------- #
# ParsedTrack.query
# --------------------------------------------------------------------------- #

def test_query_sin_remix() -> None:
    t = ParsedTrack(raw="x", line_no=1, artist="Chris Stussy", title="Breather")
    assert t.query == "Chris Stussy Breather"


def test_query_con_remix() -> None:
    """El remix va en el query: distingue versiones que para un DJ son
    tracks distintos."""
    t = ParsedTrack(
        raw="x", line_no=1, artist="A-Trak", title="Sway", remix="LiTek Remix",
    )
    assert t.query == "A-Trak Sway LiTek Remix"


def test_query_id_sin_artista() -> None:
    t = ParsedTrack(raw="ID", line_no=1, artist="", title="ID", is_id=True)
    assert t.query == "ID"
