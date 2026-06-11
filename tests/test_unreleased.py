"""Tests para :mod:`core.unreleased`: tracker persistente de unreleased.

Todo contra ``tmp_path`` — jamás se toca el store real del usuario
(``~/.music-organizer``). El recheck se prueba monkeypatcheando
``resolve_track`` en el módulo (sin red).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core import unreleased
from core.resolver import Candidate, TrackResolution
from core.tracklist import ParsedTrack
from core.unreleased import (
    UnreleasedEntry,
    UnreleasedStore,
    UnreleasedStoreError,
    recheck_store,
)


def _track(
    artist: str = "Chris Stussy",
    title: str = "Inédita",
    remix: str = "",
    is_id: bool = False,
) -> ParsedTrack:
    return ParsedTrack(
        raw=f"{artist} - {title}", line_no=1,
        artist=artist, title=title, remix=remix, is_id=is_id,
    )


def _store(tmp_path: Path) -> UnreleasedStore:
    return UnreleasedStore(tmp_path / "unreleased.json")


# --------------------------------------------------------------------------- #
# add / dedup
# --------------------------------------------------------------------------- #

def test_add_nuevo(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.add(_track(), today="2026-06-11") is True
    assert len(store) == 1
    entry = store.entries[0]
    assert entry.artist == "Chris Stussy"
    assert entry.first_seen == "2026-06-11"
    assert entry.times_seen == 1


def test_add_duplicado_incrementa_times_seen(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.add(_track())
    assert store.add(_track()) is False
    assert len(store) == 1
    assert store.entries[0].times_seen == 2


def test_add_dedup_por_normalizacion(tmp_path: Path) -> None:
    """Variantes de grafía/acentos son la misma entrada — la clave usa la
    normalización difusa del resolver."""
    store = _store(tmp_path)
    store.add(_track(artist="Trentemøller", title="Inédita"))
    assert store.add(_track(artist="trentemoller", title="inedita")) is False
    assert len(store) == 1
    assert store.entries[0].times_seen == 2


@pytest.mark.parametrize(
    "track",
    [
        pytest.param(_track(artist="ID", title="ID", is_id=True), id="id-puro"),
        pytest.param(_track(artist="", title="ID", is_id=True), id="id-sin-artista"),
        pytest.param(_track(artist="", title="Algo"), id="sin-artista"),
        pytest.param(_track(artist="Alguien", title=""), id="sin-titulo"),
    ],
)
def test_add_rechaza_no_trackeables(tmp_path: Path, track: ParsedTrack) -> None:
    """IDs y tracks sin nombre completo no se guardan — no hay query que
    re-intentar."""
    store = _store(tmp_path)
    assert store.add(track) is False
    assert len(store) == 0


# --------------------------------------------------------------------------- #
# Persistencia
# --------------------------------------------------------------------------- #

def test_roundtrip(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.add(_track(remix="Lane 8 Remix"), today="2026-06-11")
    store.save()

    reloaded = _store(tmp_path)
    assert len(reloaded) == 1
    entry = reloaded.entries[0]
    assert entry.artist == "Chris Stussy"
    assert entry.remix == "Lane 8 Remix"
    assert entry.first_seen == "2026-06-11"


def test_save_crea_directorio(tmp_path: Path) -> None:
    store = UnreleasedStore(tmp_path / "sub" / "dir" / "unreleased.json")
    store.add(_track())
    store.save()
    assert (tmp_path / "sub" / "dir" / "unreleased.json").exists()


def test_archivo_ausente_es_store_vacio(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert len(store) == 0


def test_archivo_corrupto_lanza_error_accionable(tmp_path: Path) -> None:
    """NUNCA se pisa en silencio un archivo ilegible — puede tener meses
    de datos. El error dice qué archivo y qué hacer."""
    path = tmp_path / "unreleased.json"
    path.write_text("esto no es json {{{")
    with pytest.raises(UnreleasedStoreError, match="corrupto"):
        UnreleasedStore(path)
    # El archivo sigue intacto.
    assert path.read_text() == "esto no es json {{{"


def test_version_desconocida_lanza_error(tmp_path: Path) -> None:
    path = tmp_path / "unreleased.json"
    path.write_text(json.dumps({"version": 99, "tracks": []}))
    with pytest.raises(UnreleasedStoreError, match="formato desconocido"):
        UnreleasedStore(path)


def test_entradas_ilegibles_se_saltan(tmp_path: Path) -> None:
    """Una entrada con shape inesperado se salta sin abortar el tracker."""
    path = tmp_path / "unreleased.json"
    path.write_text(json.dumps({
        "version": 1,
        "tracks": [
            "no-soy-dict",
            {"artist": None, "title": "X"},
            {"artist": "Válido", "title": "Track", "times_seen": "no-int"},
            {"artist": "Otro", "title": "Bueno", "times_seen": 3},
        ],
    }))
    store = UnreleasedStore(path)
    assert len(store) == 2
    by_artist = {e.artist: e for e in store.entries}
    assert by_artist["Válido"].times_seen == 1   # el string inválido cayó al default
    assert by_artist["Otro"].times_seen == 3


def test_save_atomico_no_deja_tempfiles(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.add(_track())
    store.save()
    store.save()  # segunda escritura sobre archivo existente
    leftovers = [p for p in tmp_path.iterdir() if p.name.startswith(".unreleased-")]
    assert leftovers == []
    # Y el JSON quedó válido.
    data = json.loads((tmp_path / "unreleased.json").read_text())
    assert data["version"] == 1


# --------------------------------------------------------------------------- #
# Recheck
# --------------------------------------------------------------------------- #

def _resolution(track: ParsedTrack, status: str) -> TrackResolution:
    best = None
    if status in ("ok", "dudoso"):
        best = Candidate(
            provider="deezer", artist=track.artist, title=track.title,
            url="https://www.deezer.com/track/1", score=95.0,
        )
    return TrackResolution(
        track=track, status=status, best=best,
        youtube_search_url="https://www.youtube.com/results?search_query=x",
    )


def test_recheck_remueve_los_que_salieron(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    store.add(_track(title="Ya Salió"))
    store.add(_track(title="Sigue Inédita"))

    def fake_resolve(track: ParsedTrack, **kwargs: object) -> TrackResolution:
        status = "ok" if track.title == "Ya Salió" else "no-encontrado"
        return _resolution(track, status)

    monkeypatch.setattr(unreleased, "resolve_track", fake_resolve)
    found = recheck_store(store)

    assert len(found) == 1
    entry, resolution = found[0]
    assert entry.title == "Ya Salió"
    assert resolution.best is not None

    # El que salió se fue; el inédito queda con last_checked actualizado.
    assert len(store) == 1
    assert store.entries[0].title == "Sigue Inédita"
    assert store.entries[0].last_checked != ""

    # Y quedó persistido (recheck guarda al final).
    reloaded = _store(tmp_path)
    assert len(reloaded) == 1


def test_recheck_dudoso_no_se_remueve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Un dudoso NO cuenta como salido — el usuario lo revisa a mano; el
    tracker lo sigue vigilando."""
    store = _store(tmp_path)
    store.add(_track())

    monkeypatch.setattr(
        unreleased, "resolve_track",
        lambda track, **kw: _resolution(track, "dudoso"),
    )
    found = recheck_store(store)
    assert found == []
    assert len(store) == 1


def test_recheck_on_result_callback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    store.add(_track(title="Uno"))
    store.add(_track(title="Dos"))

    monkeypatch.setattr(
        unreleased, "resolve_track",
        lambda track, **kw: _resolution(track, "no-encontrado"),
    )
    seen: list[str] = []
    recheck_store(store, on_result=lambda e, r: seen.append(e.title))
    assert seen == ["Uno", "Dos"]


def test_entry_to_parsed_track_con_remix() -> None:
    entry = UnreleasedEntry(artist="A", title="T", remix="X Remix")
    track = entry.to_parsed_track()
    assert track.artist == "A"
    assert track.remix == "X Remix"
    assert track.query == "A T X Remix"
    assert not track.is_id


def test_tracks_no_latinos_no_colisionan(tmp_path: Path) -> None:
    """REGRESIÓN: normalize() solo conserva [a-z0-9] — dos tracks 100%
    no-latinos (cirílico, japonés) normalizaban a clave vacía y se
    pisaban entre sí en el tracker."""
    store = _store(tmp_path)
    assert store.add(_track(artist="Кино", title="Группа крови")) is True
    assert store.add(_track(artist="坂本龍一", title="戦場のメリークリスマス")) is True
    assert len(store) == 2

    # Y la dedup exacta entre no-latinos sigue funcionando.
    assert store.add(_track(artist="Кино", title="Группа крови")) is False
    assert len(store) == 2
