"""Comando ``enrich``: paso 2 del pipeline.

Combina el nivel de energía (que ``tag`` dejó en el ``comment``) con el
``genre`` en un solo tag estructurado:

::

    genre:   "Tech House"             →   "Tech House / ALTA"
    comment: "Energia: ALTA (6.0/9)…" (queda intacto)

``--revert`` deshace el cambio: quita el sufijo `` / NIVEL`` del ``genre``.

Thin wrapper: la lógica vive en :func:`core.enricher.compute_enriched_genre`
(string puro, sin filesystem). Aquí solo recolectamos archivos, leemos tags
con :mod:`core.tagger`, despachamos la decisión, y persistimos con
:func:`core.tagger.write_genre` (**no** ``write_genre_and_comment`` —
enrich no debe tocar el comment).

Genera ``_REPORTE_ENRIQUECER.csv`` (o ``_REPORTE_REVERTIR.csv`` con
``--revert``) en la carpeta de origen, con las mismas columnas y formato
que ``enriquecer_genero.py`` original.

Flags bilingües:

- ``--simulate`` / ``--simular``
- ``--limit N`` / ``--limite N``
- ``--revert`` / ``--revertir``
"""

from __future__ import annotations

import csv
from collections import Counter
from pathlib import Path
from typing import Any

import click
from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)

from core import config
from core.enricher import compute_enriched_genre
from core.tagger import read_tags, write_genre


# Orden y nombres EXACTOS del CSV del script original. No reordenar — paridad
# para diff contra reportes históricos del usuario.
_CSV_FIELDNAMES = [
    "estado",
    "archivo",
    "genre_antes",
    "genre_despues",
    "motivo",
]


@click.command("enrich")
@click.argument(
    "source",
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
)
@click.option(
    "--simulate", "--simular",
    "simulate",
    is_flag=True,
    help="No escribe tags; solo genera el CSV de reporte.",
)
@click.option(
    "--limit", "--limite",
    "limit",
    type=int,
    default=0,
    help="Procesar solo los primeros N archivos (0 = todos).",
)
@click.option(
    "--revert", "--revertir",
    "revert",
    is_flag=True,
    help='Quita el sufijo " / NIVEL" del tag genre '
         "(deshace lo que enrich hizo en una corrida previa).",
)
def enrich_command(
    source: Path,
    simulate: bool,
    limit: int,
    revert: bool,
) -> None:
    """Combina el género con el nivel de energía en un solo tag."""
    console = Console()

    # Recolectar archivos. Mismo filtro que tag: is_file + AUDIO_EXTS + no
    # sidecars macOS. Tampoco excluye subcarpetas con prefijo "_" — eso es
    # solo del organizador.
    files = sorted(
        p for p in source.rglob("*")
        if p.is_file()
        and p.suffix.lower() in config.AUDIO_EXTS
        and not p.name.startswith(config.SIDECAR_PREFIX)
    )
    if limit:
        files = files[:limit]
    if not files:
        console.print("No se encontraron archivos de audio.")
        return

    mode_action = "REVERTIR" if revert else "ENRIQUECER"
    mode_real = "SIMULACIÓN" if simulate else "REAL (escribe tags)"
    console.print(f"Archivos a procesar: {len(files)}")
    console.print(f"Modo: {mode_action}  ({mode_real})")
    console.print("=" * 64)

    rows: list[dict[str, Any]] = []
    cambios = sin_cambio = saltados = errores = 0

    progress = Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        TextColumn("•"),
        TimeElapsedColumn(),
        TextColumn("•"),
        TimeRemainingColumn(),
        console=console,
    )

    description = "Revirtiendo" if revert else "Enriqueciendo"

    with progress:
        task_id = progress.add_task(description, total=len(files))

        for path in files:
            # read_tags es defensiva por contrato (devuelve TrackTags() vacío
            # ante corrupción / permission denied / etc.). Eso significa que
            # archivos corruptos caerán en la rama 'skip: sin tag de genero'
            # en vez de 'error: lectura: ...' del script original. Mismo
            # outcome práctico (no se escribe nada, queda log en el CSV);
            # solo cambia el texto del motivo en ese edge case.
            existing = read_tags(path)
            target, motivo = compute_enriched_genre(
                existing.genre, existing.comment, revert=revert,
            )

            if motivo.startswith("skip:"):
                saltados += 1
                progress.console.print(f"  - {path.name}: {motivo}")
                rows.append({
                    "estado": "skip",
                    "archivo": path.name,
                    "genre_antes": existing.genre,
                    "genre_despues": existing.genre,
                    "motivo": motivo,
                })
                progress.advance(task_id)
                continue

            if motivo == "sin-cambio":
                sin_cambio += 1
                rows.append({
                    "estado": "sin-cambio",
                    "archivo": path.name,
                    "genre_antes": existing.genre,
                    "genre_despues": existing.genre,
                    "motivo": motivo,
                })
                progress.advance(task_id)
                continue

            # Cambio real: motivo es 'enriquecer' o 'revertir'.
            if simulate:
                # Paridad con el script original: estado='simulado' en este
                # comando (a diferencia de `tag`, que usa 'ok' en simulate).
                # La inconsistencia entre scripts ya estaba en los originales;
                # la mantenemos para que los CSVs sean diff-eables.
                cambios += 1
                rows.append({
                    "estado": "simulado",
                    "archivo": path.name,
                    "genre_antes": existing.genre,
                    "genre_despues": target,
                    "motivo": motivo,
                })
            else:
                wrote, write_msg = write_genre(path, target)
                if wrote:
                    cambios += 1
                    rows.append({
                        "estado": "ok",
                        "archivo": path.name,
                        "genre_antes": existing.genre,
                        "genre_despues": target,
                        "motivo": motivo,
                    })
                else:
                    errores += 1
                    progress.console.print(
                        f"  [red]![/red] {path.name}: {write_msg}"
                    )
                    rows.append({
                        "estado": "error",
                        "archivo": path.name,
                        "genre_antes": existing.genre,
                        "genre_despues": target,
                        # En error, motivo del CSV se sobrescribe con el del
                        # error de escritura (paridad con el script original).
                        "motivo": write_msg,
                    })

            progress.advance(task_id)

    # CSV: nombre según modo (revert vs enriquecer).
    csv_name = config.CSV_REVERT_REPORT if revert else config.CSV_ENRICH_REPORT
    csv_target = source / csv_name
    report_path: Path | None
    try:
        with csv_target.open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=_CSV_FIELDNAMES)
            writer.writeheader()
            writer.writerows(rows)
        report_path = csv_target
    except OSError as e:
        console.print(f"AVISO: no se pudo escribir el reporte: {e}")
        report_path = None

    # Resumen (etiquetas idénticas al script original)
    console.print()
    console.print("=" * 64)
    console.print("  RESUMEN")
    console.print("=" * 64)
    cambios_label = f"  Cambios     : {cambios}"
    if simulate:
        cambios_label += "  (simulados)"
    console.print(cambios_label)
    console.print(f"  Sin cambio  : {sin_cambio}")
    console.print(f"  Saltados    : {saltados}")
    console.print(f"  Errores     : {errores}")

    # Top-15 solo en modo enriquecer (no en revert) y si hubo cambios.
    if cambios > 0 and not revert:
        top15 = Counter(
            r["genre_despues"] for r in rows
            if r["estado"] in ("ok", "simulado")
        ).most_common(15)
        if top15:
            console.print()
            console.print("  Géneros enriquecidos (top 15):")
            for genre, count in top15:
                console.print(f"    {genre:36s} {count}")

    if report_path:
        console.print()
        console.print(f"  Reporte: {report_path}")
    if simulate:
        console.print()
        console.print("  *** SIMULACIÓN: no se escribió ningún tag. ***")
    console.print("=" * 64)
