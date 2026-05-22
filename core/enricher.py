"""Paso 2 del pipeline: enriquece/revierte el tag ``genre`` con la energía.

Lógica pura de strings sobre el ``(genre, comment)`` que ``cli/enrich.py`` lee
del archivo (vía :mod:`core.tagger`). No toca el filesystem, no importa
mutagen, no carga essentia. Importar este módulo es esencialmente gratis.

Operaciones:

- **Enriquecer** (``revert=False``): toma el nivel del comment
  (``"Energia: ALTA (6.0/9) | ..."``) y lo añade como sufijo al genre
  (``"Tech House"`` → ``"Tech House / ALTA"``).
- **Revertir** (``revert=True``): quita el sufijo `` / NIVEL`` del genre,
  dejando el género base.

**Idempotencia**: re-correr enriquecer sobre un genre ya enriquecido no
encadena niveles — antes de recalcular se quita el sufijo existente, así
``"Tech House / ALTA"`` con un comment de ``MEDIA`` queda
``"Tech House / MEDIA"``, no ``"Tech House / ALTA / MEDIA"``.

**Reversibilidad**: ``revert=True`` invierte ``revert=False``. Caso patológico:
si el archivo solo tenía un sufijo de nivel (``"/ ALTA"``), revertir lo dejaría
vacío — devolvemos ``'skip: ...'`` antes de escribir un genre vacío al archivo.

**Motivos**: las cadenas de motivo van en español sin acentos, idénticas al
script ``enriquecer_genero.py`` original. Se escriben al CSV de reporte
(``_REPORTE_ENRIQUECER.csv`` / ``_REPORTE_REVERTIR.csv``) que el usuario lee.
Mantener la paridad exacta permite hacer diff contra reportes de corridas
históricas sin ruido textual.
"""

from __future__ import annotations

from core.config import RE_ENERGY_FROM_COMMENT, RE_LEVEL_SUFFIX


def compute_enriched_genre(
    current_genre: str,
    current_comment: str,
    revert: bool = False,
) -> tuple[str, str]:
    """Calcula el ``genre`` resultante y el motivo del cambio.

    Devuelve ``(nuevo_genre, motivo)`` donde ``motivo`` es uno de:

    - ``'enriquecer'`` — se añadió `` / NIVEL`` al genre.
    - ``'revertir'`` — se quitó `` / NIVEL`` del genre.
    - ``'sin-cambio'`` — el genre objetivo coincide con el actual; el caller
      puede saltar la escritura.
    - ``'skip: ...'`` — no se calculó cambio porque faltan datos o el cambio
      sería destructivo. El caller registra el motivo en el CSV y no escribe.

    Cuando ``motivo`` empieza con ``'skip:'`` o es ``'sin-cambio'``, el primer
    elemento del tuple es siempre el ``current_genre`` sin tocar (nunca se
    devuelve un genre parcialmente calculado).
    """
    if revert:
        # Quita el sufijo " / NIVEL" del final si lo tiene. Si no hay sufijo,
        # no hay nada que revertir.
        if not RE_LEVEL_SUFFIX.search(current_genre):
            return current_genre, "sin-cambio"
        stripped = RE_LEVEL_SUFFIX.sub("", current_genre).strip()
        if not stripped:
            # El archivo tenía solo un sufijo (ej. "/ ALTA") — escribir
            # genre vacío sería destructivo, mejor saltarlo.
            return current_genre, "skip: revertir dejaria genre vacio"
        return stripped, "revertir"

    # Enriquecer: necesita energia desde el comment.
    if not current_genre:
        return current_genre, "skip: sin tag de genero"
    if not current_comment:
        return current_genre, "skip: sin comment con Energia"
    match = RE_ENERGY_FROM_COMMENT.match(current_comment)
    if not match:
        return current_genre, "skip: comment no empieza con Energia: <NIVEL>"
    level = match.group(1)

    # Idempotencia: si ya tiene un sufijo de nivel, lo quitamos antes para
    # recalcular. Así re-correr nunca encadena " / ALTA / ALTA" y permite
    # corregir un nivel viejo cuando recalibrate cambió el bucket de la pista.
    base = RE_LEVEL_SUFFIX.sub("", current_genre).strip()
    target = f"{base} / {level}"
    if target == current_genre:
        return current_genre, "sin-cambio"
    return target, "enriquecer"
