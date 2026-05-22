"""Lectura y escritura unificada de tags ``genre`` y ``comment``.

Centraliza todas las idas y vueltas con mutagen. Reglas por familia de contenedor:

- **FLAC**: Vorbis comments con claves ``genre`` / ``comment``.
- **MP3, AIFF, AIF, WAV**: ID3 directo (``TCON`` para genre, ``COMM:energia:eng``
  para comment). **No se usa ``MutagenFile()``** porque algunos WAV traen el
  ID3 al inicio del archivo (estilo MP3, no dentro de un chunk RIFF) y
  ``MutagenFile()`` los confunde con MP3 — falla con
  ``"can't sync to MPEG frame"``. ``ID3(path)`` los abre bien.
- **M4A, AAC**: MP4 atoms (``\\xa9gen`` para genre, ``\\xa9cmt`` para comment).

OGG está en :data:`config.AUDIO_EXTS` para enumeración del filesystem (igual
que en los scripts originales), pero las funciones de lectura devuelven vacío
y las de escritura devuelven ``"formato no soportado para escritura: .ogg"``.
Mismo comportamiento que el código de referencia. Se añadirá soporte real con
tests si surge la necesidad.

Las funciones de lectura nunca lanzan: cualquier corrupción o tag ausente se
traduce a strings vacíos. Las de escritura devuelven ``(ok, motivo)`` para
que el caller pueda registrarlo en el reporte CSV.

Los imports de mutagen son **lazy** (por rama de extensión) para que módulos
ligeros — como el futuro ``web/`` o utilidades que solo usan
:func:`safe_dirname` — no carguen mutagen entero al importar este archivo.

**Sobre ``audio: Any``**: cada rama por extensión asigna a ``audio`` un objeto
mutagen de tipo distinto (FLAC / ID3 / MP4) y retorna temprano. Mypy no entiende
los early-returns, así que sin la anotación ``Any`` flagea cada reasignación
como "type incompatible". Anotamos ``Any`` una vez por función — los métodos
de cada objeto mutagen están testeados en :mod:`tests.test_tagger`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


# NOTA sobre los imports: ``mutagen/__init__.py`` carga los parsers de
# formato (FLAC, ID3, MP4, Vorbis) transitivamente, así que **ningún**
# import de mutagen — ni siquiera ``MutagenError`` — va al top del módulo.
# Cada función pública hace su propio import lazy. Esto deja
# ``from core.tagger import safe_dirname`` totalmente libre de mutagen.


# Marcador con el que `analyzer` empieza el comment. La lectura ID3 filtra por
# este prefijo (no por desc='energia') para tolerar archivos creados por
# versiones previas del script o por otras herramientas del usuario.
_COMMENT_MARKER = "Energia:"

# COMM frame que escribimos en ID3. Esta combinación desc/lang es la que
# `delall` borra antes de reescribir, así no pisamos comentarios del usuario
# en otros idiomas o con otra descripción.
_COMM_DESC = "energia"
_COMM_LANG = "eng"

# Familias de contenedor según extensión. WAV va con ID3 (no con WAVE/MutagenFile).
_EXTS_FLAC = frozenset({".flac"})
_EXTS_ID3 = frozenset({".mp3", ".aiff", ".aif", ".wav"})
_EXTS_MP4 = frozenset({".m4a", ".aac"})


@dataclass
class TrackTags:
    """Snapshot de los dos tags que toca el pipeline. Strings vacíos = ausentes."""
    genre: str = ""
    comment: str = ""


# --------------------------------------------------------------------------- #
# Lectura
# --------------------------------------------------------------------------- #

def read_tags(path: Path) -> TrackTags:
    """Lee ``genre`` y ``comment`` del archivo. Defensiva: devuelve
    :class:`TrackTags` con campos vacíos para formatos no soportados,
    archivos corruptos, o tags ausentes. Nunca lanza."""
    from mutagen import MutagenError
    audio: Any  # FLAC | MP4 según la rama; mypy no entiende los early-returns
    ext = path.suffix.lower()
    try:
        if ext in _EXTS_FLAC:
            from mutagen.flac import FLAC
            audio = FLAC(str(path))
            return TrackTags(
                genre=_first_str(audio.get("genre")),
                comment=_first_str(audio.get("comment")),
            )

        if ext in _EXTS_ID3:
            return _read_id3(path)

        if ext in _EXTS_MP4:
            from mutagen.mp4 import MP4
            audio = MP4(str(path))
            return TrackTags(
                genre=_first_str(audio.get("\xa9gen")),
                comment=_first_str(audio.get("\xa9cmt")),
            )
    except (MutagenError, OSError):
        return TrackTags()

    return TrackTags()


def _read_id3(path: Path) -> TrackTags:
    """Lectura ID3. Busca el COMM cuyo texto empiece con el marcador
    (puede haber otros COMM con notas del usuario en distintos idiomas).

    Solo distingue el caso legítimo de "archivo sin header ID3 todavía"
    (devuelve vacío). Corrupción real propaga al ``except`` exterior de
    :func:`read_tags`, así no se confunden ambos casos en un solo handler.
    """
    from mutagen.id3 import ID3, ID3NoHeaderError
    try:
        audio = ID3(str(path))
    except ID3NoHeaderError:
        return TrackTags()

    genre = ""
    tcon = audio.get("TCON")
    if tcon and getattr(tcon, "text", None):
        genre = str(tcon.text[0])

    comment = ""
    for key, frame in audio.items():
        if not key.startswith("COMM"):
            continue
        text = (
            str(frame.text[0])
            if getattr(frame, "text", None)
            else str(frame)
        )
        if text.startswith(_COMMENT_MARKER):
            comment = text
            break

    return TrackTags(genre=genre, comment=comment)


# --------------------------------------------------------------------------- #
# Escritura
# --------------------------------------------------------------------------- #

def write_genre(path: Path, genre: str) -> tuple[bool, str]:
    """Escribe solo el tag ``genre``. Devuelve ``(ok, motivo)``.

    Usado por ``enrich`` (paso 2): no debe tocar el ``comment`` con la
    energía detallada.
    """
    from mutagen import MutagenError
    audio: Any  # FLAC | ID3 | MP4 según la rama
    ext = path.suffix.lower()
    try:
        if ext in _EXTS_FLAC:
            from mutagen.flac import FLAC
            audio = FLAC(str(path))
            audio["genre"] = genre
            audio.save()
            return True, "ok"

        if ext in _EXTS_ID3:
            from mutagen.id3 import ID3, ID3NoHeaderError, TCON
            try:
                audio = ID3(str(path))
            except ID3NoHeaderError:
                audio = ID3()
            audio.setall("TCON", [TCON(encoding=3, text=[genre])])
            audio.save(str(path))
            return True, "ok"

        if ext in _EXTS_MP4:
            from mutagen.mp4 import MP4
            audio = MP4(str(path))
            audio["\xa9gen"] = [genre]
            audio.save()
            return True, "ok"

        return False, f"formato no soportado para escritura: {ext}"
    except (MutagenError, OSError) as e:
        return False, f"error escribiendo: {e}"


def write_genre_and_comment(
    path: Path, genre: str, comment: str
) -> tuple[bool, str]:
    """Escribe ``genre`` y ``comment`` en una sola pasada. Devuelve
    ``(ok, motivo)``. Usado por ``tag`` (paso 1) y ``recalibrate``.

    En ID3, borra solo el COMM con ``desc=energia,lang=eng`` antes de añadir
    el nuevo — preserva otros COMM (notas del usuario, idiomas distintos).
    """
    from mutagen import MutagenError
    audio: Any  # FLAC | ID3 | MP4 según la rama
    ext = path.suffix.lower()
    try:
        if ext in _EXTS_FLAC:
            from mutagen.flac import FLAC
            audio = FLAC(str(path))
            audio["genre"] = genre
            audio["comment"] = comment
            audio.save()
            return True, "ok"

        if ext in _EXTS_ID3:
            from mutagen.id3 import ID3, ID3NoHeaderError, COMM, TCON
            try:
                audio = ID3(str(path))
            except ID3NoHeaderError:
                audio = ID3()
            audio.setall("TCON", [TCON(encoding=3, text=[genre])])
            audio.delall(f"COMM:{_COMM_DESC}:{_COMM_LANG}")
            audio.add(COMM(
                encoding=3,
                lang=_COMM_LANG,
                desc=_COMM_DESC,
                text=[comment],
            ))
            audio.save(str(path))
            return True, "ok"

        if ext in _EXTS_MP4:
            from mutagen.mp4 import MP4
            audio = MP4(str(path))
            audio["\xa9gen"] = [genre]
            audio["\xa9cmt"] = [comment]
            audio.save()
            return True, "ok"

        return False, f"formato no soportado para escritura: {ext}"
    except (MutagenError, OSError) as e:
        return False, f"error escribiendo: {e}"


# --------------------------------------------------------------------------- #
# Utilidad de paths
# --------------------------------------------------------------------------- #

# Caracteres prohibidos en nombres de archivo/carpeta en Windows y exFAT.
# '/' se trata aparte (se reemplaza por ' - ', no se elimina).
_INVALID_DIRNAME_CHARS = re.compile(r'[<>:"\\|?*]')
_WHITESPACE_RUN = re.compile(r"\s+")


def safe_dirname(s: str) -> str:
    """Sanitiza un string para usarlo como nombre de carpeta.

    - Reemplaza ``/`` por `` - `` (ej. ``'Disco / Nu-Disco'`` →
      ``'Disco - Nu-Disco'``) para preservar la legibilidad del género
      compuesto sin crear una jerarquía no deseada.
    - Elimina caracteres prohibidos en Windows/exFAT (``<>:"\\|?*``).
    - Colapsa espacios consecutivos y hace ``strip``.
    - Si el resultado queda vacío, devuelve ``'_SIN_NOMBRE'``.
    """
    s = s.replace("/", " - ")
    s = _INVALID_DIRNAME_CHARS.sub("", s)
    s = _WHITESPACE_RUN.sub(" ", s).strip()
    return s or "_SIN_NOMBRE"


# --------------------------------------------------------------------------- #
# Helpers internos
# --------------------------------------------------------------------------- #

def _first_str(value: object) -> str:
    """Normaliza el resultado de ``tags.get(...)`` a un ``str``.

    Vorbis (FLAC) devuelve ``list[str]``; MP4 devuelve ``list`` con tipos
    variados; los frames ID3 se manejan aparte (tienen ``.text``).
    """
    if not value:
        return ""
    if isinstance(value, list):
        return str(value[0]) if value else ""
    return str(value)
