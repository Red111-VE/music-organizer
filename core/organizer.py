"""Paso 3 del pipeline: organiza archivos por género + energía.

Toma un origen y reorganiza los archivos en una estructura de dos niveles
bajo el destino:

::

    <dest_base>/
        Tech House/
            ALTA/   <- archivo.flac
            MEDIA/
        Disco - Nu-Disco/   <- '/' del género se reemplaza por ' - '
            ALTA/
        _SIN_TAG/   <- archivos sin tag genre
        Tech House/
            _SIN_NIVEL/   <- género presente pero sin sufijo / NIVEL

Default COPIA (preserva el origen); ``move=True`` mueve. No renombra archivos.

Separación de responsabilidades:

- :func:`parse_enriched_genre` y :func:`compute_target` son **lógica pura
  de strings/paths** — testeables sin tocar disco, sin dependencias pesadas.
- :func:`organize_file` ejecuta la operación de filesystem (``shutil``).
- :func:`collect_audio_files` recorre el árbol y aplica los filtros del
  script original (sidecars, dest interno, subcarpetas con prefijo ``_``).

**Capa ``Lossless/``**: el script original cuelga las carpetas de género bajo
``<destino>/Lossless/`` por convención (futura coexistencia con otros
formatos), y ofrece ``--sin-lossless`` para colgarlas directamente del
destino. Esa decisión vive en la CLI: aquí ``compute_target`` solo recibe el
``dest_base`` ya resuelto (con o sin la capa ``Lossless/``).

No requiere essentia ni mutagen. ``shutil`` es stdlib.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from core.config import (
    AUDIO_EXTS,
    HIDDEN_DIR_PREFIX,
    RE_LEVEL_SUFFIX,
    SIDECAR_PREFIX,
)
from core.tagger import safe_dirname


OrganizeStatus = Literal["ok", "skip", "error"]


@dataclass
class OrganizeResult:
    """Resultado de organizar un archivo, listo para volcar al CSV.

    ``genre_tag`` se incluye aunque :func:`organize_file` no lo usa para la
    operación de filesystem — lo pasa como data plumbing para que el CLI
    arme la fila del CSV en una sola pasada sin tracking adicional.
    """
    source: Path
    target: Path
    status: OrganizeStatus
    motivo: str
    genre_tag: str


# --------------------------------------------------------------------------- #
# Lógica pura (sin filesystem)
# --------------------------------------------------------------------------- #

def parse_enriched_genre(genre_tag: str) -> tuple[str, str | None]:
    """De ``"Tech House / ALTA"`` → ``("Tech House", "ALTA")``.

    Si no hay sufijo de nivel, devuelve ``(genre_tag, None)``.

    ``RE_LEVEL_SUFFIX`` está anclado a ``$``, así que en géneros compound
    (``"Disco / Nu-Disco / ALTA"``) solo captura el último ``/ NIVEL`` cuando
    coincide con un nivel válido — ``"Disco / Nu-Disco"`` (sin nivel al final)
    devuelve ``("Disco / Nu-Disco", None)``.

    Casos patológicos: ``""`` → ``("", None)``; ``"/ ALTA"`` →
    ``("", "ALTA")`` (base queda vacío después del strip).

    El nivel se devuelve **en mayúsculas** aunque ``RE_LEVEL_SUFFIX`` sea
    case-insensitive. Razón: un archivo editado manualmente a
    ``"Tech House / alta"`` debe producir el mismo nombre de carpeta
    (``ALTA``) que la versión canónica que escribe :mod:`core.analyzer`.
    Sin esta normalización, ``compute_target`` crearía carpetas
    ``alta``/``Alta``/``ALTA`` separadas y fragmentaría la biblioteca.
    Esta es una mejora deliberada sobre el organizador original, que usaba
    un regex case-sensitive (en él, ``"/ alta"`` caía en ``_SIN_NIVEL``).
    """
    if not genre_tag:
        return "", None
    match = RE_LEVEL_SUFFIX.search(genre_tag)
    if not match:
        return genre_tag, None
    level = match.group(1).upper()
    base = RE_LEVEL_SUFFIX.sub("", genre_tag).strip()
    return base, level


def compute_target(
    source: Path,
    genre_tag: str,
    dest_base: Path,
    flat: bool = False,
) -> tuple[Path, str]:
    """Calcula la ruta destino para ``source`` y el motivo del cálculo.

    Casos (los motivos son los exactos del script original — el CSV los lee):

    - ``genre_tag`` vacío o resultando en base vacía →
      ``<dest_base>/_SIN_TAG/<filename>``, motivo ``"sin tag genre"``.
    - ``flat=True`` (``--solo-genero``) → ``<dest_base>/<genre>/<filename>``,
      motivo ``"ok"``. La energía queda solo en el tag.
    - Género presente sin sufijo de nivel →
      ``<dest_base>/<genre>/_SIN_NIVEL/<filename>``,
      motivo ``"genre sin sufijo / NIVEL"``.
    - Normal → ``<dest_base>/<genre>/<nivel>/<filename>``, motivo ``"ok"``.
    """
    base, level = parse_enriched_genre(genre_tag)
    if not base:
        return dest_base / "_SIN_TAG" / source.name, "sin tag genre"
    base_dir = dest_base / safe_dirname(base)
    if flat:
        return base_dir / source.name, "ok"
    if level is None:
        return base_dir / "_SIN_NIVEL" / source.name, "genre sin sufijo / NIVEL"
    return base_dir / level / source.name, "ok"


# --------------------------------------------------------------------------- #
# Operación de filesystem
# --------------------------------------------------------------------------- #

def organize_file(
    source: Path,
    target: Path,
    genre_tag: str,
    base_motivo: str,
    move: bool = False,
    dry_run: bool = False,
) -> OrganizeResult:
    """Ejecuta copy/move de ``source`` a ``target``.

    - ``dry_run=True``: no toca el filesystem; devuelve ``status='ok'`` con el
      ``base_motivo`` intacto. El CLI decide cómo presentar el dry-run en el
      CSV (típicamente status ``'simulado'``).
    - Éxito: crea ``target.parent`` si no existe; usa ``shutil.copy2``
      (preserva mtime/permisos) o ``shutil.move``. ``status='ok'``,
      motivo preservado.
    - Error: ``status='error'``, motivo ``"<base_motivo> | <error>"``.
      Mantiene la causa de targeting (ej. ``"sin tag genre"``) junto al
      mensaje del fallo de IO, como hacía el script original.

    ``base_motivo`` y ``genre_tag`` son data plumbing — :class:`OrganizeResult`
    los necesita completos para que el CLI escriba el CSV sin estado extra.
    """
    if dry_run:
        return OrganizeResult(
            source=source,
            target=target,
            status="ok",
            motivo=base_motivo,
            genre_tag=genre_tag,
        )
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if move:
            shutil.move(str(source), str(target))
        else:
            shutil.copy2(str(source), str(target))
        return OrganizeResult(
            source=source,
            target=target,
            status="ok",
            motivo=base_motivo,
            genre_tag=genre_tag,
        )
    except (OSError, shutil.Error) as e:
        return OrganizeResult(
            source=source,
            target=target,
            status="error",
            motivo=f"{base_motivo} | {e}",
            genre_tag=genre_tag,
        )


# --------------------------------------------------------------------------- #
# Recorrido de filesystem
# --------------------------------------------------------------------------- #

def collect_audio_files(source_root: Path, dest_root: Path) -> list[Path]:
    """Devuelve la lista ordenada de archivos de audio bajo ``source_root``
    elegibles para organizar.

    Filtros (replicados textuales del script ``organizar_genero_energia.py``
    original — es la parte más delicada, no simplificar):

    1. Solo archivos (no directorios) con extensión en :data:`AUDIO_EXTS`.
    2. Excluye sidecars macOS/exFAT (nombre empieza con :data:`SIDECAR_PREFIX`,
       ``"._"``). Son resource forks que aparecen al copiar a/desde HFS+/exFAT.
    3. Excluye archivos que ya estén bajo ``dest_root`` — evita re-procesar
       archivos organizados en una corrida previa cuando dest queda dentro
       de source.
    4. Excluye archivos cuyo path relativo a ``source_root`` tiene algún
       componente intermedio que empieza con :data:`HIDDEN_DIR_PREFIX`
       (``"_"``). Filtra carpetas tipo ``_REVISAR``, ``_SIN_TAG``,
       ``_Playlists`` de corridas anteriores. El propio ``source_root`` NO
       se mira (``parts[:-1]`` descarta el filename; los componentes
       comparados son solo los intermedios).
    """
    result: list[Path] = []
    for path in source_root.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() not in AUDIO_EXTS:
            continue
        if path.name.startswith(SIDECAR_PREFIX):
            continue
        if dest_root in path.parents:
            continue
        rel = path.relative_to(source_root)
        if any(part.startswith(HIDDEN_DIR_PREFIX) for part in rel.parts[:-1]):
            continue
        result.append(path)
    result.sort()
    return result
