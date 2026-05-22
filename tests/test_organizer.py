"""Tests para :mod:`core.organizer`.

``parse_enriched_genre`` y ``compute_target`` son lógica pura de strings/
paths. ``collect_audio_files`` y ``organize_file`` tocan filesystem
(``tmp_path`` de pytest).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.organizer import (
    OrganizeResult,
    collect_audio_files,
    compute_target,
    organize_file,
    parse_enriched_genre,
)


# --------------------------------------------------------------------------- #
# parse_enriched_genre
# --------------------------------------------------------------------------- #

class TestParseEnrichedGenre:
    @pytest.mark.parametrize("tag,expected", [
        # Caso normal
        ("Tech House / ALTA", ("Tech House", "ALTA")),
        # Sin nivel
        ("Tech House", ("Tech House", None)),
        # Compound con nivel — captura solo el último `/ NIVEL`
        ("Disco / Nu-Disco / ALTA", ("Disco / Nu-Disco", "ALTA")),
        # Compound sin nivel (Nu-Disco no es nivel válido)
        ("Disco / Nu-Disco", ("Disco / Nu-Disco", None)),
        # Multi-word level
        ("Techno / MUY ALTA", ("Techno", "MUY ALTA")),
        # Vacío
        ("", ("", None)),
        # Patológico: solo el sufijo (base queda vacío)
        ("/ ALTA", ("", "ALTA")),
    ])
    def test_basic_cases(self, tag, expected):
        assert parse_enriched_genre(tag) == expected

    @pytest.mark.parametrize("tag,expected_base,expected_level", [
        # El parseo normaliza el nivel a mayúsculas (mejora documentada vs original)
        ("Tech House / alta", "Tech House", "ALTA"),
        ("Tech House / Alta", "Tech House", "ALTA"),
        ("Tech House / aLTa", "Tech House", "ALTA"),
        ("Tech House / muy alta", "Tech House", "MUY ALTA"),
        ("Tech House / Muy Alta", "Tech House", "MUY ALTA"),
    ])
    def test_level_uppercased(self, tag, expected_base, expected_level):
        """Normalización: aunque el regex es case-insensitive, el nivel
        devuelto siempre es UPPERCASE para que compute_target genere folders
        canónicos."""
        base, level = parse_enriched_genre(tag)
        assert base == expected_base
        assert level == expected_level

    def test_does_not_match_internal_slash(self):
        """`/` interno sin nivel al final no se interpreta como sufijo."""
        # En "House / Other" no hay nivel válido al final → base es todo el string
        base, level = parse_enriched_genre("House / Other")
        assert base == "House / Other"
        assert level is None


# --------------------------------------------------------------------------- #
# compute_target (4 caminos + flat + compound)
# --------------------------------------------------------------------------- #

class TestComputeTarget:
    def setup_method(self):
        self.source = Path("/music/track.flac")
        self.dest_base = Path("/dest/Lossless")

    def test_no_genre_goes_to_sin_tag(self):
        target, motivo = compute_target(self.source, "", self.dest_base)
        assert target == self.dest_base / "_SIN_TAG" / "track.flac"
        assert motivo == "sin tag genre"

    def test_only_suffix_goes_to_sin_tag(self):
        """`/ ALTA` → base vacía → _SIN_TAG (no `/ALTA/` folder)."""
        target, motivo = compute_target(self.source, "/ ALTA", self.dest_base)
        assert target == self.dest_base / "_SIN_TAG" / "track.flac"
        assert motivo == "sin tag genre"

    def test_genre_without_level_goes_to_sin_nivel(self):
        target, motivo = compute_target(
            self.source, "Tech House", self.dest_base,
        )
        assert target == self.dest_base / "Tech House" / "_SIN_NIVEL" / "track.flac"
        assert motivo == "genre sin sufijo / NIVEL"

    def test_normal_genre_and_level(self):
        target, motivo = compute_target(
            self.source, "Tech House / ALTA", self.dest_base,
        )
        assert target == self.dest_base / "Tech House" / "ALTA" / "track.flac"
        assert motivo == "ok"

    def test_flat_omits_level_subfolder(self):
        target, motivo = compute_target(
            self.source, "Tech House / ALTA", self.dest_base, flat=True,
        )
        # flat=True: omite el subnivel de energía
        assert target == self.dest_base / "Tech House" / "track.flac"
        assert motivo == "ok"

    def test_flat_with_no_level_does_not_use_sin_nivel(self):
        """Con flat=True, un genre sin sufijo va directamente a la carpeta
        del género (no a _SIN_NIVEL)."""
        target, motivo = compute_target(
            self.source, "Tech House", self.dest_base, flat=True,
        )
        assert target == self.dest_base / "Tech House" / "track.flac"
        assert motivo == "ok"

    def test_compound_genre_sanitizes_internal_slash(self):
        """`Disco / Nu-Disco` → folder `Disco - Nu-Disco` vía safe_dirname."""
        target, motivo = compute_target(
            self.source, "Disco / Nu-Disco / ALTA", self.dest_base,
        )
        assert target == (
            self.dest_base / "Disco - Nu-Disco" / "ALTA" / "track.flac"
        )
        assert motivo == "ok"

    def test_lowercase_level_normalized_to_upper_folder(self):
        """`Tech House / alta` → folder `ALTA/` (normalización del organizer)."""
        target, motivo = compute_target(
            self.source, "Tech House / alta", self.dest_base,
        )
        assert target == self.dest_base / "Tech House" / "ALTA" / "track.flac"
        assert motivo == "ok"

    def test_dest_base_can_be_relative(self):
        """compute_target funciona con paths relativos también."""
        rel_dest = Path("output")
        target, _ = compute_target(
            self.source, "Tech House / ALTA", rel_dest,
        )
        assert target == rel_dest / "Tech House" / "ALTA" / "track.flac"


# --------------------------------------------------------------------------- #
# collect_audio_files (filtros sobre árbol sintético)
# --------------------------------------------------------------------------- #

class TestCollectAudioFiles:
    def test_basic_collection(self, tmp_path: Path):
        src = tmp_path / "music"
        dst = tmp_path / "out"
        src.mkdir()
        (src / "a.flac").touch()
        (src / "b.mp3").touch()
        (src / "c.wav").touch()
        dst.mkdir()

        files = collect_audio_files(src, dst)
        assert sorted(p.name for p in files) == ["a.flac", "b.mp3", "c.wav"]

    def test_filters_sidecars(self, tmp_path: Path):
        """Archivos `._*` (sidecars macOS/exFAT) se excluyen."""
        src = tmp_path / "music"
        dst = tmp_path / "out"
        src.mkdir()
        (src / "a.flac").touch()
        (src / "._a.flac").touch()  # sidecar de a.flac
        (src / "._otherjunk").touch()
        dst.mkdir()

        files = collect_audio_files(src, dst)
        assert [p.name for p in files] == ["a.flac"]

    def test_filters_non_audio(self, tmp_path: Path):
        """Solo archivos con extensión en AUDIO_EXTS."""
        src = tmp_path / "music"
        dst = tmp_path / "out"
        src.mkdir()
        (src / "track.flac").touch()
        (src / "cover.jpg").touch()
        (src / "playlist.m3u8").touch()
        (src / "README.txt").touch()
        dst.mkdir()

        files = collect_audio_files(src, dst)
        assert [p.name for p in files] == ["track.flac"]

    def test_filters_underscore_subdirs(self, tmp_path: Path):
        """Archivos en subcarpetas con prefijo `_` se excluyen."""
        src = tmp_path / "music"
        dst = tmp_path / "out"
        src.mkdir()
        (src / "a.flac").touch()
        (src / "_REVISAR").mkdir()
        (src / "_REVISAR" / "b.flac").touch()
        (src / "_SIN_TAG").mkdir()
        (src / "_SIN_TAG" / "c.flac").touch()
        # Una subcarpeta normal sí se incluye
        (src / "normal").mkdir()
        (src / "normal" / "d.flac").touch()
        dst.mkdir()

        files = collect_audio_files(src, dst)
        names = sorted(p.name for p in files)
        assert names == ["a.flac", "d.flac"]

    def test_does_not_filter_source_root_starting_with_underscore(
        self, tmp_path: Path,
    ):
        """El propio source_root NO se mira contra HIDDEN_DIR_PREFIX."""
        src = tmp_path / "_my_music"  # source empieza con _
        dst = tmp_path / "out"
        src.mkdir()
        (src / "a.flac").touch()
        (src / "b.flac").touch()
        dst.mkdir()

        files = collect_audio_files(src, dst)
        # Los archivos NO se filtran aunque el root empieza con _
        assert sorted(p.name for p in files) == ["a.flac", "b.flac"]

    def test_excludes_files_inside_dest(self, tmp_path: Path):
        """Si dest está bajo source, archivos en dest no se procesan
        (evita re-procesar archivos organizados en corrida previa)."""
        src = tmp_path / "music"
        src.mkdir()
        (src / "fresh.flac").touch()
        # dest queda DENTRO de source
        nested_dst = src / "organized"
        nested_dst.mkdir()
        (nested_dst / "old.flac").touch()
        (nested_dst / "Tech House").mkdir()
        (nested_dst / "Tech House" / "older.flac").touch()

        files = collect_audio_files(src, nested_dst)
        # Solo fresh.flac, los archivos dentro de dest se filtran
        assert [p.name for p in files] == ["fresh.flac"]

    def test_sorted_output(self, tmp_path: Path):
        """El resultado viene ordenado (predictibilidad del CSV)."""
        src = tmp_path / "music"
        dst = tmp_path / "out"
        src.mkdir()
        for name in ["zeta.flac", "alpha.flac", "mike.flac"]:
            (src / name).touch()
        dst.mkdir()

        files = collect_audio_files(src, dst)
        assert [p.name for p in files] == ["alpha.flac", "mike.flac", "zeta.flac"]

    def test_empty_source_returns_empty_list(self, tmp_path: Path):
        src = tmp_path / "music"
        dst = tmp_path / "out"
        src.mkdir()
        dst.mkdir()
        assert collect_audio_files(src, dst) == []

    def test_recursive_through_normal_subdirs(self, tmp_path: Path):
        """Subcarpetas sin prefijo `_` se recorren recursivamente."""
        src = tmp_path / "music"
        dst = tmp_path / "out"
        src.mkdir()
        (src / "sub1").mkdir()
        (src / "sub1" / "a.flac").touch()
        (src / "sub1" / "sub2").mkdir()
        (src / "sub1" / "sub2" / "b.flac").touch()
        dst.mkdir()

        files = collect_audio_files(src, dst)
        names = sorted(p.name for p in files)
        assert names == ["a.flac", "b.flac"]


# --------------------------------------------------------------------------- #
# organize_file (IO real con tmp_path)
# --------------------------------------------------------------------------- #

class TestOrganizeFile:
    def test_dry_run_does_not_touch_filesystem(self, tmp_path: Path):
        src = tmp_path / "src.flac"
        src.write_bytes(b"contenido")
        target = tmp_path / "out" / "Tech House" / "ALTA" / "src.flac"

        result = organize_file(src, target, "Tech House / ALTA", "ok", dry_run=True)
        assert result.status == "ok"
        assert result.motivo == "ok"
        # Filesystem intacto
        assert not target.exists()
        assert not target.parent.exists()
        # Source preservado
        assert src.exists()

    def test_copy_real_preserves_source(self, tmp_path: Path):
        src = tmp_path / "src.flac"
        src.write_bytes(b"contenido")
        target = tmp_path / "out" / "Tech House" / "ALTA" / "src.flac"

        result = organize_file(src, target, "Tech House / ALTA", "ok")
        assert result.status == "ok"
        # Source y target ambos existen
        assert src.exists()
        assert target.exists()
        # Contenido idéntico
        assert target.read_bytes() == b"contenido"

    def test_move_real_removes_source(self, tmp_path: Path):
        src = tmp_path / "src.flac"
        src.write_bytes(b"contenido")
        target = tmp_path / "out" / "sub" / "src.flac"

        result = organize_file(src, target, "X", "ok", move=True)
        assert result.status == "ok"
        # Source desaparece, target aparece
        assert not src.exists()
        assert target.exists()

    def test_creates_parent_directories(self, tmp_path: Path):
        """target.parent.mkdir(parents=True) crea todos los ancestros."""
        src = tmp_path / "src.flac"
        src.write_bytes(b"x")
        # Target con 4 niveles de profundidad que no existen
        target = tmp_path / "a" / "b" / "c" / "d" / "src.flac"
        assert not target.parent.exists()

        result = organize_file(src, target, "X", "ok")
        assert result.status == "ok"
        assert target.parent.exists()
        assert target.exists()

    def test_error_when_source_missing(self, tmp_path: Path):
        """Source inexistente: status='error', motivo combina base + error IO."""
        src = tmp_path / "no_existe.flac"
        target = tmp_path / "out" / "no_existe.flac"

        result = organize_file(src, target, "X", "ok")
        assert result.status == "error"
        # El motivo arranca con el base_motivo y le concatena el error IO
        assert result.motivo.startswith("ok |")

    def test_result_preserves_genre_tag_for_csv(self, tmp_path: Path):
        """genre_tag pasa por organize_file sin tocarse — el CLI lo necesita
        para escribir el CSV en una sola pasada."""
        src = tmp_path / "src.flac"
        src.write_bytes(b"x")
        target = tmp_path / "out" / "src.flac"

        result = organize_file(
            src, target, "Tech House / ALTA", "ok", dry_run=True,
        )
        assert result.genre_tag == "Tech House / ALTA"

    def test_result_dataclass_fields(self, tmp_path: Path):
        """OrganizeResult tiene los 5 campos esperados."""
        src = tmp_path / "src.flac"
        src.write_bytes(b"x")
        target = tmp_path / "out" / "src.flac"

        result = organize_file(src, target, "X / ALTA", "ok", dry_run=True)
        assert isinstance(result, OrganizeResult)
        assert result.source == src
        assert result.target == target
        assert result.status == "ok"
        assert result.motivo == "ok"
        assert result.genre_tag == "X / ALTA"
