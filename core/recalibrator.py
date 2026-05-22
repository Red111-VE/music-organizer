"""Módulo 7: recalcula niveles de energía sin re-analizar el audio.

Cuando el usuario corre ``tag`` por primera vez, los umbrales por defecto
(``DEFAULT_ENERGY_THRESHOLDS = (4.8, 5.8, 6.6)``) son los del modelo emoMusic
crudo. En la mayoría de bibliotecas reales la distribución de arousal se
concentra en un rango mucho más estrecho — el resultado típico es todo en
``ALTA``, casi nada en ``BAJA``.

Este módulo permite reajustar los niveles aplicando umbrales nuevos al
``arousal`` numérico que ya quedó escrito en el ``comment``. Tarda <1 min
sobre cientos de pistas — vs. ~60–90 min de re-análisis con Essentia.

Casos de uso:

- **Custom**: el usuario pasa ``--thresholds B,M,A`` con valores específicos.
- **Auto-calibración** (``--auto-calibrate``):
  :func:`compute_calibrated_thresholds` saca los percentiles 25/50/75 de los
  arousals reales de la biblioteca y los aplica como umbrales. Resultado:
  distribución equilibrada por construcción (~25% en cada nivel).

Lógica pura: no toca filesystem ni mutagen. La lectura/escritura de tags
queda en ``cli/recalibrate.py`` (vía :mod:`core.tagger`).

Reusos clave:

- ``energy_bucket`` de :mod:`core.analyzer` — la dejamos libre de numpy
  precisamente para que recalibrator pueda importarla sin arrastrar TF.
- ``RE_FULL_COMMENT`` y ``RE_LEVEL_SUFFIX`` de :mod:`core.config`.
"""

from __future__ import annotations

from dataclasses import dataclass
from statistics import quantiles

from core.analyzer import energy_bucket
from core.config import (
    EnergyLevel,
    EnergyThresholds,
    RE_FULL_COMMENT,
    RE_LEVEL_SUFFIX,
)


@dataclass
class RecalibrationResult:
    """Resultado de aplicar umbrales nuevos a una pista que ya fue analizada.

    Cuando ``motivo == 'skip: sin arousal en comment'`` (el comment no parseó),
    los campos ``new_*`` quedan iguales a los ``old_*`` y ``arousal`` /
    ``new_level`` son ``None``. El CLI no debe escribir nada en ese caso.

    Cuando ``motivo == 'sin-cambio'`` el nivel y el genre quedan idénticos —
    el CLI puede saltar la escritura.

    Cuando ``motivo == 'recalcular'`` hay cambio en al menos uno de los dos
    (nivel o sufijo del genre). El CLI escribe ``new_genre`` + ``new_comment``.
    """
    arousal: float | None
    old_level: EnergyLevel | None
    new_level: EnergyLevel | None
    old_genre: str
    new_genre: str
    new_comment: str
    motivo: str


def parse_arousal_comment(
    comment: str,
) -> tuple[EnergyLevel, float, str, str] | None:
    """Parse ``"Energia: ALTA (6.0/9) | Genero: ..."`` →
    ``("ALTA", 6.0, "6.0", " | Genero: ...")``.

    Devuelve ``(level, arousal_float, arousal_raw, rest)``:

    - ``arousal_float``: para pasar a :func:`energy_bucket` (necesita ``float``).
    - ``arousal_raw``: el **string exacto** capturado por el regex. Se usa
      para reconstruir el comment sin perder precisión ni ceros finales
      (``"6.10"`` no se convierte en ``"6.1"``). Garantía fuerte: recalibrate
      nunca toca el número, solo el nivel.

    Devuelve ``None`` si el comment no tiene el formato esperado (faltante,
    editado por el usuario, formato viejo, etc.) — el caller decide qué hacer.

    Se exporta porque ``cli/recalibrate.py`` lo necesita para la fase de
    auto-calibración: barrer la biblioteca extrayendo arousals **antes** de
    decidir los umbrales.
    """
    match = RE_FULL_COMMENT.match(comment)
    if not match:
        return None
    level: EnergyLevel = match.group(1)  # type: ignore[assignment]
    arousal_raw = match.group(2)
    arousal = float(arousal_raw)
    rest = match.group(3)
    return level, arousal, arousal_raw, rest


def compute_calibrated_thresholds(arousals: list[float]) -> EnergyThresholds:
    """Calcula umbrales auto-calibrados como percentiles 25/50/75.

    ``statistics.quantiles(data, n=4)`` devuelve los 3 cuartiles Q1/Q2/Q3
    en orden ascendente — encajan directamente como ``(low, med, high)`` del
    triple :data:`EnergyThresholds`. Redondeados a 2 decimales para que los
    umbrales escritos al log/CSV sean legibles.

    Propaga ``StatisticsError`` si ``arousals`` tiene menos de 2 elementos.
    El CLI lo cacha y muestra el mensaje del usuario ("corre tag primero").
    """
    q = quantiles(arousals, n=4)
    return (round(q[0], 2), round(q[1], 2), round(q[2], 2))


def compute_recalibration(
    genre: str,
    comment: str,
    thresholds: EnergyThresholds,
) -> RecalibrationResult:
    """Aplica ``thresholds`` a una pista ya analizada. Idempotente: re-correr
    con los mismos umbrales devuelve ``motivo='sin-cambio'``.

    Comportamiento detallado:

    - Si el comment no parsea → ``'skip: sin arousal en comment'``, no se
      proponen cambios.
    - Reconstruye el comment con el nivel nuevo, **preservando el arousal
      numérico exacto y el resto** (típicamente `` | Genero: ...``).
    - Actualiza el sufijo `` / NIVEL`` del ``genre`` **solo si ya lo tenía**.
      No enriquece pistas que estaban sin enriquecer — eso es trabajo de
      :mod:`core.enricher`, no de este módulo.
    - Si nivel y genre quedan idénticos, ``'sin-cambio'``. Si cambia algo
      (incluso solo el case del sufijo, p.ej. ``/ alta`` → ``/ ALTA``),
      ``'recalcular'``.
    """
    parsed = parse_arousal_comment(comment)
    if parsed is None:
        return RecalibrationResult(
            arousal=None,
            old_level=None,
            new_level=None,
            old_genre=genre,
            new_genre=genre,
            new_comment=comment,
            motivo="skip: sin arousal en comment",
        )
    old_level, arousal, arousal_raw, rest = parsed

    # energy_bucket devuelve (nivel, valor_redondeado); solo necesitamos el
    # nivel. El arousal se preserva textual abajo.
    new_level, _ = energy_bucket(arousal, thresholds)

    # Reconstruir comment con el nivel nuevo. **El arousal se preserva
    # textual**: recalibrate solo cambia el nivel, nunca el número. Usar
    # arousal_raw (string capturado) en vez de f"{arousal}" evita el
    # round-trip float→str que elimina ceros finales ("6.10" → "6.1") o
    # introduce ruido decimal si en el futuro el analyzer cambia la
    # precisión escrita.
    new_comment = f"Energia: {new_level} ({arousal_raw}/9){rest}"

    # Actualizar el sufijo del genre SOLO si ya tenía uno. RE_LEVEL_SUFFIX
    # es case-insensitive: ``"Tech House / alta"`` se detecta y se reemplaza
    # por ``"Tech House / ALTA"`` (normaliza el case de paso).
    new_genre = genre
    if RE_LEVEL_SUFFIX.search(genre):
        base = RE_LEVEL_SUFFIX.sub("", genre).strip()
        new_genre = f"{base} / {new_level}"

    if new_level == old_level and new_genre == genre:
        motivo = "sin-cambio"
    else:
        motivo = "recalcular"

    return RecalibrationResult(
        arousal=arousal,
        old_level=old_level,
        new_level=new_level,
        old_genre=genre,
        new_genre=new_genre,
        new_comment=new_comment,
        motivo=motivo,
    )
