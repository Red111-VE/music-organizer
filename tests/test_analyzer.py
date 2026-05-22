"""Tests para :mod:`core.analyzer` — las funciones puras, sin Essentia.

Cubre ``simplify_genre`` y ``energy_bucket``, que son lógica de strings/
floats sin dependencia de Essentia ni audio. ``analyze_track`` (la función
pesada que sí carga modelos) se valida manualmente con audio + modelos
reales en la fase final, no aquí.
"""

from __future__ import annotations

import pytest

from core.analyzer import TrackAnalysis, energy_bucket, simplify_genre


# --------------------------------------------------------------------------- #
# simplify_genre
# --------------------------------------------------------------------------- #

class TestSimplifyGenre:
    @pytest.mark.parametrize("label,expected", [
        # Caso canónico de Discogs: "Familia---Sub-etiqueta"
        ("Electronic---Tech House", "Tech House"),
        ("Electronic---Deep House", "Deep House"),
        ("Electronic---House", "House"),
        # Mapeos compuestos
        ("Electronic---Disco", "Disco / Nu-Disco"),
        ("Electronic---Nu-Disco", "Disco / Nu-Disco"),
        ("Electronic---Italo-Disco", "Disco / Nu-Disco"),
        ("Electronic---Minimal", "Minimal / Deep Tech"),
        ("Electronic---Minimal Techno", "Minimal / Deep Tech"),
        # Sin separador --- (string entero se busca en el mapa)
        ("Tech House", "Tech House"),
        # Fallback: sub-etiqueta no mapeada → se devuelve textual (case preservado)
        ("Electronic---FOOBAR", "FOOBAR"),
        # Whitespace en la sub-etiqueta se descarta
        ("Electronic--- Tech House  ", "Tech House"),
        # Múltiples --- → solo importa el último segmento
        ("Foo---Bar---Tech House", "Tech House"),
        # Case-insensitive: la lookup se hace con .lower()
        ("Electronic---TECH HOUSE", "Tech House"),
        ("Electronic---tech house", "Tech House"),
        ("Electronic---Tech HOUSE", "Tech House"),
    ])
    def test_known_cases(self, label, expected):
        assert simplify_genre(label) == expected

    def test_empty_string(self):
        """``''.split('---')[-1] = ''`` → ``GENRE_SIMPLIFY.get('', '')``."""
        assert simplify_genre("") == ""

    def test_only_separator(self):
        """``'---'.split('---')[-1] = ''`` → fallback al texto vacío."""
        assert simplify_genre("---") == ""


# --------------------------------------------------------------------------- #
# energy_bucket
# --------------------------------------------------------------------------- #

class TestEnergyBucket:
    """Las comparaciones usan ``arousal`` **crudo**, el retorno trae el valor
    **redondeado a 1 decimal**. Los thresholds son parametrizables."""

    @pytest.mark.parametrize("arousal,expected_level,expected_value", [
        # Defaults: (4.8, 5.8, 6.6)
        (4.0, "BAJA", 4.0),
        (4.79, "BAJA", 4.8),  # crudo 4.79 < 4.8; valor redondea a 4.8
        (4.8, "MEDIA", 4.8),  # boundary exacto: <4.8 BAJA, >=4.8 MEDIA
        (5.7, "MEDIA", 5.7),
        (5.8, "ALTA", 5.8),  # boundary exacto
        (6.5, "ALTA", 6.5),
        (6.6, "MUY ALTA", 6.6),  # boundary exacto: >=6.6 MUY ALTA
        (7.2, "MUY ALTA", 7.2),
        (9.0, "MUY ALTA", 9.0),  # extremo superior de la escala 1-9
        (1.0, "BAJA", 1.0),      # extremo inferior
    ])
    def test_default_thresholds(self, arousal, expected_level, expected_value):
        level, value = energy_bucket(arousal)
        assert level == expected_level
        assert value == expected_value

    def test_custom_thresholds(self):
        """Caso de uso de ``recalibrate``: umbrales custom."""
        thresholds = (5.0, 6.0, 7.0)
        assert energy_bucket(4.5, thresholds) == ("BAJA", 4.5)
        assert energy_bucket(5.5, thresholds) == ("MEDIA", 5.5)
        assert energy_bucket(6.5, thresholds) == ("ALTA", 6.5)
        assert energy_bucket(7.5, thresholds) == ("MUY ALTA", 7.5)

    def test_comparison_uses_raw_not_rounded(self):
        """4.79 redondea a 4.8 pero el bucket se decide sobre el crudo.

        Si la decisión usara el rounded, 4.79 caería en MEDIA (4.8 >= 4.8).
        Con raw: 4.79 < 4.8 → BAJA.
        """
        level, value = energy_bucket(4.79)
        assert level == "BAJA"
        assert value == 4.8

    def test_auto_calibrated_thresholds_typical(self):
        """Umbrales auto-calibrados típicos (~Q1/Q2/Q3 de una biblioteca
        de house concentrada en el rango alto)."""
        thresholds = (5.9, 6.1, 6.4)
        # Distribución más balanceada con umbrales estrechos
        assert energy_bucket(5.5, thresholds)[0] == "BAJA"
        assert energy_bucket(6.0, thresholds)[0] == "MEDIA"
        assert energy_bucket(6.2, thresholds)[0] == "ALTA"
        assert energy_bucket(6.5, thresholds)[0] == "MUY ALTA"


# --------------------------------------------------------------------------- #
# TrackAnalysis dataclass
# --------------------------------------------------------------------------- #

class TestTrackAnalysis:
    def test_construction_with_all_fields(self):
        t = TrackAnalysis(
            genre="Tech House",
            genre_raw="Electronic---Tech House",
            genre_top3_text="Tech House 63% | House 43% | Techno 34%",
            genre_confidence=0.63,
            arousal=6.123,
            energy_level="ALTA",
            energy_value=6.1,
        )
        assert t.genre == "Tech House"
        assert t.genre_raw == "Electronic---Tech House"
        assert t.genre_confidence == 0.63
        assert t.energy_level == "ALTA"

    def test_arousal_raw_vs_energy_value(self):
        """``arousal`` es el crudo del modelo; ``energy_value`` es el redondeado."""
        t = TrackAnalysis(
            genre="Tech House", genre_raw="...", genre_top3_text="...",
            genre_confidence=0.5, arousal=6.123, energy_level="ALTA",
            energy_value=6.1,
        )
        assert t.arousal != t.energy_value
        assert t.arousal == 6.123
        assert t.energy_value == 6.1
