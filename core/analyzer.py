"""Paso 1 del pipeline: analiza una pista y devuelve géneros + energía.

Combina los dos cabezales cargados por :mod:`core.models`:

- **Género**: ``MAEST`` (embeddings) → ``genre_head`` (cabezal Discogs-400) →
  vector de 400 probabilidades → top-3 simplificado por :data:`GENRE_SIMPLIFY`.
- **Energía**: ``VGGish`` (embeddings) → ``energy_head`` (cabezal emoMusic) →
  ``arousal`` crudo (escala 1–9) → nivel discreto vía :func:`energy_bucket`.

El resultado es :class:`TrackAnalysis`, con los 7 campos que ``cli/tag.py``
necesita para el CSV de reporte y para construir el ``comment`` que
:mod:`core.tagger` escribe en cada archivo.

**Política de errores**: :func:`analyze_track` lanza libremente. Cualquier
fallo (audio corrupto, modelo crasheando) propaga al caller — típicamente
``cli/tag.py``, que registra ese archivo como error en el CSV y sigue con el
siguiente. Analyzer NO escribe tags ni CSVs; solo computa.

**Imports lazy**:

- **essentia / essentia.standard**: dentro de :func:`analyze_track` solo.
  Es lo pesado (arrastra TensorFlow).
- **numpy**: también lazy. ``simplify_genre`` y ``energy_bucket`` son
  puro Python; mantenerlos libres de numpy permite a :mod:`core.recalibrator`
  reusar :func:`energy_bucket` sin pagar numpy al importar.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from core.config import (
    DEFAULT_ENERGY_THRESHOLDS,
    GENRE_SIMPLIFY,
    EnergyLevel,
    EnergyThresholds,
)

if TYPE_CHECKING:
    from core.models import EssentiaModels


@dataclass
class TrackAnalysis:
    """Salida de :func:`analyze_track` para una pista.

    Todos los campos terminan en el reporte CSV de ``cli/tag.py`` y/o en el
    ``comment`` que :mod:`core.tagger` escribe dentro del archivo de audio.
    """
    genre: str                # simplificado: "Tech House"
    genre_raw: str            # crudo de Discogs: "Electronic---Tech House"
    genre_top3_text: str      # "Tech House 63% | House 43% | Techno 34%"
    genre_confidence: float   # probabilidad del top-1, en [0, 1]
    arousal: float            # arousal crudo (no redondeado), escala 1–9
    energy_level: EnergyLevel
    energy_value: float       # arousal redondeado a 1 decimal


def simplify_genre(label: str) -> str:
    """Colapsa una etiqueta cruda Discogs ('Electronic---Tech House') a la
    categoría DJ del usuario ('Tech House').

    Toma todo lo que está después del último '---', lo busca en lowercase en
    :data:`GENRE_SIMPLIFY`. Si la sub-etiqueta no está mapeada, devuelve el
    texto crudo después del '---' (preservando el case original de Discogs).
    """
    sub = label.split("---")[-1].strip()
    return GENRE_SIMPLIFY.get(sub.lower(), sub)


def energy_bucket(
    arousal: float,
    thresholds: EnergyThresholds = DEFAULT_ENERGY_THRESHOLDS,
) -> tuple[EnergyLevel, float]:
    """Convierte un ``arousal`` crudo en ``(nivel, valor_redondeado)``.

    Las comparaciones se hacen contra el ``arousal`` **crudo** (no contra el
    valor ya redondeado): así un arousal de ``4.79`` cae en BAJA aunque su
    representación redondeada (``4.8``) coincida con el umbral por defecto.

    Los umbrales son configurables para que ``recalibrate`` pueda aplicar
    percentiles calculados sobre la biblioteca real del usuario sin tener
    que re-analizar el audio.
    """
    low, med, high = thresholds
    level: EnergyLevel
    if arousal < low:
        level = "BAJA"
    elif arousal < med:
        level = "MEDIA"
    elif arousal < high:
        level = "ALTA"
    else:
        level = "MUY ALTA"
    return level, round(arousal, 1)


def analyze_track(
    path: Path,
    models: EssentiaModels,
    thresholds: EnergyThresholds = DEFAULT_ENERGY_THRESHOLDS,
) -> TrackAnalysis:
    """Analiza una pista: MAEST → top-3 género + VGGish/emoMusic → arousal.

    Lanza libremente: cualquier error de Essentia o de IO propaga al caller.
    No escribe tags ni CSVs.

    ``MonoLoader`` se instancia aquí (no en :class:`EssentiaModels`) porque
    necesita el ``filename=`` de cada pista — no es un modelo compartido.
    """
    import numpy as np
    from essentia import Pool
    from essentia.standard import MonoLoader

    # 1) Audio mono 16 kHz — lo que esperan tanto MAEST como VGGish.
    # Parámetros idénticos al script original; no cambiar sin recalibrar.
    audio = MonoLoader(
        filename=str(path),
        sampleRate=16000,
        resampleQuality=4,
    )()

    # 2) GÉNERO: embeddings MAEST → Pool → cabezal Discogs-400.
    maest_emb = models.maest(audio)
    pool = Pool()
    pool.set("embeddings", maest_emb)
    # output shape: (parches, 1, 1, 400) — promediar parches y aplastar
    # las dos dims unitarias para quedar con un vector (400,).
    # NO "simplificar" a axis=0: dejaría (1, 1, 400) y rompería argsort.
    g_pred = models.genre_head(pool)["PartitionedCall/Identity_1"].mean(
        axis=(0, 1, 2)
    )

    top3_idx = list(np.argsort(g_pred)[::-1][:3])
    genre_raw = models.genre_labels[top3_idx[0]]
    genre = simplify_genre(genre_raw)
    genre_confidence = float(g_pred[top3_idx[0]])
    genre_top3_text = " | ".join(
        f"{simplify_genre(models.genre_labels[j])} "
        f"{int(round(float(g_pred[j]) * 100))}%"
        for j in top3_idx
    )

    # 3) ENERGÍA: embeddings VGGish → cabezal emoMusic.
    vggish_emb = models.vggish(audio)
    e_pred = models.energy_head(vggish_emb).mean(axis=0)
    # arousal está en e_pred[1]. emoMusic devuelve [valence, arousal] en
    # escala 1-9; usamos SOLO arousal. NO es e_pred[0] (eso es valence).
    # Confundir los índices invalida la calibración entera de la biblioteca.
    arousal = float(e_pred[1])
    energy_level, energy_value = energy_bucket(arousal, thresholds)

    return TrackAnalysis(
        genre=genre,
        genre_raw=genre_raw,
        genre_top3_text=genre_top3_text,
        genre_confidence=genre_confidence,
        arousal=arousal,
        energy_level=energy_level,
        energy_value=energy_value,
    )
