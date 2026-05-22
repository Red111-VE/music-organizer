"""Tests para :mod:`core.enricher`: paridad con ``calcular_objetivo``.

Lógica pura de strings sobre ``(genre, comment, revert)``. No toca
filesystem, mutagen ni essentia. Estos tests son la fuente de verdad de
que el output del enricher es **idéntico** al del script original
``enriquecer_genero.py``.
"""

from __future__ import annotations

import pytest

from core.enricher import compute_enriched_genre


# --------------------------------------------------------------------------- #
# Paridad con el script original: matriz de casos
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "genre,comment,revert,expected_target,expected_motivo",
    [
        # === ENRIQUECER (revert=False) =====================================
        pytest.param(
            "Tech House",
            "Energia: ALTA (6.0/9) | Genero: Tech House 63%",
            False,
            "Tech House / ALTA",
            "enriquecer",
            id="enrich-fresh",
        ),
        pytest.param(
            "Tech House / ALTA",
            "Energia: ALTA (6.0/9)",
            False,
            "Tech House / ALTA",
            "sin-cambio",
            id="enrich-idempotent",
        ),
        pytest.param(
            "Tech House / MEDIA",
            "Energia: ALTA (6.0/9)",
            False,
            "Tech House / ALTA",
            "enriquecer",
            id="enrich-rebucket",
        ),
        pytest.param(
            "Disco / Nu-Disco",
            "Energia: MEDIA (5.0/9)",
            False,
            "Disco / Nu-Disco / MEDIA",
            "enriquecer",
            id="enrich-compound-genre",
        ),
        pytest.param(
            "Disco / Nu-Disco / ALTA",
            "Energia: MEDIA (5.0/9)",
            False,
            "Disco / Nu-Disco / MEDIA",
            "enriquecer",
            id="enrich-compound-rebucket",
        ),
        pytest.param(
            "",
            "Energia: ALTA (6.0/9)",
            False,
            "",
            "skip: sin tag de genero",
            id="skip-no-genre",
        ),
        pytest.param(
            "Tech House",
            "",
            False,
            "Tech House",
            "skip: sin comment con Energia",
            id="skip-no-comment",
        ),
        pytest.param(
            "Tech House",
            "Otra cosa",
            False,
            "Tech House",
            "skip: comment no empieza con Energia: <NIVEL>",
            id="skip-comment-wrong-format",
        ),
        pytest.param(
            "Tech House",
            "energia: ALTA (6.0/9)",  # 'e' minúscula
            False,
            "Tech House",
            "skip: comment no empieza con Energia: <NIVEL>",
            id="skip-comment-lowercase-energia",
        ),
        pytest.param(
            "Tech House",
            "Energia: MUY ALTA (8.0/9)",
            False,
            "Tech House / MUY ALTA",
            "enriquecer",
            id="enrich-muy-alta",
        ),

        # === REVERTIR (revert=True) ========================================
        pytest.param(
            "Tech House / ALTA",
            "",
            True,
            "Tech House",
            "revertir",
            id="revert-normal",
        ),
        pytest.param(
            "Tech House",
            "",
            True,
            "Tech House",
            "sin-cambio",
            id="revert-no-suffix",
        ),
        pytest.param(
            "/ ALTA",
            "",
            True,
            "/ ALTA",
            "skip: revertir dejaria genre vacio",
            id="revert-would-empty",
        ),
        pytest.param(
            "Disco / Nu-Disco / ALTA",
            "",
            True,
            "Disco / Nu-Disco",
            "revertir",
            id="revert-compound",
        ),
        pytest.param(
            "Disco / Nu-Disco",
            "",
            True,
            "Disco / Nu-Disco",
            "sin-cambio",
            id="revert-compound-no-suffix",
        ),
        pytest.param(
            "Tech House / alta",  # lowercase, RE_LEVEL_SUFFIX es case-insensitive
            "",
            True,
            "Tech House",
            "revertir",
            id="revert-lowercase-suffix",
        ),
        pytest.param(
            "Tech House / muy alta",
            "",
            True,
            "Tech House",
            "revertir",
            id="revert-multi-word-lowercase",
        ),
    ],
)
def test_compute_enriched_genre(
    genre: str,
    comment: str,
    revert: bool,
    expected_target: str,
    expected_motivo: str,
) -> None:
    target, motivo = compute_enriched_genre(genre, comment, revert=revert)
    assert target == expected_target
    assert motivo == expected_motivo


# --------------------------------------------------------------------------- #
# Propiedades: idempotencia y reversibilidad
# --------------------------------------------------------------------------- #

class TestIdempotency:
    """Re-correr ``enrich`` sobre un input ya enriquecido no encadena niveles."""

    def test_double_enrich_same_level(self):
        """Doble enrich con el mismo nivel: segunda corrida es sin-cambio."""
        target1, motivo1 = compute_enriched_genre(
            "Tech House", "Energia: ALTA (6.0/9)",
        )
        assert target1 == "Tech House / ALTA"
        assert motivo1 == "enriquecer"

        target2, motivo2 = compute_enriched_genre(
            target1, "Energia: ALTA (6.0/9)",
        )
        # NO encadena " / ALTA / ALTA"
        assert target2 == "Tech House / ALTA"
        assert motivo2 == "sin-cambio"

    def test_enrich_with_different_level_replaces(self):
        """Re-enrich con nivel distinto reemplaza el sufijo, no encadena."""
        target, motivo = compute_enriched_genre(
            "Tech House / ALTA", "Energia: MEDIA (5.0/9)",
        )
        assert target == "Tech House / MEDIA"
        assert motivo == "enriquecer"

    def test_enrich_compound_with_different_level(self):
        """Idempotencia también vale para géneros compound con / interno."""
        target, _ = compute_enriched_genre(
            "Disco / Nu-Disco / ALTA", "Energia: MEDIA (5.0/9)",
        )
        assert target == "Disco / Nu-Disco / MEDIA"


class TestReversibility:
    """``enrich`` seguido de ``revert`` vuelve al genre original."""

    def test_round_trip_simple(self):
        original = "Tech House"
        enriched, _ = compute_enriched_genre(original, "Energia: ALTA (6.0/9)")
        assert enriched == "Tech House / ALTA"

        reverted, motivo = compute_enriched_genre(enriched, "", revert=True)
        assert reverted == original
        assert motivo == "revertir"

    def test_round_trip_compound(self):
        original = "Disco / Nu-Disco"
        enriched, _ = compute_enriched_genre(
            original, "Energia: MEDIA (5.0/9)",
        )
        assert enriched == "Disco / Nu-Disco / MEDIA"

        reverted, motivo = compute_enriched_genre(enriched, "", revert=True)
        assert reverted == original
        assert motivo == "revertir"

    def test_round_trip_muy_alta(self):
        """Reversibilidad con multi-word level (MUY ALTA)."""
        original = "Techno"
        enriched, _ = compute_enriched_genre(
            original, "Energia: MUY ALTA (7.5/9)",
        )
        assert enriched == "Techno / MUY ALTA"

        reverted, _ = compute_enriched_genre(enriched, "", revert=True)
        assert reverted == original
