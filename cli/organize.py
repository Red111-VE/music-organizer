"""Comando ``organize``: paso 3 del pipeline.

Reorganiza una biblioteca enriquecida (con tags ``"Tech House / ALTA"``) en
una estructura plana de dos niveles bajo el destino:

::

    <dest>/Lossless/
        Tech House/
            ALTA/   <- archivo.flac
            MEDIA/
        Disco - Nu-Disco/
            ALTA/
        _SIN_TAG/    <- archivos sin tag genre
        Tech House/
            _SIN_NIVEL/  <- género presente pero sin sufijo / NIVEL

Default **COPIA** (preserva el origen). Con ``--move`` mueve (vacía el
origen). No renombra archivos.

Thin wrapper: la lógica vive en :mod:`core.organizer`. Aquí solo resolvemos
``dest_base``, recolectamos archivos, despachamos por archivo, escribimos CSV
y resumen. La capa ``Lossless/`` se aplica aquí (no en core) porque es una
decisión de presentación del CLI, controlada por ``--no-lossless``.

Genera ``_REPORTE_ORGANIZACION.csv`` en la carpeta de origen, con las mismas
columnas y formato que ``organizar_genero_energia.py`` original.

Flags bilingües:

- ``--source`` / ``--origen`` (req.) y ``--dest`` / ``--destino`` (req.)
- ``--simulate`` / ``--simular``
- ``--move`` / ``--mover`` (con aviso visible cuando está activo en modo real)
- ``--limit N`` / ``--limite N``
- ``--flat`` / ``--solo-genero``
- ``--no-lossless`` / ``--sin-lossless``
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
from core.organizer import collect_audio_files, compute_target, organize_file
from core.tagger import read_tags


# Orden y nombres EXACTOS del CSV del script original. No reordenar.
_CSV_FIELDNAMES = [
    "estado",
    "archivo",
    "genre",
    "destino",
    "motivo",
]


@click.command("organize")
@click.option(
    "--source", "--origen",
    "source",
    required=True,
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
    help="Carpeta con la música enriquecida (recursiva).",
)
@click.option(
    "--dest", "--destino",
    "dest",
    required=True,
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
    help="Carpeta destino (se crea si no existe).",
)
@click.option(
    "--simulate", "--simular",
    "simulate",
    is_flag=True,
    help="No copia/mueve archivos; solo genera el CSV de reporte.",
)
@click.option(
    "--move", "--mover",
    "move",
    is_flag=True,
    help="MUEVE archivos en vez de copiar (vacía el origen). "
         "Default: COPIA (seguro, preserva el origen).",
)
@click.option(
    "--limit", "--limite",
    "limit",
    type=int,
    default=0,
    help="Procesar solo los primeros N archivos (0 = todos).",
)
@click.option(
    "--flat", "--solo-genero",
    "flat",
    is_flag=True,
    help="Una sola carpeta por género (omite el subnivel de energía). "
         "La energía queda en el tag, no en la estructura de carpetas.",
)
@click.option(
    "--no-lossless", "--sin-lossless",
    "no_lossless",
    is_flag=True,
    help="Crea las carpetas de género directamente en --dest, "
         'sin la subcarpeta "Lossless/" intermedia.',
)
def organize_command(
    source: Path,
    dest: Path,
    simulate: bool,
    move: bool,
    limit: int,
    flat: bool,
    no_lossless: bool,
) -> None:
    """Copia/mueve archivos a una estructura de carpetas por género y energía."""
    console = Console()

    # Resolver dest_base: el script original cuelga las carpetas de género
    # bajo "Lossless/" por convención (por si en el futuro coexisten otros
    # formatos). --no-lossless las cuelga directamente del destino.
    dest_base = dest if no_lossless else dest / "Lossless"

    # Aviso destacado cuando --move está activo en modo real. Es destructivo
    # (vacía el origen) — el usuario tiene unos cientos de ms para Ctrl-C
    # antes de que arranque el procesamiento.
    if move and not simulate:
        console.print()
        console.print(
            "[bold red]ATENCIÓN: --move/--mover está activo.[/bold red]"
        )
        console.print(
            "[red]Los archivos se MUEVEN del origen al destino "
            "(el origen queda vacío de pistas organizadas).[/red]"
        )
        console.print(
            "Si esto no es lo que querés, cancelá con Ctrl-C y volvé "
            "a correr sin --move (default = COPIA)."
        )
        console.print()

    # Recolectar archivos con el filtro complejo de core.organizer:
    # is_file + audio ext + excluye sidecars ._* + excluye dentro de dest +
    # excluye subcarpetas con prefijo _.
    files = collect_audio_files(source, dest)
    if limit:
        files = files[:limit]
    if not files:
        console.print("No se encontraron archivos de audio en el origen.")
        return

    action = "MOVER" if move else "COPIAR"
    mode_real = "SIMULACIÓN (no toca archivos)" if simulate else "REAL"
    console.print(f"Origen   : {source}")
    console.print(f"Destino  : {dest_base}")
    console.print(f"Archivos : {len(files)}")
    console.print(f"Modo     : {action}  ({mode_real})")
    console.print("=" * 64)

    rows: list[dict[str, Any]] = []
    ok = sin_tag = errores = 0

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

    description = "Moviendo" if move else "Copiando"

    with progress:
        task_id = progress.add_task(description, total=len(files))

        for path in files:
            # read_tags es defensiva (devuelve TrackTags() vacío ante
            # corrupción). En ese caso genre_tag='' y compute_target manda
            # el archivo a _SIN_TAG/ — donde el script original abortaba
            # con estado='error' al fallar la lectura. La diferencia está
            # documentada en el CHANGELOG.
            existing = read_tags(path)
            genre_tag = existing.genre

            target, base_motivo = compute_target(
                path, genre_tag, dest_base, flat=flat,
            )

            # Bump del contador en base al targeting. Optimista: asumimos que
            # la operación va a tener éxito; si falla, lo revertimos abajo y
            # contamos solo como error. Esto garantiza que cada archivo cuenta
            # en exactamente un contador (ok + sin_tag + errores == total).
            #
            # El script original tenía un bug aquí: solo decrementaba `ok` en
            # error (`ok -= 1 if motivo == 'ok' else 0`), nunca `sin_tag`. Así
            # un archivo con targeting "sin tag" que después fallaba se
            # contaba en `sin_tag` Y en `errores`, inflando el resumen. Lo
            # arreglamos — ver CHANGELOG → Fixed.
            if base_motivo == "ok":
                ok += 1
            else:
                sin_tag += 1

            result = organize_file(
                path, target, genre_tag, base_motivo,
                move=move, dry_run=simulate,
            )

            # Mapeo de result.status al estado del CSV:
            #   - dry-run (simulate): siempre 'simulado'
            #     (organize_file devuelve 'ok' en dry_run, pero el CSV del
            #     original escribía 'simulado' — preservamos esa semántica).
            #   - real: 'ok' o 'error' según organize_file.
            if simulate:
                csv_estado = "simulado"
            elif result.status == "ok":
                csv_estado = "ok"
            else:  # 'error'
                csv_estado = "error"
                errores += 1
                # Revertir el incremento del targeting, sea cual sea. Así el
                # archivo cuenta SOLO en `errores`, no doble.
                if base_motivo == "ok":
                    ok -= 1
                else:
                    sin_tag -= 1
                progress.console.print(
                    f"  [red]![/red] {path.name}: {result.motivo}"
                )

            # `destino` del CSV: relativo a dest (no dest_base), o sea
            # incluye el componente "Lossless/" cuando aplica.
            destino_rel = str(result.target.relative_to(dest))

            rows.append({
                "estado": csv_estado,
                "archivo": path.name,
                "genre": genre_tag,
                "destino": destino_rel,
                "motivo": result.motivo,
            })

            progress.advance(task_id)

    # Reporte CSV en la carpeta de origen (no en el destino — así el usuario
    # puede repetir corridas y comparar reportes side-by-side).
    csv_target = source / config.CSV_ORGANIZE_REPORT
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

    # Resumen
    console.print()
    console.print("=" * 64)
    console.print("  RESUMEN")
    console.print("=" * 64)
    console.print(f"  OK              : {ok}")
    console.print(f"  Sin tag / nivel : {sin_tag}")
    console.print(f"  Errores         : {errores}")

    # Distribución por carpeta destino (top 20). Usa Path.parts (no .split('/'))
    # para que funcione en Windows también. Descarta "Lossless" si aparece
    # como primer componente, para que el resumen sea consistente con/sin
    # --no-lossless.
    def _categoria(rel: str) -> str:
        parts = Path(rel).parts[:-1]  # drop filename
        if parts and parts[0] == "Lossless":
            parts = parts[1:]
        return "/".join(parts)

    distribucion = Counter(
        _categoria(r["destino"]) for r in rows
        if r["estado"] in ("ok", "simulado") and r["destino"]
    )
    if distribucion:
        console.print()
        console.print("  Distribución (top 20):")
        for path_str, count in distribucion.most_common(20):
            console.print(f"    {path_str:42s} {count}")

    if report_path:
        console.print()
        console.print(f"  Reporte: {report_path}")
    if simulate:
        console.print()
        console.print("  *** SIMULACIÓN: no se tocó ningún archivo. ***")
    console.print("=" * 64)
