"""Comando ``pipeline``: corre los 3 pasos del pipeline en orden.

::

    1. tag      — analiza audio, escribe género + energía en los tags.
    2. enrich   — combina género con nivel de energía.
    3. organize — copia/mueve archivos a carpetas por género/nivel.

**No incluye ``recalibrate``**: es una operación posterior y manual. El
usuario corre el pipeline, revisa la distribución de energía resultante, y
si no le gusta corre ``recalibrate`` por separado con umbrales custom o
``--auto-calibrate``.

Implementación: thin orchestrator. Usa ``ctx.invoke`` para reusar los tres
comandos tal cual — sin duplicar lógica ni romper las garantías de cada
uno (incluyendo el aviso de seguridad de ``organize --move``, que se
dispara dentro de organize, no aquí).

Si un paso falla con ``click.Abort()`` (ej. tag no encuentra modelos),
los pasos siguientes no se ejecutan. Si un paso reporta errores por
archivo (algunos archivos fallaron pero la mayoría OK), el pipeline
continúa al siguiente — esos errores quedan en el CSV del paso afectado.

**Nota sobre ``--simulate``**: cada paso simula independientemente leyendo
el estado **actual** de cada archivo, no el estado intermedio que
produciría el paso anterior. Si la biblioteca nunca pasó por ``tag``,
en simulate ``enrich`` y ``organize`` van a ver archivos sin tags y
saltarlos / mandarlos a ``_SIN_TAG/``. Para una preview encadenada real,
corré los 3 pasos en modo real uno por uno.

Flags bilingües: igual que los comandos individuales. ``--resume`` (tag)
y ``--revert`` (enrich) **no se exponen** — son operaciones puntuales,
no parte del flujo inicial.
"""

from __future__ import annotations

from pathlib import Path

import click
from rich.console import Console

from cli.enrich import enrich_command
from cli.organize import organize_command
from cli.tag import tag_command


@click.command("pipeline")
@click.argument(
    "source",
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
)
@click.option(
    "--models", "--modelos",
    "models_dir",
    required=True,
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
    help="Carpeta con los modelos de Essentia (para el paso `tag`).",
)
@click.option(
    "--dest", "--destino",
    "dest",
    required=True,
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
    help="Carpeta destino para el paso `organize` (se crea si no existe).",
)
@click.option(
    "--simulate", "--simular",
    "simulate",
    is_flag=True,
    help="Modo simulación: ningún paso escribe tags ni copia archivos. "
         "Cada paso lee el estado actual del archivo (no el intermedio "
         "que produciría el paso anterior).",
)
@click.option(
    "--limit", "--limite",
    "limit",
    type=int,
    default=0,
    help="Procesar solo los primeros N archivos por paso (0 = todos).",
)
@click.option(
    "--move", "--mover",
    "move",
    is_flag=True,
    help="MOVER archivos en organize (vacía el origen). "
         "Default: COPIA. organize muestra un aviso antes de mover.",
)
@click.option(
    "--flat", "--solo-genero",
    "flat",
    is_flag=True,
    help="Pasa --flat a organize: una sola carpeta por género "
         "(omite el subnivel de energía en la estructura de carpetas).",
)
@click.option(
    "--no-lossless", "--sin-lossless",
    "no_lossless",
    is_flag=True,
    help="Pasa --no-lossless a organize: omite la subcarpeta `Lossless/` "
         "intermedia y cuelga los géneros directamente del destino.",
)
@click.pass_context
def pipeline_command(
    ctx: click.Context,
    source: Path,
    models_dir: Path,
    dest: Path,
    simulate: bool,
    limit: int,
    move: bool,
    flat: bool,
    no_lossless: bool,
) -> None:
    """Corre los 3 pasos del pipeline en orden: tag → enrich → organize."""
    console = Console()

    def _step_header(n: int, name: str) -> None:
        console.print()
        console.print("=" * 64)
        console.print(f"=== PASO {n}/3: {name} ===")
        console.print("=" * 64)

    # Paso 1: tag (no exponemos --resume; pipeline asume corrida fresca)
    _step_header(1, "TAG")
    ctx.invoke(
        tag_command,
        source=source,
        models_dir=models_dir,
        simulate=simulate,
        limit=limit,
        resume=False,
    )

    # Paso 2: enrich (no exponemos --revert; pipeline siempre enriquece)
    _step_header(2, "ENRICH")
    ctx.invoke(
        enrich_command,
        source=source,
        simulate=simulate,
        limit=limit,
        revert=False,
    )

    # Paso 3: organize. El aviso de --move lo dispara organize por su cuenta.
    _step_header(3, "ORGANIZE")
    ctx.invoke(
        organize_command,
        source=source,
        dest=dest,
        simulate=simulate,
        move=move,
        limit=limit,
        flat=flat,
        no_lossless=no_lossless,
    )

    # Cierre
    console.print()
    console.print("=" * 64)
    if simulate:
        console.print("  *** PIPELINE COMPLETO (SIMULACIÓN) ***")
        console.print("  Cada paso generó su CSV de reporte en el origen.")
        console.print(
            "  Si los resultados se ven bien, corré sin --simulate."
        )
    else:
        console.print("  *** PIPELINE COMPLETO ***")
        console.print(
            "  Si la distribución de energía no calza con tu biblioteca, "
            "corré `music-organizer recalibrate --auto-calibrate` y "
            "después `organize` de nuevo."
        )
    console.print("=" * 64)
