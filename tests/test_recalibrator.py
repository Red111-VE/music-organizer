"""Tests para :mod:`core.recalibrator`.

Lógica pura: ``parse_arousal_comment`` (incluyendo el blindaje del arousal
textual), ``compute_calibrated_thresholds`` (percentiles 25/50/75) y
``compute_recalibration`` (los 3 motivos + el boundary con enricher: genre
sin sufijo NO se enriquece).
"""

from __future__ import annotations

from statistics import StatisticsError

import pytest

from core.config import DEFAULT_ENERGY_THRESHOLDS
from core.recalibrator import (
    RecalibrationResult,
    compute_calibrated_thresholds,
    compute_recalibration,
    parse_arousal_comment,
)


# --------------------------------------------------------------------------- #
# parse_arousal_comment
# --------------------------------------------------------------------------- #

class TestParseArousalComment:
    def test_canonical_format(self):
        result = parse_arousal_comment(
            "Energia: ALTA (6.0/9) | Genero: Tech House 63%"
        )
        assert result == ("ALTA", 6.0, "6.0", " | Genero: Tech House 63%")

    def test_returns_4_tuple(self):
        """Sanity: contrato del retorno son 4 elementos (level, float, raw, rest)."""
        result = parse_arousal_comment("Energia: ALTA (6.0/9)")
        assert result is not None
        assert len(result) == 4

    @pytest.mark.parametrize("level", ["BAJA", "MEDIA", "ALTA", "MUY ALTA"])
    def test_all_levels_parse(self, level):
        result = parse_arousal_comment(f"Energia: {level} (5.5/9)")
        assert result is not None
        assert result[0] == level

    def test_empty_rest_when_no_pipe(self):
        result = parse_arousal_comment("Energia: ALTA (6.0/9)")
        assert result is not None
        _, _, _, rest = result
        assert rest == ""

    @pytest.mark.parametrize("comment", [
        "",  # vacío
        "Otro texto",  # sin prefijo Energia:
        "energia: ALTA (6.0/9)",  # 'e' minúscula
        "Energia: ALTA",  # sin parte (X.X/9)
        "Energia: INVALIDO (6.0/9)",  # nivel no válido
    ])
    def test_invalid_format_returns_none(self, comment):
        assert parse_arousal_comment(comment) is None


class TestParseArousalRawPreservation:
    """El arousal_raw es el blindaje contra el round-trip float→str que
    pierde ceros finales. Estos tests confirman que llega textual del regex
    sin pasar por ``float()``."""

    @pytest.mark.parametrize("raw,expected_float", [
        ("6.0", 6.0),
        ("6.10", 6.1),  # cero final que float() come
        ("5.90", 5.9),
        ("7.000", 7.0),
        ("6.123", 6.123),
        ("4.8", 4.8),
    ])
    def test_raw_string_preserved_alongside_float(self, raw, expected_float):
        result = parse_arousal_comment(f"Energia: ALTA ({raw}/9)")
        assert result is not None
        level, arousal_float, arousal_raw, _ = result
        assert level == "ALTA"
        assert arousal_float == expected_float  # float disponible para stats
        assert arousal_raw == raw  # textual exacto para reconstruir comment

    def test_raw_differs_from_str_of_float(self):
        """Demostración del problema que el blindaje resuelve."""
        result = parse_arousal_comment("Energia: ALTA (6.10/9)")
        assert result is not None
        _, arousal_float, arousal_raw, _ = result
        # float() colapsa el cero final
        assert str(arousal_float) == "6.1"
        # raw lo preserva
        assert arousal_raw == "6.10"


# --------------------------------------------------------------------------- #
# compute_calibrated_thresholds
# --------------------------------------------------------------------------- #

class TestComputeCalibratedThresholds:
    def test_uniform_distribution_ascending(self):
        """Distribución uniforme: Q1<Q2<Q3 con valores predecibles."""
        arousals = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0]
        low, med, high = compute_calibrated_thresholds(arousals)
        assert low < med < high

    def test_returns_3_floats(self):
        arousals = [1.0, 2.0, 3.0, 4.0]
        result = compute_calibrated_thresholds(arousals)
        assert len(result) == 3
        assert all(isinstance(x, float) for x in result)

    def test_realistic_house_library_distribution(self):
        """Biblioteca house concentrada en el rango alto: percentiles
        deberían estar cerca entre sí (rango estrecho)."""
        arousals = [
            5.8, 5.9, 6.0, 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.7, 6.8, 6.9,
        ]
        low, _med, high = compute_calibrated_thresholds(arousals)
        # Distribución concentrada → rango (high - low) angosto
        assert (high - low) < 1.0

    def test_rounded_to_two_decimals(self):
        """Los umbrales se redondean a 2 decimales (legibilidad en logs/CSV).

        Por precisión float (IEEE 754) no podemos checkear ``v * 100 ==
        int(v * 100)`` ni ``v == round(v, 2)`` con igualdad estricta.
        Verificamos que el valor coincide con su redondeo a 2 decimales
        dentro de tolerancia de float.
        """
        arousals = [1.111, 2.222, 3.333, 4.444, 5.555, 6.666, 7.777]
        low, med, high = compute_calibrated_thresholds(arousals)
        for v in (low, med, high):
            assert v == pytest.approx(round(v, 2), abs=1e-9), (
                f"{v} no parece redondeado a 2 decimales"
            )

    def test_raises_on_too_few_data_points(self):
        """statistics.quantiles requiere ≥2 datos. El CLI cacha y muestra
        mensaje útil al usuario."""
        with pytest.raises(StatisticsError):
            compute_calibrated_thresholds([5.0])

    def test_works_with_exactly_two_data_points(self):
        """Mínimo: 2 datos (statistics.quantiles los interpola)."""
        # No debe lanzar
        result = compute_calibrated_thresholds([5.0, 7.0])
        assert len(result) == 3
        assert result[0] < result[1] < result[2]


# --------------------------------------------------------------------------- #
# compute_recalibration: los 3 motivos
# --------------------------------------------------------------------------- #

class TestComputeRecalibrationMotivos:
    def test_skip_when_comment_does_not_parse(self):
        result = compute_recalibration(
            "Tech House / ALTA", "comment sin formato", (4.0, 5.0, 6.0),
        )
        assert result.motivo == "skip: sin arousal en comment"
        assert result.arousal is None
        assert result.new_level is None
        assert result.old_level is None
        # En skip, los campos new_* quedan iguales a los old_*
        assert result.new_genre == "Tech House / ALTA"
        assert result.new_comment == "comment sin formato"

    def test_sin_cambio_when_level_unchanged(self):
        """Mismo nivel con los mismos umbrales: sin-cambio."""
        result = compute_recalibration(
            "Tech House / ALTA",
            "Energia: ALTA (6.0/9) | Genero: Tech House",
            (4.8, 5.8, 6.6),  # defaults: 6.0 ∈ [5.8, 6.6) → ALTA
        )
        assert result.motivo == "sin-cambio"
        assert result.old_level == "ALTA"
        assert result.new_level == "ALTA"
        assert result.new_genre == "Tech House / ALTA"

    def test_recalcular_when_level_changes(self):
        """Umbrales nuevos hacen que el nivel cambie."""
        result = compute_recalibration(
            "Tech House / ALTA",
            "Energia: ALTA (6.0/9) | Genero: Tech House",
            (5.5, 6.5, 7.5),  # 6.0 ∈ [5.5, 6.5) → MEDIA
        )
        assert result.motivo == "recalcular"
        assert result.old_level == "ALTA"
        assert result.new_level == "MEDIA"
        assert result.new_genre == "Tech House / MEDIA"
        # Comment reconstruido con el nivel nuevo
        assert result.new_comment == "Energia: MEDIA (6.0/9) | Genero: Tech House"

    def test_idempotency_with_same_thresholds(self):
        """Re-correr con los mismos umbrales devuelve sin-cambio."""
        comment = "Energia: ALTA (6.0/9) | Genero: X"
        thresholds = (5.0, 6.0, 7.0)
        # Primera corrida: ALTA con (5,6,7): 6.0 ∈ [6.0, 7.0) → ALTA (sin-cambio)
        r1 = compute_recalibration("X / ALTA", comment, thresholds)
        assert r1.motivo == "sin-cambio"
        # Segunda corrida con el resultado: mismo nivel → sin-cambio
        r2 = compute_recalibration(r1.new_genre, r1.new_comment, thresholds)
        assert r2.motivo == "sin-cambio"


# --------------------------------------------------------------------------- #
# Boundary con enricher: genre sin sufijo NO se enriquece
# --------------------------------------------------------------------------- #

class TestRecalibrationBoundaryWithEnricher:
    """Recalibrate **no** debe enriquecer pistas que estaban sin sufijo.
    Eso es trabajo de enricher. Si el comment cambia de nivel, solo se
    reescribe el comment — el genre se deja intacto."""

    def test_genre_without_suffix_not_enriched(self):
        """Genre sin sufijo y cambio de nivel: comment se actualiza pero
        genre queda intacto."""
        result = compute_recalibration(
            "Tech House",  # sin sufijo / NIVEL
            "Energia: ALTA (6.0/9) | Genero: Tech House",
            (5.5, 6.5, 7.5),  # 6.0 → MEDIA
        )
        # El comment cambia (nuevo nivel)
        assert result.new_level == "MEDIA"
        assert "MEDIA" in result.new_comment
        # Pero el genre NO se enriquece (no se le añade " / MEDIA")
        assert result.new_genre == "Tech House"
        assert "/" not in result.new_genre
        # Motivo: hubo cambio (en el nivel del comment)
        assert result.motivo == "recalcular"

    def test_genre_without_suffix_no_change_at_all(self):
        """Genre sin sufijo y nivel que no cambia: sin-cambio."""
        result = compute_recalibration(
            "Tech House",
            "Energia: ALTA (6.0/9)",
            (4.8, 5.8, 6.6),  # nivel sigue ALTA
        )
        assert result.motivo == "sin-cambio"
        assert result.new_genre == "Tech House"

    def test_genre_with_suffix_updates_suffix(self):
        """Genre con sufijo y nivel cambiado: sufijo se actualiza."""
        result = compute_recalibration(
            "Tech House / ALTA",
            "Energia: ALTA (6.0/9)",
            (5.5, 6.5, 7.5),  # 6.0 → MEDIA
        )
        assert result.new_genre == "Tech House / MEDIA"


# --------------------------------------------------------------------------- #
# Preservación textual del arousal en el comment reconstruido
# --------------------------------------------------------------------------- #

class TestArousalRawPreservedInNewComment:
    """Cuando compute_recalibration reescribe el comment, el arousal se
    preserva textual (raw), NO via str(float(...)) que perdería ceros."""

    @pytest.mark.parametrize("raw_in_comment", [
        "6.0",
        "6.10",  # cero final que float() come
        "5.90",
        "7.000",
        "6.123",
    ])
    def test_arousal_preserved_textual(self, raw_in_comment):
        """new_comment contiene el arousal textual exacto del original."""
        result = compute_recalibration(
            "X / ALTA",
            f"Energia: ALTA ({raw_in_comment}/9) | Genero: X",
            (8.0, 8.5, 9.0),  # umbrales altos para forzar cambio a BAJA
        )
        assert result.motivo == "recalcular"
        # arousal textual preservado en el comment nuevo
        assert f"({raw_in_comment}/9)" in result.new_comment

    def test_rest_of_comment_preserved(self):
        """Todo lo que viene después de `(X/9)` se preserva intacto."""
        result = compute_recalibration(
            "X / ALTA",
            "Energia: ALTA (6.0/9) | Genero: Tech House 63% | House 43%",
            (5.5, 6.5, 7.5),
        )
        assert result.new_comment == (
            "Energia: MEDIA (6.0/9) | Genero: Tech House 63% | House 43%"
        )


# --------------------------------------------------------------------------- #
# Sufijo lowercase normalizado a uppercase (consistencia con organizer)
# --------------------------------------------------------------------------- #

class TestSuffixCaseNormalization:
    def test_lowercase_suffix_normalized_in_new_genre(self):
        """Si el genre tenía `/ alta`, el resultado tiene `/ ALTA`."""
        result = compute_recalibration(
            "Tech House / alta",  # lowercase
            "Energia: ALTA (6.0/9)",
            (4.8, 5.8, 6.6),  # mismo nivel resultante
        )
        # Aunque el nivel del bucket no cambia, el case del sufijo sí (normaliza)
        assert result.new_genre == "Tech House / ALTA"
        # Motivo: hubo cambio (case del sufijo)
        assert result.motivo == "recalcular"


# --------------------------------------------------------------------------- #
# Dataclass RecalibrationResult
# --------------------------------------------------------------------------- #

class TestRecalibrationResultDataclass:
    def test_all_7_fields_present(self):
        """Dataclass tiene los 7 campos esperados."""
        r = RecalibrationResult(
            arousal=6.0,
            old_level="ALTA",
            new_level="MEDIA",
            old_genre="X / ALTA",
            new_genre="X / MEDIA",
            new_comment="Energia: MEDIA (6.0/9)",
            motivo="recalcular",
        )
        assert r.arousal == 6.0
        assert r.old_level == "ALTA"
        assert r.new_level == "MEDIA"
        assert r.motivo == "recalcular"

    def test_skip_case_allows_none_fields(self):
        """Para skip, arousal/old_level/new_level son None."""
        r = RecalibrationResult(
            arousal=None,
            old_level=None,
            new_level=None,
            old_genre="X",
            new_genre="X",
            new_comment="",
            motivo="skip: sin arousal en comment",
        )
        assert r.arousal is None
        assert r.old_level is None


# --------------------------------------------------------------------------- #
# Smoke: default thresholds funcionan
# --------------------------------------------------------------------------- #

def test_defaults_passable():
    """compute_recalibration con DEFAULT_ENERGY_THRESHOLDS funciona."""
    result = compute_recalibration(
        "Tech House / ALTA",
        "Energia: ALTA (6.0/9)",
        DEFAULT_ENERGY_THRESHOLDS,
    )
    # 6.0 con defaults (4.8, 5.8, 6.6) → ALTA → sin-cambio
    assert result.motivo == "sin-cambio"
