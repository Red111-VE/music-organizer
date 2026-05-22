"""Comando ``recalibrate``: ajusta los niveles de energía sin re-analizar audio.

Lee el arousal numérico que ya quedó escrito en cada ``comment`` por ``tag``,
aplica umbrales nuevos (custom o auto-calibrados como percentiles 25/50/75
de la biblioteca), y reescribe:

- ``comment``: nivel actualizado, **arousal y top-3 de género intactos**.
- ``genre``: sufijo ``/ NIVEL`` actualizado **solo si ya lo tenía**
  (no enriquece archivos sin sufijo — eso es trabajo de :mod:`cli.enrich`).

Toma <1 min sobre cientos de pistas — vs. ~60–90 min de re-análisis con
Essentia. Útil cuando los niveles que produjo ``tag`` con los defaults no
calzan con la biblioteca real (típicamente todo cae en ``ALTA``).

Thin wrapper sobre :mod:`core.recalibrator`. Aquí orquestamos en dos pasadas:

1. **Lectura**: leemos todos los ``(genre, comment)`` con :mod:`core.tagger`
   y extraemos los arousals con :func:`parse_arousal_comment`. Esta pasada es
   necesaria antes de decidir los umbrales en modo ``--auto-calibrate`` —
   sin los arousals no se pueden computar los percentiles.
2. **Escritura**: aplicamos los umbrales decididos a cada pista con
   :func:`compute_recalibration` y persistimos con
   :func:`write_genre_and_comment` (a diferencia de ``enrich``,
   ``recalibrate`` **sí toca el comment** porque el nivel vive ahí).

Genera ``_REPORTE_RECALCULO.csv`` en la carpeta de origen.

Flags bilingües:

- ``--thresholds B,M,A`` / ``--umbrales B,M,A``
- ``--auto-calibrate`` / ``--auto-calibrar``
- ``--simulate`` / ``--simular``
- ``--limit N`` / ``--limite N``

``--thresholds`` y ``--auto-calibrate`` son **mutuamente excluyentes**, pero
**uno** de los dos es obligatorio.
"""

from __future__ import annotations

import csv
from collections import Counter
from pathlib import Path
from statistics import StatisticsError
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
from core.config import EnergyThresholds
from core.recalibrator import (
    compute_calibrated_thresholds,
    compute_recalibration,
    parse_arousal_comment,
)
from core.tagger import read_tags, write_genre_and_comment


# Columnas y orden EXACTOS del CSV del script original. No reordenar.
_CSV_FIELDNAMES = [
    "estado",
    "archivo",
    "arousal",
    "nivel_antes",
    "nivel_despues",
    "genre_antes",
    "genre_despues",
    "motivo",
]


def _parse_thresholds_arg(s: str) -> EnergyThresholds:
    """Parsea ``"B,M,A"`` → ``(B, M, A)`` validando 3 floats ascendentes."""
    parts = [x.strip() for x in s.split(",")]
    if len(parts) != 3:
        raise click.BadParameter(
            f"se esperan 3 valores separados por coma (B,M,A); "
            f"llegaron {len(parts)}"
        )
    try:
        values = tuple(float(p) for p in parts)
    except ValueError as e:
        raise click.BadParameter(f"valores no numéricos: {e}") from e
    if values[0] >= values[1] or values[1] >= values[2]:
        raise click.BadParameter(
            f"los umbrales deben ser crecientes (low < med < high); "
            f"recibí {values}"
        )
    return values  # type: ignore[return-value]


@click.command("recalibrate")
@click.argument(
    "source",
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
)
@click.option(
    "--thresholds", "--umbrales",
    "thresholds_arg",
    default=None,
    help='Tres umbrales separados por coma: "B,M,A" '
         "(ej. \"5.90,6.10,6.40\"). Mutuamente excluyente con --auto-calibrate.",
)
@click.option(
    "--auto-calibrate", "--auto-calibrar",
    "auto_calibrate",
    is_flag=True,
    help="Calcula los umbrales como percentiles 25/50/75 de la biblioteca. "
         "Mutuamente excluyente con --thresholds.",
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
def recalibrate_command(
    source: Path,
    thresholds_arg: str | None,
    auto_calibrate: bool,
    simulate: bool,
    limit: int,
) -> None:
    """Recalcula niveles de energía sin re-analizar audio."""
    console = Console()

    # Mutex de modo: exactamente uno de los dos.
    if not thresholds_arg and not auto_calibrate:
        raise click.UsageError(
            "Falta el modo: pasá --thresholds B,M,A o --auto-calibrate."
        )
    if thresholds_arg and auto_calibrate:
        raise click.UsageError(
            "--thresholds y --auto-calibrate son mutuamente excluyentes."
        )

    # Parsear --thresholds upfront para fallar rápido con error claro si
    # están mal formados, antes de leer archivos. Para --auto-calibrate los
    # umbrales se computan después del paso 1; aquí queda None y se asigna
    # luego.
    parsed_thresholds: EnergyThresholds | None = (
        _parse_thresholds_arg(thresholds_arg) if thresholds_arg else None
    )

    # Recolectar archivos (mismo filtro que tag/enrich).
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

    # --- PASO 1: lectura. Leemos (genre, comment) de todos y extraemos
    # los arousals que vamos a necesitar si el modo es --auto-calibrate.
    # Cacheamos (path, genre, comment) para no releer en el paso 3.
    console.print(f"Leyendo {len(files)} archivo(s)...")
    file_data: list[tuple[Path, str, str]] = []
    arousals: list[float] = []
    for path in files:
        tags = read_tags(path)
        file_data.append((path, tags.genre, tags.comment))
        parsed = parse_arousal_comment(tags.comment)
        if parsed is not None:
            arousals.append(parsed[1])  # arousal_float

    if not arousals:
        console.print(
            "[red]ERROR:[/red] no se encontró ningún arousal en los comments. "
            "Corre `music-organizer tag` primero para que las pistas tengan "
            "el campo 'Energia:' escrito."
        )
        raise click.Abort()

    # --- PASO 2: decidir umbrales.
    if auto_calibrate:
        try:
            thresholds = compute_calibrated_thresholds(arousals)
        except StatisticsError as e:
            raise click.UsageError(
                f"No hay datos suficientes para auto-calibrar "
                f"({len(arousals)} arousal(s) encontrados). Detalle: {e}"
            ) from e
        console.print(
            f"Auto-calibrado: percentiles 25/50/75 = "
            f"{thresholds[0]} / {thresholds[1]} / {thresholds[2]}"
        )
    else:
        # parsed_thresholds garantizado no-None por el mutex + parseo upfront.
        assert parsed_thresholds is not None
        thresholds = parsed_thresholds

    console.print()
    console.print("Umbrales a aplicar:")
    console.print(f"  BAJA     <  {thresholds[0]}")
    console.print(f"  MEDIA    <  {thresholds[1]}")
    console.print(f"  ALTA     <  {thresholds[2]}")
    console.print(f"  MUY ALTA >= {thresholds[2]}")

    mode_real = "SIMULACIÓN" if simulate else "REAL (escribe tags)"
    console.print()
    console.print(f"Modo: {mode_real}")
    console.print("=" * 64)

    # --- PASO 3: recalcular y escribir.
    rows: list[dict[str, Any]] = []
    cambios = sin_cambio = saltados = errores = 0
    transitions: Counter[tuple[str, str]] = Counter()
    nueva_dist: Counter[str] = Counter()

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
        task_id = progress.add_task("Recalculando", total=len(file_data))

        for path, genre, comment in file_data:
            result = compute_recalibration(genre, comment, thresholds)

            if result.motivo.startswith("skip:"):
                # Comment no parseó. CSV motivo = textual original.
                saltados += 1
                rows.append({
                    "estado": "skip",
                    "archivo": path.name,
                    "arousal": "",
                    "nivel_antes": "",
                    "nivel_despues": "",
                    "genre_antes": result.old_genre,
                    "genre_despues": result.old_genre,
                    "motivo": "sin arousal en comment",
                })
                progress.advance(task_id)
                continue

            # En esta rama, parse_arousal_comment sí parseó; los campos
            # arousal/old_level/new_level no son None.
            assert result.arousal is not None
            assert result.new_level is not None
            assert result.old_level is not None

            nueva_dist[result.new_level] += 1

            if result.motivo == "sin-cambio":
                # CSV motivo = 'mismo nivel' (paridad textual con el original;
                # el dataclass usa 'sin-cambio' pero el CSV usa 'mismo nivel').
                sin_cambio += 1
                rows.append({
                    "estado": "sin-cambio",
                    "archivo": path.name,
                    "arousal": result.arousal,
                    "nivel_antes": result.old_level,
                    "nivel_despues": result.new_level,
                    "genre_antes": result.old_genre,
                    "genre_despues": result.old_genre,
                    "motivo": "mismo nivel",
                })
                progress.advance(task_id)
                continue

            # result.motivo == "recalcular": el nivel y/o el genre cambian.
            transitions[(result.old_level, result.new_level)] += 1
            csv_motivo = f"{result.old_level} -> {result.new_level}"

            if simulate:
                cambios += 1
                rows.append({
                    "estado": "simulado",
                    "archivo": path.name,
                    "arousal": result.arousal,
                    "nivel_antes": result.old_level,
                    "nivel_despues": result.new_level,
                    "genre_antes": result.old_genre,
                    "genre_despues": result.new_genre,
                    "motivo": csv_motivo,
                })
            else:
                # write_genre_and_comment: recalibrate SÍ toca el comment
                # (el nivel vive dentro de él), a diferencia de enrich que
                # solo toca el genre.
                wrote, msg = write_genre_and_comment(
                    path, result.new_genre, result.new_comment,
                )
                if wrote:
                    cambios += 1
                    rows.append({
                        "estado": "ok",
                        "archivo": path.name,
                        "arousal": result.arousal,
                        "nivel_antes": result.old_level,
                        "nivel_despues": result.new_level,
                        "genre_antes": result.old_genre,
                        "genre_despues": result.new_genre,
                        "motivo": csv_motivo,
                    })
                else:
                    errores += 1
                    progress.console.print(
                        f"  [red]![/red] {path.name}: {msg}"
                    )
                    rows.append({
                        "estado": "error",
                        "archivo": path.name,
                        "arousal": result.arousal,
                        "nivel_antes": result.old_level,
                        "nivel_despues": result.new_level,
                        "genre_antes": result.old_genre,
                        "genre_despues": result.new_genre,
                        "motivo": msg,
                    })

            progress.advance(task_id)

    # CSV de reporte.
    csv_target = source / config.CSV_RECALIBRATE_REPORT
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

    # Resumen.
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

    if transitions:
        console.print()
        console.print("  Transiciones de nivel:")
        for (before, after), n in sorted(
            transitions.items(), key=lambda x: -x[1]
        ):
            console.print(f"    {before:10s} -> {after:10s} : {n}")

    if sum(nueva_dist.values()) > 0:
        console.print()
        console.print("  Nueva distribución de energía:")
        total_dist = sum(nueva_dist.values())
        for level in config.LEVELS_TUPLE:
            n = nueva_dist.get(level, 0)
            pct = 100 * n / total_dist if total_dist else 0
            histogram = "█" * int(pct / 2)
            console.print(f"    {level:10s} {n:4d}  {pct:5.1f}%  {histogram}")

    if report_path:
        console.print()
        console.print(f"  Reporte: {report_path}")
    if simulate:
        console.print()
        console.print("  *** SIMULACIÓN: no se escribió ningún tag. ***")
        console.print("  Si te gustan los resultados, corre sin --simulate.")
        console.print()
        console.print("  Después de aplicar los cambios reales:")
        console.print(
            "    1. Re-importá la biblioteca en Rekordbox para que vea "
            "los nuevos tags."
        )
        console.print(
            "    2. (Opcional) Volvé a correr `music-organizer organize` "
            "si querés reorganizar las carpetas a la nueva distribución."
        )
    console.print("=" * 64)
