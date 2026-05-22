"""Constantes y tipos compartidos entre los módulos de ``core/`` y ``cli/``.

Este archivo no importa nada de los demás módulos del proyecto. Es la base.
"""

from __future__ import annotations

import re
from typing import Final, Literal


# --------------------------------------------------------------------------- #
# Tipos
# --------------------------------------------------------------------------- #

# Los strings literales son los mismos que se escriben dentro de los tags
# (FLAC.comment, ID3 COMM, MP4 \xa9cmt). Mantenemos el tipo como Literal
# para evitar la fricción de convertir Enum.value ↔ str en cada lectura/escritura.
EnergyLevel = Literal["BAJA", "MEDIA", "ALTA", "MUY ALTA"]

# Triple de umbrales (low/med/high) para `arousal` en escala 1–9.
EnergyThresholds = tuple[float, float, float]


# --------------------------------------------------------------------------- #
# Archivos de audio
# --------------------------------------------------------------------------- #

AUDIO_EXTS: Final[frozenset[str]] = frozenset({
    ".mp3", ".flac", ".wav", ".aiff", ".aif", ".m4a", ".aac", ".ogg",
})

# Prefijo de sidecars macOS/exFAT (resource forks tipo `._cancion.flac`).
SIDECAR_PREFIX: Final[str] = "._"

# Prefijo de subcarpetas a ignorar al recorrer (ej. `_REVISAR`, `_SIN_TAG`,
# `_Playlists`). Se aplica a *componentes intermedios* del path relativo al
# origen, no al nombre del propio origen.
HIDDEN_DIR_PREFIX: Final[str] = "_"


# --------------------------------------------------------------------------- #
# Niveles de energía y umbrales
# --------------------------------------------------------------------------- #

# Orden ascendente. Usado para construir regex y para iterar el resumen
# de distribución en orden estable.
LEVELS_TUPLE: Final[tuple[EnergyLevel, ...]] = (
    "BAJA", "MEDIA", "ALTA", "MUY ALTA",
)

# Umbrales por defecto del modelo emoMusic original (arousal 1–9). En la
# mayoría de bibliotecas reales la distribución de arousal se concentra en un
# rango mucho más estrecho que [4.8, 6.6], y estos defaults producen
# distribuciones muy desbalanceadas (típicamente todo en ALTA, casi nada en
# BAJA).
#
# Recomendado: después de la primera corrida de `tag`, ejecutar
# `music-organizer recalibrate --auto` para ajustar los umbrales a los
# percentiles 25/50/75 de tu propia biblioteca.
#
#   arousal <  ENERGY_THRESHOLD_LOW  → BAJA
#   arousal <  ENERGY_THRESHOLD_MED  → MEDIA
#   arousal <  ENERGY_THRESHOLD_HIGH → ALTA
#   arousal >= ENERGY_THRESHOLD_HIGH → MUY ALTA
ENERGY_THRESHOLD_LOW: Final[float] = 4.8
ENERGY_THRESHOLD_MED: Final[float] = 5.8
ENERGY_THRESHOLD_HIGH: Final[float] = 6.6

DEFAULT_ENERGY_THRESHOLDS: Final[EnergyThresholds] = (
    ENERGY_THRESHOLD_LOW,
    ENERGY_THRESHOLD_MED,
    ENERGY_THRESHOLD_HIGH,
)


# --------------------------------------------------------------------------- #
# Regex compartidos
# --------------------------------------------------------------------------- #

# "Energia: ALTA (6.0/9) | Genero: ..."  →  grupo 1 = nivel
# Usado por `enricher` para extraer el nivel de energía del comment.
RE_ENERGY_FROM_COMMENT: Final[re.Pattern[str]] = re.compile(
    r"^Energia:\s*(" + "|".join(LEVELS_TUPLE) + r")\b"
)

# Sufijo " / NIVEL" al final del tag `genre`.
# Case-insensitive porque algún editor externo podría haber cambiado el case.
# Ancla a fin de string, así que no confunde el " / " interno de géneros como
# "Disco / Nu-Disco" o "Minimal / Deep Tech": solo captura el último " / NIVEL"
# si el grupo coincide con un nivel válido.
RE_LEVEL_SUFFIX: Final[re.Pattern[str]] = re.compile(
    r"\s*/\s*(" + "|".join(LEVELS_TUPLE) + r")\s*$",
    re.IGNORECASE,
)

# Comment completo del paso 1, con `arousal` numérico expuesto.
#   grupo 1 = nivel actual
#   grupo 2 = arousal (float, ej. "6.0")
#   grupo 3 = resto (" | Genero: ...", puede estar vacío)
# Usado por `recalibrator` para recalcular niveles sin re-analizar audio.
RE_FULL_COMMENT: Final[re.Pattern[str]] = re.compile(
    r"^Energia:\s*(" + "|".join(LEVELS_TUPLE) + r")\s*\(([\d.]+)/9\)(.*)$"
)


# --------------------------------------------------------------------------- #
# Mapa de simplificación de géneros
# --------------------------------------------------------------------------- #
# Las 400 etiquetas Discogs llegan crudas como "Electronic---Tech House".
# Aquí colapsamos al estilo que de verdad usa un DJ. Si la sub-etiqueta no
# está en el mapa, se usa tal cual el texto después de "---".
#
# Las claves son lowercase (la lookup se hace con `.lower()`).
# Los valores son la etiqueta final escrita al tag `genre`.

GENRE_SIMPLIFY: Final[dict[str, str]] = {
    "tech house":        "Tech House",
    "deep house":        "Deep House",
    "house":             "House",
    "minimal":           "Minimal / Deep Tech",
    "minimal techno":    "Minimal / Deep Tech",
    "micro house":       "Minimal / Deep Tech",
    "techno":            "Techno",
    "progressive house": "Progressive House",
    "electro house":     "Electro House",
    "disco":             "Disco / Nu-Disco",
    "nu-disco":          "Disco / Nu-Disco",
    "italo-disco":       "Disco / Nu-Disco",
    "breakbeat":         "Breaks",
    "uk garage":         "Garage / Bass",
    "garage house":      "Garage / Bass",
    "bassline":          "Garage / Bass",
    "drum n bass":       "Drum & Bass",
    "electro":           "Electro",
    "acid house":        "Acid House",
    "ambient":           "Ambient / Downtempo",
    "downtempo":         "Ambient / Downtempo",
    "trance":            "Trance",
    "afro house":        "Afro House",
}


# --------------------------------------------------------------------------- #
# Modelos Essentia
# --------------------------------------------------------------------------- #
# Nombres de archivo esperados dentro de la carpeta `--models`.
# Se descargan con `scripts/download_models.sh` (ver docs/MODELS.md).

# Embeddings MAEST 30s (Transformer afinado para música, ~348 MB).
EMB_MAEST: Final[str] = "discogs-maest-30s-pw-2.pb"

# Cabezal Discogs-400 que opera sobre los embeddings de MAEST.
GENRE_HEAD: Final[str] = "genre_discogs400-discogs-maest-30s-pw-1.pb"

# Lista oficial de las 400 etiquetas de género (mismo orden que la cabeza).
GENRE_LABELS: Final[str] = "genre_discogs400-discogs-maest-30s-pw-1.json"

# Embeddings VGGish (input al cabezal emoMusic).
EMB_VGGISH: Final[str] = "audioset-vggish-3.pb"

# Cabezal emoMusic (arousal/valence en escala 1–9; usamos el arousal).
ENERGY_HEAD: Final[str] = "emomusic-audioset-vggish-2.pb"

# Tupla con los 5 archivos esperados — útil para validar que la carpeta de
# modelos esté completa en un solo barrido.
MODEL_FILES: Final[tuple[str, ...]] = (
    EMB_MAEST, GENRE_HEAD, GENRE_LABELS, EMB_VGGISH, ENERGY_HEAD,
)


# --------------------------------------------------------------------------- #
# Reportes CSV
# --------------------------------------------------------------------------- #
# Se escriben en la carpeta de origen (no en el repo). Mantenemos los nombres
# de los scripts originales para que el usuario pueda diff-ar contra reportes
# de corridas pasadas.

CSV_TAG_REPORT: Final[str] = "_REPORTE_GENERO.csv"
CSV_ENRICH_REPORT: Final[str] = "_REPORTE_ENRIQUECER.csv"
CSV_REVERT_REPORT: Final[str] = "_REPORTE_REVERTIR.csv"
CSV_ORGANIZE_REPORT: Final[str] = "_REPORTE_ORGANIZACION.csv"
CSV_RECALIBRATE_REPORT: Final[str] = "_REPORTE_RECALCULO.csv"
