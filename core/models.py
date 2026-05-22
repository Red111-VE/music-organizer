"""Carga de los modelos Essentia del pipeline.

Centraliza la inicialización de los 4 modelos TensorFlow que el paso 1
(``analyzer``) necesita, más la lista oficial de las 400 etiquetas Discogs:

- **Género**: MAEST 30s (embeddings) + cabezal Discogs-400.
- **Energía**: VGGish (embeddings) + cabezal emoMusic (arousal/valence en 1–9).

La carga toma ~10–15 s en un MacBook Air M4 (la mayoría es TensorFlow
inicializándose). Por eso se separan dos operaciones:

- :func:`validate_models_dir` solo chequea que los 5 archivos estén en disco.
  No importa Essentia. Permite a la CLI fallar rápido si falta algo, antes de
  pagar el costo de cargar TF.
- :func:`load_models` carga los modelos. Aquí sí se importa Essentia.

Los imports de Essentia son **lazy** dentro de :func:`load_models` (mismo
principio que con mutagen en :mod:`core.tagger`): Essentia es enorme y arrastra
TensorFlow, así que solo se carga cuando realmente se va a usar.

**Nota sobre ``MonoLoader``**: no aparece en :class:`EssentiaModels` porque
no es un modelo compartido — se instancia por archivo con ``filename=`` en
:mod:`core.analyzer`. Aquí solo guardamos los modelos que se cargan una vez
y se reutilizan.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from core.config import (
    EMB_MAEST,
    EMB_VGGISH,
    ENERGY_HEAD,
    GENRE_HEAD,
    GENRE_LABELS,
    MODEL_FILES,
)

if TYPE_CHECKING:
    # Solo para type hints; no se ejecuta en runtime gracias a
    # ``from __future__ import annotations``.
    from essentia.standard import (
        TensorflowPredict,
        TensorflowPredict2D,
        TensorflowPredictMAEST,
        TensorflowPredictVGGish,
    )


@dataclass
class EssentiaModels:
    """Modelos Essentia ya cargados, listos para usar en :mod:`core.analyzer`.

    Los 4 modelos TF se cargan una vez y se reutilizan a través de todas las
    pistas del batch. ``genre_labels`` es la lista oficial de 400 strings
    Discogs en el orden que espera el cabezal Discogs-400.
    """
    maest: TensorflowPredictMAEST          # embeddings MAEST 30s
    genre_head: TensorflowPredict          # cabezal Discogs-400
    vggish: TensorflowPredictVGGish        # embeddings VGGish
    energy_head: TensorflowPredict2D       # cabezal emoMusic (arousal/valence)
    genre_labels: list[str]                # 400 etiquetas Discogs


def validate_models_dir(models_dir: Path) -> list[str]:
    """Devuelve la lista de nombres de archivo que faltan en ``models_dir``.

    Lista vacía = todo presente, listo para :func:`load_models`. Solo toca
    el filesystem (``Path.is_file()``); no importa Essentia.
    """
    return [name for name in MODEL_FILES if not (models_dir / name).is_file()]


def load_models(models_dir: Path) -> EssentiaModels:
    """Carga los 4 modelos TF + las 400 etiquetas Discogs.

    Tarda ~10–15 s (la mayoría es TensorFlow inicializándose). Valida primero
    que estén los 5 archivos en ``models_dir`` y lanza
    :class:`FileNotFoundError` con la lista de los que faltan si algo no está.
    """
    missing = validate_models_dir(models_dir)
    if missing:
        raise FileNotFoundError(
            "Faltan archivos en la carpeta de modelos:\n  "
            + "\n  ".join(missing)
            + "\n\nDescárgalos de https://essentia.upf.edu/models/"
        )

    # Las 400 etiquetas: stdlib, no necesita Essentia. El JSON publicado por
    # Essentia tiene la lista bajo la clave 'classes'.
    with (models_dir / GENRE_LABELS).open(encoding="utf-8") as f:
        labels: list[str] = json.load(f)["classes"]

    # Essentia (y TensorFlow detrás) es lo pesado del proyecto. Lazy.
    from essentia.standard import (
        TensorflowPredict,
        TensorflowPredict2D,
        TensorflowPredictMAEST,
        TensorflowPredictVGGish,
    )

    # GÉNERO: MAEST embeddings -> cabezal Discogs-400.
    # Parámetros idénticos al script original (probados sobre 471 pistas).
    maest = TensorflowPredictMAEST(
        graphFilename=str(models_dir / EMB_MAEST),
        output="PartitionedCall/Identity_12",
    )
    genre_head = TensorflowPredict(
        graphFilename=str(models_dir / GENRE_HEAD),
        inputs=["embeddings"],
        outputs=["PartitionedCall/Identity_1"],
    )

    # ENERGÍA: VGGish embeddings -> cabezal emoMusic.
    # El cabezal devuelve [valence, arousal] en escala 1–9; analyzer usa
    # solo arousal (índice 1).
    vggish = TensorflowPredictVGGish(
        graphFilename=str(models_dir / EMB_VGGISH),
        output="model/vggish/embeddings",
    )
    energy_head = TensorflowPredict2D(
        graphFilename=str(models_dir / ENERGY_HEAD),
        output="model/Identity",
    )

    return EssentiaModels(
        maest=maest,
        genre_head=genre_head,
        vggish=vggish,
        energy_head=energy_head,
        genre_labels=labels,
    )
