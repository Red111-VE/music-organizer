"""Comando ``tag``: paso 1 del pipeline.

Analiza cada archivo de audio del origen con MAEST + emoMusic (vía
:mod:`core.analyzer`) y escribe dos tags en cada archivo:

- ``genre``: categoría DJ simplificada (ej. ``"Tech House"``).
- ``comment``: energía detallada + top-3 de género
  (ej. ``"Energia: ALTA (6.0/9) | Genero: Tech House 63% | House 43% | ..."``).

Genera ``_REPORTE_GENERO.csv`` en la carpeta de origen, con las mismas
columnas y formato que ``etiquetar_genero.py`` original (UTF-8 con BOM para
que Excel lo abra sin pelearse con los acentos).

Thin wrapper: la lógica de análisis vive en :mod:`core.analyzer`; la
escritura de tags en :mod:`core.tagger`; aquí solo orquestamos.

Flags bilingües:

- ``--simulate`` / ``--simular``: corre el análisis pero no escribe tags;
  el CSV se genera igual para revisar las etiquetas antes de aplicarlas.
- ``--limit N`` / ``--limite N``: procesa solo los primeros N archivos.
- ``--resume`` / ``--reanudar``: salta archivos cuyo comment ya empieza con
  ``"Energia:"`` (útil para retomar una corrida interrumpida sin re-analizar
  pistas ya tageadas — el análisis cuesta ~10 s por pista).
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
from core.analyzer import analyze_track
from core.models import load_models, validate_models_dir
from core.tagger import read_tags, write_genre_and_comment


# Orden y nombres EXACTOS del CSV del script original. No reordenar — el
# usuario puede tener reportes históricos contra los que comparar.
_CSV_FIELDNAMES = [
    "estado",
    "archivo",
    "genero_detectado",
    "genero_top3",
    "genero_discogs_crudo",
    "confianza_genero",
    "energia_nivel",
    "energia_arousal",
    "ruta",
]


@click.command("tag")
@click.argument(
    "source",
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
)
@click.option(
    "--models", "--modelos",
    "models_dir",
    required=True,
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
    help="Carpeta con los .pb y .json de Essentia "
         "(descargados con scripts/download_models.sh).",
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
    "--resume", "--reanudar",
    "resume",
    is_flag=True,
    help='Saltar archivos cuyo comment ya empieza con "Energia:" '
         "(retomar corrida previa sin re-analizar).",
)
def tag_command(
    source: Path,
    models_dir: Path,
    simulate: bool,
    limit: int,
    resume: bool,
) -> None:
    """Analiza audio y escribe tags de género + energía."""
    console = Console()

    # Validar modelos en disco antes de cargar Essentia — barato, evita pagar
    # los ~10-15 s de TF si falta algún .pb.
    missing = validate_models_dir(models_dir)
    if missing:
        console.print(
            "[red]ERROR:[/red] faltan archivos en la carpeta de modelos:"
        )
        for name in missing:
            console.print(f"  {name}")
        console.print()
        console.print("Descárgalos de https://essentia.upf.edu/models/")
        raise click.Abort()

    # Lazy import de Essentia ocurre dentro de load_models. ~10–15 s.
    console.print(
        "Cargando Essentia y los modelos... (esto tarda unos segundos)"
    )
    models = load_models(models_dir)

    # Recolectar archivos. Filtros del script original:
    #   - tipo: archivo (no dir) con extensión de audio
    #   - excluye sidecars macOS/exFAT (._*)
    # No excluye subcarpetas con prefijo "_" — eso es solo del organizador.
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

    mode = "SIMULACIÓN (no escribe tags)" if simulate else "REAL (escribe tags)"
    console.print(f"Archivos a procesar: {len(files)}")
    console.print(f"Modo: {mode}")
    if resume:
        console.print(
            'Reanudación: ACTIVA — se saltan archivos ya tageados con "Energia:"'
        )
    console.print("=" * 64)

    rows: list[dict[str, Any]] = []
    ok = err = skipped = 0

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

    with progress:
        task_id = progress.add_task("Analizando", total=len(files))

        for path in files:
            # --resume: lee el comment para decidir si saltar. read_tags es
            # mucho más barato que analyze_track (no toca Essentia).
            if resume:
                existing = read_tags(path)
                if existing.comment.startswith("Energia:"):
                    skipped += 1
                    progress.advance(task_id)
                    continue

            try:
                result = analyze_track(path, models)
            except Exception as e:  # noqa: BLE001 — boundary: error por archivo, sigue
                err += 1
                progress.console.print(
                    f"  [red]![/red] {path.name}: error de análisis: {e}"
                )
                rows.append({
                    "estado": "error",
                    "archivo": path.name,
                    "genero_detectado": "",
                    "genero_top3": "",
                    "genero_discogs_crudo": "",
                    "confianza_genero": "",
                    "energia_nivel": "",
                    "energia_arousal": "",
                    "ruta": str(path),
                })
                progress.advance(task_id)
                continue

            # Formato del comment idéntico al del script original — analyzer
            # produce los campos, aquí los componemos en el string que va al
            # tag y queda legible para el usuario al ver el archivo.
            comment_text = (
                f"Energia: {result.energy_level} ({result.energy_value}/9)"
                f" | Genero: {result.genre_top3_text}"
            )

            if simulate:
                # Paridad con el script original: en simulate, write_tags
                # devolvía (True, 'simulado'), así que estado='ok' en el CSV.
                # El "SIMULACIÓN" en cabecera y banner final indica al usuario
                # que nada se escribió al archivo.
                status = "ok"
                ok += 1
            else:
                wrote, write_motivo = write_genre_and_comment(
                    path, result.genre, comment_text,
                )
                if wrote:
                    status = "ok"
                    ok += 1
                else:
                    status = "error"
                    err += 1
                    progress.console.print(
                        f"  [red]![/red] {path.name}: {write_motivo}"
                    )

            rows.append({
                "estado": status,
                "archivo": path.name,
                "genero_detectado": result.genre,
                "genero_top3": result.genre_top3_text,
                "genero_discogs_crudo": result.genre_raw,
                "confianza_genero": f"{result.genre_confidence:.2f}",
                "energia_nivel": result.energy_level,
                "energia_arousal": result.energy_value,
                "ruta": str(path),
            })
            progress.advance(task_id)

    # Reporte CSV en la carpeta de origen. utf-8-sig para que Excel en
    # Windows reconozca el encoding sin pelearse con los acentos.
    report_path: Path | None = None
    if rows:
        report_path = source / config.CSV_TAG_REPORT
        try:
            with report_path.open("w", newline="", encoding="utf-8-sig") as f:
                writer = csv.DictWriter(f, fieldnames=_CSV_FIELDNAMES)
                writer.writeheader()
                writer.writerows(rows)
        except OSError as e:
            console.print(f"AVISO: no se pudo escribir el reporte: {e}")
            report_path = None
    else:
        console.print(
            "AVISO: no se procesó ningún archivo nuevo "
            "(--resume omitió todos)."
        )

    # Resumen
    console.print()
    console.print("=" * 64)
    console.print("  RESUMEN")
    console.print("=" * 64)
    console.print(f"  Procesados OK : {ok}")
    console.print(f"  Errores       : {err}")
    if skipped:
        console.print(
            f"  Omitidos      : {skipped}  (ya tenían tag de corrida previa)"
        )

    if rows:
        genres = Counter(
            r["genero_detectado"] for r in rows if r["genero_detectado"]
        )
        if genres:
            console.print()
            console.print("  Géneros detectados:")
            for genre, count in genres.most_common():
                console.print(f"    {genre:28s} {count}")

        levels = Counter(
            r["energia_nivel"] for r in rows if r["energia_nivel"]
        )
        if levels:
            console.print()
            console.print("  Energía:")
            for level, count in levels.most_common():
                console.print(f"    {level:8s} {count}")

    if report_path:
        console.print()
        console.print(f"  Reporte: {report_path}")
    if simulate:
        console.print()
        console.print("  *** SIMULACIÓN: no se escribió ningún tag. ***")
        console.print(
            "  Revisa el CSV; si las etiquetas tienen sentido, "
            "corre sin --simulate."
        )
    console.print("=" * 64)
