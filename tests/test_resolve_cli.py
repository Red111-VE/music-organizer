"""Tests para :mod:`cli.resolve`: el comando ``resolve`` end-to-end.

CliRunner de Click con la red monkeypatcheada en los puntos que el módulo
importa (``cli.resolve.resolve_track`` / ``cli.resolve.fetch_tracklist_text``).
El tracker siempre apunta a ``tmp_path`` vía ``--store`` — jamás se toca
``~/.music-organizer``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from cli import resolve as resolve_module
from cli.main import cli
from core.resolver import Candidate, TrackResolution
from core.sources import SourceError, SourceResult
from core.tracklist import ParsedTrack


_TRACKLIST = """\
01. Chris Stussy - All Night Long
02. ID - ID
03. Artista Inventado - Track Inexistente
"""


def _fake_resolve(track: ParsedTrack, **kwargs: object) -> TrackResolution:
    """Resolver determinista: el primero ok, el ID corto-circuito, el
    inventado no-encontrado."""
    if track.is_id:
        return TrackResolution(
            track=track, status="id", best=None, youtube_search_url="",
        )
    if track.artist == "Chris Stussy":
        return TrackResolution(
            track=track,
            status="ok",
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
def runner(monkeypatch: pytest.MonkeyPatch) -> CliRunner:
    monkeypatch.setattr(resolve_module, "resolve_track", _fake_resolve)
    return CliRunner()


def _store_arg(tmp_path: Path) -> list[str]:
    return ["--store", str(tmp_path / "unreleased.json")]


# --------------------------------------------------------------------------- #
# Inputs
# --------------------------------------------------------------------------- #

def test_resolve_archivo(runner: CliRunner, tmp_path: Path) -> None:
    tracklist = tmp_path / "tracklist.txt"
    tracklist.write_text(_TRACKLIST, encoding="utf-8")

    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(
            cli, ["resolve", str(tracklist), *_store_arg(tmp_path)],
        )
    assert result.exit_code == 0, result.output
    assert "Encontrados    : 1" in result.output
    assert "No encontrados : 1" in result.output
    assert "IDs            : 1" in result.output


def test_resolve_stdin(runner: CliRunner, tmp_path: Path) -> None:
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(
            cli, ["resolve", "-", *_store_arg(tmp_path)], input=_TRACKLIST,
        )
    assert result.exit_code == 0, result.output
    assert "stdin" in result.output
    assert "Encontrados    : 1" in result.output


def test_resolve_texto_literal(runner: CliRunner, tmp_path: Path) -> None:
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(
            cli,
            ["resolve", "Chris Stussy - All Night Long", *_store_arg(tmp_path)],
        )
    assert result.exit_code == 0, result.output
    assert "texto directo" in result.output
    assert "Encontrados    : 1" in result.output


def test_resolve_url(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_fetch(url: str, **kwargs: object) -> SourceResult:
        assert "youtu" in url
        return SourceResult(
            text=_TRACKLIST, origin="youtube:descripcion", title="Mi Set",
        )

    monkeypatch.setattr(resolve_module, "fetch_tracklist_text", fake_fetch)
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(
            cli,
            ["resolve", "https://youtu.be/dQw4w9WgXcQ", *_store_arg(tmp_path)],
        )
    assert result.exit_code == 0, result.output
    assert "youtube:descripcion" in result.output
    assert "Mi Set" in result.output


def test_resolve_url_con_error_accionable(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Un SourceError (sin key, video inexistente…) sale como mensaje
    limpio, sin traceback."""
    def fake_fetch(url: str, **kwargs: object) -> SourceResult:
        raise SourceError("Para leer YouTube hace falta una API key gratuita")

    monkeypatch.setattr(resolve_module, "fetch_tracklist_text", fake_fetch)
    result = runner.invoke(cli, ["resolve", "https://youtu.be/dQw4w9WgXcQ"])
    assert result.exit_code != 0
    assert "API key gratuita" in result.output
    assert "Traceback" not in result.output


def test_resolve_sin_input_da_ayuda(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["resolve"])
    assert result.exit_code != 0
    assert "Falta el input" in result.output


def test_resolve_texto_sin_tracks(runner: CliRunner, tmp_path: Path) -> None:
    """Texto multilínea (claramente texto, no una ruta) sin ningún track
    parseable → el error de "0 tracks", no el de archivo inexistente."""
    result = runner.invoke(
        cli,
        ["resolve", "hola que tal\neste texto no tiene tracks", *_store_arg(tmp_path)],
    )
    assert result.exit_code != 0
    assert "parseables" in result.output


def test_resolve_limit(runner: CliRunner, tmp_path: Path) -> None:
    tracklist = tmp_path / "t.txt"
    tracklist.write_text(_TRACKLIST, encoding="utf-8")
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(
            cli,
            ["resolve", str(tracklist), "--limit", "1", *_store_arg(tmp_path)],
        )
    assert result.exit_code == 0, result.output
    assert "Tracks a resolver: 1" in result.output


# --------------------------------------------------------------------------- #
# CSV + tracker
# --------------------------------------------------------------------------- #

def test_resolve_escribe_csv(runner: CliRunner, tmp_path: Path) -> None:
    tracklist = tmp_path / "t.txt"
    tracklist.write_text(_TRACKLIST, encoding="utf-8")

    workdir = tmp_path / "work"
    workdir.mkdir()
    with runner.isolated_filesystem(temp_dir=workdir) as fs:
        result = runner.invoke(
            cli, ["resolve", str(tracklist), *_store_arg(tmp_path)],
        )
        assert result.exit_code == 0, result.output
        csv_path = Path(fs) / "_REPORTE_TRACKLIST.csv"
        assert csv_path.exists()
        content = csv_path.read_text(encoding="utf-8-sig")

    header = content.splitlines()[0]
    assert header.split(",")[:6] == [
        "estado", "linea", "artista", "titulo", "remix", "proveedor",
    ]
    assert "no-encontrado" in content
    assert "deezer" in content


def test_resolve_anota_no_encontrados_en_tracker(
    runner: CliRunner, tmp_path: Path,
) -> None:
    tracklist = tmp_path / "t.txt"
    tracklist.write_text(_TRACKLIST, encoding="utf-8")
    store_file = tmp_path / "unreleased.json"

    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(
            cli, ["resolve", str(tracklist), "--store", str(store_file)],
        )
    assert result.exit_code == 0, result.output
    assert "1 track(s) anotado(s)" in result.output

    data = json.loads(store_file.read_text(encoding="utf-8"))
    artists = [t["artist"] for t in data["tracks"]]
    # Solo el no-encontrado con nombre; ni el ok ni el ID.
    assert artists == ["Artista Inventado"]


def test_resolve_no_anota_fallos_de_red_en_tracker(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REGRESIÓN: un no-encontrado por corte de RED (notes con errores de
    proveedor) NO debe ir al tracker — contaminaría el tracker con tracks
    que sí existen."""
    def network_down(track: ParsedTrack, **kwargs: object) -> TrackResolution:
        return TrackResolution(
            track=track, status="no-encontrado", best=None,
            youtube_search_url="",
            notes=["deezer: Error de red consultando la fuente: DNS"],
        )

    monkeypatch.setattr(resolve_module, "resolve_track", network_down)
    store_file = tmp_path / "unreleased.json"
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(
            cli,
            ["resolve", "Artista Real - Track Real", "--store", str(store_file)],
        )
    assert result.exit_code == 0, result.output
    assert "anotado(s)" not in result.output
    if store_file.exists():
        data = json.loads(store_file.read_text(encoding="utf-8"))
        assert data["tracks"] == []


def test_resolve_archivo_inexistente_error_claro(
    runner: CliRunner, tmp_path: Path,
) -> None:
    """REGRESIÓN: «missing.txt» caía a texto literal y daba el confuso
    "no encontré líneas parseables" en vez de decir que el archivo no
    existe."""
    result = runner.invoke(cli, ["resolve", "missing.txt", *_store_arg(tmp_path)])
    assert result.exit_code != 0
    assert "No existe el archivo" in result.output
    assert "missing.txt" in result.output


def test_resolve_directorio_como_input_error_claro(
    runner: CliRunner, tmp_path: Path,
) -> None:
    subdir = tmp_path / "carpeta"
    subdir.mkdir()
    result = runner.invoke(cli, ["resolve", str(subdir), *_store_arg(tmp_path)])
    assert result.exit_code != 0
    assert "no es un archivo legible" in result.output


def test_resolve_tracker_corrupto_es_warning_no_fatal(
    runner: CliRunner, tmp_path: Path,
) -> None:
    """En modo resolve, el tracker es secundario: corrupto → warning y el
    comando completa igual."""
    store_file = tmp_path / "unreleased.json"
    store_file.write_text("{{{corrupto")
    tracklist = tmp_path / "t.txt"
    tracklist.write_text(_TRACKLIST, encoding="utf-8")

    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(
            cli, ["resolve", str(tracklist), "--store", str(store_file)],
        )
    assert result.exit_code == 0, result.output
    assert "AVISO" in result.output
    # El archivo corrupto NO fue pisado.
    assert store_file.read_text() == "{{{corrupto"


# --------------------------------------------------------------------------- #
# --recheck
# --------------------------------------------------------------------------- #

def test_recheck_vacio(runner: CliRunner, tmp_path: Path) -> None:
    result = runner.invoke(cli, ["resolve", "--recheck", *_store_arg(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "vacío" in result.output


def test_recheck_con_input_es_error(runner: CliRunner, tmp_path: Path) -> None:
    result = runner.invoke(
        cli, ["resolve", "--recheck", "algo.txt", *_store_arg(tmp_path)],
    )
    assert result.exit_code != 0
    assert "no lleva input" in result.output


def test_recheck_encuentra_los_que_salieron(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Sembrar el tracker con dos entradas.
    store_file = tmp_path / "unreleased.json"
    store_file.write_text(json.dumps({
        "version": 1,
        "tracks": [
            {"artist": "Chris Stussy", "title": "All Night Long",
             "first_seen": "2026-01-01", "last_checked": "2026-01-01"},
            {"artist": "Artista Inventado", "title": "Track Inexistente",
             "first_seen": "2026-01-01", "last_checked": "2026-01-01"},
        ],
    }))
    # El recheck usa core.unreleased.resolve_track — se patchea allá.
    from core import unreleased as unreleased_module
    monkeypatch.setattr(unreleased_module, "resolve_track", _fake_resolve)

    result = runner.invoke(
        cli, ["resolve", "--recheck", "--store", str(store_file)],
    )
    assert result.exit_code == 0, result.output
    assert "¡1 track(s) ya salieron!" in result.output
    assert "Quedan 1 track(s)" in result.output

    data = json.loads(store_file.read_text(encoding="utf-8"))
    assert len(data["tracks"]) == 1
    assert data["tracks"][0]["artist"] == "Artista Inventado"


def test_recheck_tracker_corrupto_es_fatal(
    runner: CliRunner, tmp_path: Path,
) -> None:
    """En modo --recheck el tracker es la única razón de ser: corrupto →
    error fatal accionable."""
    store_file = tmp_path / "unreleased.json"
    store_file.write_text("{{{corrupto")
    result = runner.invoke(
        cli, ["resolve", "--recheck", "--store", str(store_file)],
    )
    assert result.exit_code != 0
    assert "corrupto" in result.output
