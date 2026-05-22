"""Tests para :mod:`core.config`: constantes compartidas, regex, mapas.

Estos tests son **lógica pura**: no tocan filesystem, audio, ni mutagen.
Validan que las constantes que el resto del proyecto asume sigan en su
lugar y con el formato esperado.
"""

from __future__ import annotations

import re

import pytest

from core import config


# --------------------------------------------------------------------------- #
# Tipos y niveles
# --------------------------------------------------------------------------- #

def test_levels_tuple_order_and_size():
    assert config.LEVELS_TUPLE == ("BAJA", "MEDIA", "ALTA", "MUY ALTA")


def test_default_thresholds_ascending():
    low, med, high = config.DEFAULT_ENERGY_THRESHOLDS
    assert low < med < high


def test_default_thresholds_emomusic_raw_values():
    """Los defaults son los del modelo emoMusic crudo (no recalibrados)."""
    assert config.DEFAULT_ENERGY_THRESHOLDS == (4.8, 5.8, 6.6)
    assert config.ENERGY_THRESHOLD_LOW == 4.8
    assert config.ENERGY_THRESHOLD_MED == 5.8
    assert config.ENERGY_THRESHOLD_HIGH == 6.6


# --------------------------------------------------------------------------- #
# Audio extensions y prefijos
# --------------------------------------------------------------------------- #

def test_audio_exts_canonical_set():
    """Las 8 extensiones del pipeline (sin .alac, esa va en v0.2.0)."""
    expected = {
        ".mp3", ".flac", ".wav", ".aiff", ".aif", ".m4a", ".aac", ".ogg",
    }
    assert config.AUDIO_EXTS == expected


def test_no_alac_in_audio_exts():
    # .alac está en el organizador viejo (v0.2.0), no en Fase 1.
    assert ".alac" not in config.AUDIO_EXTS


def test_audio_exts_all_lowercase():
    """El check `path.suffix.lower() in AUDIO_EXTS` exige lowercase."""
    for ext in config.AUDIO_EXTS:
        assert ext == ext.lower()
        assert ext.startswith(".")


def test_sidecar_and_hidden_prefixes():
    assert config.SIDECAR_PREFIX == "._"
    assert config.HIDDEN_DIR_PREFIX == "_"


# --------------------------------------------------------------------------- #
# RE_ENERGY_FROM_COMMENT (enricher lo usa para extraer el nivel)
# --------------------------------------------------------------------------- #

class TestRegexEnergyFromComment:
    def test_matches_canonical_format(self):
        m = config.RE_ENERGY_FROM_COMMENT.match(
            "Energia: ALTA (6.0/9) | Genero: Tech House 63%"
        )
        assert m is not None
        assert m.group(1) == "ALTA"

    @pytest.mark.parametrize("level", ["BAJA", "MEDIA", "ALTA", "MUY ALTA"])
    def test_matches_all_levels(self, level):
        m = config.RE_ENERGY_FROM_COMMENT.match(f"Energia: {level} (5.0/9)")
        assert m is not None
        assert m.group(1) == level

    def test_case_sensitive_on_prefix(self):
        # 'energia' lowercase NO matchea (Energia con E mayúscula es canónico).
        assert config.RE_ENERGY_FROM_COMMENT.match(
            "energia: ALTA (6.0/9)"
        ) is None

    def test_no_match_on_unrelated(self):
        assert config.RE_ENERGY_FROM_COMMENT.match("comentario libre") is None
        assert config.RE_ENERGY_FROM_COMMENT.match("") is None


# --------------------------------------------------------------------------- #
# RE_LEVEL_SUFFIX (enricher, organizer, recalibrator)
# --------------------------------------------------------------------------- #

class TestRegexLevelSuffix:
    def test_captures_suffix(self):
        m = config.RE_LEVEL_SUFFIX.search("Tech House / ALTA")
        assert m is not None
        assert m.group(1) == "ALTA"

    def test_anchored_to_end(self):
        # No matchea si hay texto después del sufijo.
        assert config.RE_LEVEL_SUFFIX.search("Tech House / ALTA extra") is None

    def test_compound_genre_captures_only_last_suffix(self):
        """``Disco / Nu-Disco / ALTA`` → captura solo el último `/ ALTA`."""
        m = config.RE_LEVEL_SUFFIX.search("Disco / Nu-Disco / ALTA")
        assert m is not None
        assert m.group(1) == "ALTA"

    def test_compound_without_level_does_not_match(self):
        """``Disco / Nu-Disco`` no termina en NIVEL válido, no matchea."""
        assert config.RE_LEVEL_SUFFIX.search("Disco / Nu-Disco") is None

    def test_case_insensitive(self):
        """Sufijos en minúscula también se detectan (normalización en organizer)."""
        m = config.RE_LEVEL_SUFFIX.search("Tech House / alta")
        assert m is not None
        assert m.group(1).upper() == "ALTA"

    def test_multi_word_level(self):
        m = config.RE_LEVEL_SUFFIX.search("Techno / MUY ALTA")
        assert m is not None
        assert m.group(1) == "MUY ALTA"

    def test_multi_word_level_lowercase(self):
        m = config.RE_LEVEL_SUFFIX.search("Techno / muy alta")
        assert m is not None
        assert m.group(1).upper() == "MUY ALTA"


# --------------------------------------------------------------------------- #
# RE_FULL_COMMENT (recalibrator lo usa para extraer arousal + resto)
# --------------------------------------------------------------------------- #

class TestRegexFullComment:
    def test_extracts_level_arousal_and_rest(self):
        m = config.RE_FULL_COMMENT.match(
            "Energia: ALTA (6.0/9) | Genero: Tech House"
        )
        assert m is not None
        assert m.group(1) == "ALTA"
        assert m.group(2) == "6.0"  # arousal RAW, no float()
        assert m.group(3) == " | Genero: Tech House"

    def test_preserves_arousal_trailing_zero(self):
        """Blindaje del arousal: '6.10' debe capturarse textual, no como '6.1'."""
        m = config.RE_FULL_COMMENT.match("Energia: ALTA (6.10/9)")
        assert m is not None
        assert m.group(2) == "6.10"
        # Si lo convirtiéramos a float y de vuelta a str, perderíamos el cero
        assert str(float(m.group(2))) == "6.1"

    def test_preserves_arousal_multiple_decimals(self):
        m = config.RE_FULL_COMMENT.match("Energia: ALTA (6.123/9)")
        assert m is not None
        assert m.group(2) == "6.123"

    def test_no_match_without_arousal_part(self):
        assert config.RE_FULL_COMMENT.match("Energia: ALTA") is None
        assert config.RE_FULL_COMMENT.match("Otro texto") is None

    def test_empty_rest_when_no_pipe(self):
        m = config.RE_FULL_COMMENT.match("Energia: MUY ALTA (7.2/9)")
        assert m is not None
        assert m.group(3) == ""


# --------------------------------------------------------------------------- #
# GENRE_SIMPLIFY
# --------------------------------------------------------------------------- #

class TestGenreSimplify:
    def test_size_matches_original(self):
        """23 entradas según ``etiquetar_genero.py`` original."""
        assert len(config.GENRE_SIMPLIFY) == 23

    def test_all_keys_lowercase(self):
        """``simplify_genre`` hace lookup con ``.lower()`` — las keys deben
        estar en lowercase o nunca matchearán."""
        for key in config.GENRE_SIMPLIFY:
            assert key == key.lower(), f"key {key!r} no está en lowercase"

    @pytest.mark.parametrize("key,expected", [
        ("tech house", "Tech House"),
        ("deep house", "Deep House"),
        ("house", "House"),
        ("disco", "Disco / Nu-Disco"),
        ("nu-disco", "Disco / Nu-Disco"),
        ("italo-disco", "Disco / Nu-Disco"),
        ("minimal", "Minimal / Deep Tech"),
        ("minimal techno", "Minimal / Deep Tech"),
        ("micro house", "Minimal / Deep Tech"),
        ("trance", "Trance"),
        ("afro house", "Afro House"),
        ("drum n bass", "Drum & Bass"),
    ])
    def test_known_mappings(self, key, expected):
        assert config.GENRE_SIMPLIFY[key] == expected


# --------------------------------------------------------------------------- #
# Modelos Essentia
# --------------------------------------------------------------------------- #

def test_model_filenames_have_correct_extensions():
    assert config.EMB_MAEST.endswith(".pb")
    assert config.GENRE_HEAD.endswith(".pb")
    assert config.GENRE_LABELS.endswith(".json")
    assert config.EMB_VGGISH.endswith(".pb")
    assert config.ENERGY_HEAD.endswith(".pb")


def test_model_files_tuple_complete():
    """``MODEL_FILES`` tiene los 5 archivos para ``validate_models_dir``."""
    assert len(config.MODEL_FILES) == 5
    assert set(config.MODEL_FILES) == {
        config.EMB_MAEST,
        config.GENRE_HEAD,
        config.GENRE_LABELS,
        config.EMB_VGGISH,
        config.ENERGY_HEAD,
    }


# --------------------------------------------------------------------------- #
# Reportes CSV: nombres exactos del script original
# --------------------------------------------------------------------------- #

def test_csv_report_names_match_originals():
    """Paridad con los scripts originales — el usuario puede tener reportes
    históricos contra los que diff-ar."""
    assert config.CSV_TAG_REPORT == "_REPORTE_GENERO.csv"
    assert config.CSV_ENRICH_REPORT == "_REPORTE_ENRIQUECER.csv"
    assert config.CSV_REVERT_REPORT == "_REPORTE_REVERTIR.csv"
    assert config.CSV_ORGANIZE_REPORT == "_REPORTE_ORGANIZACION.csv"
    assert config.CSV_RECALIBRATE_REPORT == "_REPORTE_RECALCULO.csv"


# --------------------------------------------------------------------------- #
# Smoke test: los regex son objetos compilados
# --------------------------------------------------------------------------- #

def test_all_regex_are_compiled_patterns():
    assert isinstance(config.RE_ENERGY_FROM_COMMENT, re.Pattern)
    assert isinstance(config.RE_LEVEL_SUFFIX, re.Pattern)
    assert isinstance(config.RE_FULL_COMMENT, re.Pattern)
