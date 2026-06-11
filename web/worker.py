"""Worker subprocess para la interfaz web: corre el pipeline y emite eventos
JSON por stdout.

Diseñado para ejecutarse como ``python -m web.worker <source> --models <dir>
--dest <dir> [--simulate] [--flat] [--no-lossless] [--move]`` desde un
subprocess. Cada evento es una línea JSON terminada en newline, con
``flush=True`` inmediato — el runner del lado web lee línea por línea y
empuja el progreso al frontend vía WebSocket.

Eventos emitidos en orden:

- ``{"event": "start"}``
- ``{"event": "models_loading"}`` / ``{"event": "models_loaded"}``
- Por cada fase (tag → enrich → organize):

  - ``{"event": "phase", "phase": "<nombre>", "total": N}``
  - N × ``{"event": "progress", "phase": ..., "done": i, "total": N,
    "file": "...", "status": "...", ...}``

- ``{"event": "done", "tag_ok": X, "tag_err": Y, "report": "...csv"}``
- En error global (modelos faltantes, origen no existe, excepción
  inesperada): ``{"event": "error", "message": "..."}`` y exit code 1.

Reutiliza ``core/`` íntegro: ``validate_models_dir``, ``load_models``,
``analyze_track``, ``compute_enriched_genre``, ``compute_target``,
``organize_file``, ``collect_audio_files``, ``read_tags``, ``write_genre``,
``write_genre_and_comment``. No duplica lógica.

NO usa ``rich`` ni ``click`` — la salida es JSONL para máquina, no para
humano. Los imports de essentia son lazy (vía :func:`core.models.load_models`).

El CSV de tag (``_REPORTE_GENERO.csv``) se escribe en la carpeta de origen,
con el mismo formato que ``cli/tag.py``, para que el usuario pueda abrirlo
en Excel desde el resultado de la web. Los CSVs de enrich y organize no se
escriben en esta versión — la web mantiene el estado en memoria.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any

from core import config
from core.analyzer import analyze_track
from core.enricher import compute_enriched_genre
from core.models import load_models, validate_models_dir
from core.organizer import collect_audio_files, compute_target, organize_file
from core.tagger import read_tags, write_genre, write_genre_and_comment


# Columnas del CSV de tag — paridad exacta con cli/tag.py para que los
# reportes generados desde la web sean diff-eables contra los del CLI.
_CSV_TAG_FIELDNAMES = [
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


# --------------------------------------------------------------------------- #
# Emisión de eventos
# --------------------------------------------------------------------------- #

def emit(**event: Any) -> None:
    """Emite un evento como una línea JSON por stdout, con flush inmediato.

    ``ensure_ascii=False`` deja los acentos legibles en el stream (los nombres
    de archivo y motivos llevan tildes/ñ). ``flush=True`` es lo que permite al
    runner web leer el progreso en tiempo real — sin él, Python bufferea
    stdout cuando no es una TTY (caso subprocess) y la UI queda muda.
    """
    print(json.dumps(event, ensure_ascii=False), flush=True)


# --------------------------------------------------------------------------- #
# Recolección de archivos
# --------------------------------------------------------------------------- #

def _collect_simple(source: Path) -> list[Path]:
    """Filtro simple usado por tag y enrich: archivo + extensión de audio +
    no sidecar. Idéntico al de :mod:`cli.tag` y :mod:`cli.enrich`.

    No excluye subcarpetas con prefijo ``_`` — esa exclusión es solo de
    :func:`core.organizer.collect_audio_files`.
    """
    return sorted(
        p for p in source.rglob("*")
        if p.is_file()
        and p.suffix.lower() in config.AUDIO_EXTS
        and not p.name.startswith(config.SIDECAR_PREFIX)
    )


# --------------------------------------------------------------------------- #
# Fases
# --------------------------------------------------------------------------- #

def run_tag(
    source: Path,
    models_dir: Path,
    simulate: bool,
) -> tuple[int, int, Path | None]:
    """Fase 1: análisis + escritura de tags. Devuelve ``(ok, err, report)``.

    Errores por archivo (audio corrupto, modelo crasheando, IO) NO abortan el
    batch: se reportan vía ``progress`` con ``status="error"`` y se sigue con
    el siguiente. Errores globales (modelos no cargan) propagan al caller, que
    los traduce a evento ``error``.
    """
    emit(event="models_loading")
    models = load_models(models_dir)
    emit(event="models_loaded")

    files = _collect_simple(source)
    total = len(files)
    emit(event="phase", phase="tag", total=total)

    rows: list[dict[str, Any]] = []
    ok = err = 0

    for i, path in enumerate(files, start=1):
        try:
            result = analyze_track(path, models)
        except Exception as e:  # noqa: BLE001 — boundary: error por archivo, sigue
            err += 1
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
            emit(
                event="progress",
                phase="tag",
                done=i,
                total=total,
                file=path.name,
                status="error",
                motivo=f"error de análisis: {e}",
            )
            continue

        # Formato del comment idéntico al de cli/tag.py — analyzer produce
        # los campos, aquí los componemos en el string que va al tag.
        comment_text = (
            f"Energia: {result.energy_level} ({result.energy_value}/9)"
            f" | Genero: {result.genre_top3_text}"
        )

        if simulate:
            # Paridad con cli/tag.py: en simulate, estado='ok' en el CSV.
            # El frontend muestra el banner de simulación aparte.
            status = "ok"
            ok += 1
        else:
            wrote, write_motivo = write_genre_and_comment(
                path, result.genre, comment_text,
            )
            if not wrote:
                err += 1
                rows.append({
                    "estado": "error",
                    "archivo": path.name,
                    "genero_detectado": result.genre,
                    "genero_top3": result.genre_top3_text,
                    "genero_discogs_crudo": result.genre_raw,
                    "confianza_genero": f"{result.genre_confidence:.2f}",
                    "energia_nivel": result.energy_level,
                    "energia_arousal": result.energy_value,
                    "ruta": str(path),
                })
                emit(
                    event="progress",
                    phase="tag",
                    done=i,
                    total=total,
                    file=path.name,
                    status="error",
                    motivo=write_motivo,
                )
                continue
            status = "ok"
            ok += 1

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
        emit(
            event="progress",
            phase="tag",
            done=i,
            total=total,
            file=path.name,
            status="ok",
            genre=result.genre,
            energy_level=result.energy_level,
            arousal=result.energy_value,            # ya redondeado a 1 decimal
            confidence=round(result.genre_confidence, 2),
        )

    # CSV en la carpeta de origen, utf-8-sig para Excel (paridad con cli/tag).
    # Si falla la escritura, no es crítico: la web tiene el estado en memoria;
    # el campo `report` simplemente queda omitido en el evento `done`.
    report_path: Path | None = None
    if rows:
        candidate = source / config.CSV_TAG_REPORT
        try:
            with candidate.open("w", newline="", encoding="utf-8-sig") as f:
                writer = csv.DictWriter(f, fieldnames=_CSV_TAG_FIELDNAMES)
                writer.writeheader()
                writer.writerows(rows)
            report_path = candidate
        except OSError:
            report_path = None

    return ok, err, report_path


def run_enrich(source: Path, simulate: bool) -> None:
    """Fase 2: combina género con nivel de energía leyendo tags actuales.

    Re-recolecta su propia lista (filtro simple, paridad con cli/enrich) —
    no asume nada del estado intermedio de tag, igual que el pipeline CLI.

    Errores de escritura por archivo se reportan con ``status="error"`` y se
    sigue. Casos sin cambio (``sin-cambio``) y skips (``skip: ...``) también
    se emiten como progress — el frontend decide cómo mostrarlos.
    """
    files = _collect_simple(source)
    total = len(files)
    emit(event="phase", phase="enrich", total=total)

    for i, path in enumerate(files, start=1):
        existing = read_tags(path)
        target, motivo = compute_enriched_genre(
            existing.genre, existing.comment, revert=False,
        )

        if motivo.startswith("skip:"):
            emit(
                event="progress",
                phase="enrich",
                done=i,
                total=total,
                file=path.name,
                status="skip",
                motivo=motivo,
            )
            continue

        if motivo == "sin-cambio":
            emit(
                event="progress",
                phase="enrich",
                done=i,
                total=total,
                file=path.name,
                status="sin-cambio",
                motivo=motivo,
            )
            continue

        # Cambio real: motivo es 'enriquecer'.
        if simulate:
            emit(
                event="progress",
                phase="enrich",
                done=i,
                total=total,
                file=path.name,
                status="ok",
                motivo=motivo,
            )
            continue

        wrote, write_msg = write_genre(path, target)
        emit(
            event="progress",
            phase="enrich",
            done=i,
            total=total,
            file=path.name,
            status="ok" if wrote else "error",
            motivo=motivo if wrote else write_msg,
        )


def run_organize(
    source: Path,
    dest: Path,
    simulate: bool,
    move: bool,
    flat: bool,
    no_lossless: bool,
) -> None:
    """Fase 3: organiza archivos a ``<dest>/[Lossless/]<Género>/<Nivel>/``.

    Usa :func:`core.organizer.collect_audio_files` (filtro complejo: excluye
    sidecars, archivos ya bajo ``dest``, y subcarpetas con prefijo ``_``).
    En simulate, ``organize_file(dry_run=True)`` no toca el filesystem pero
    igual emite progress por archivo para que la UI muestre el plan.

    La capa ``Lossless/`` se aplica acá (decisión de presentación, igual que
    en :mod:`cli.organize`) — :func:`core.organizer.compute_target` recibe
    ``dest_base`` ya resuelto.
    """
    dest_base = dest if no_lossless else dest / "Lossless"

    files = collect_audio_files(source, dest)
    total = len(files)
    emit(event="phase", phase="organize", total=total)

    for i, path in enumerate(files, start=1):
        existing = read_tags(path)
        target, base_motivo = compute_target(
            path, existing.genre, dest_base, flat=flat,
        )
        result = organize_file(
            path, target, existing.genre, base_motivo,
            move=move, dry_run=simulate,
        )
        # En simulate, organize_file devuelve status="ok" sin tocar el FS.
        emit(
            event="progress",
            phase="organize",
            done=i,
            total=total,
            file=path.name,
            status="ok" if result.status == "ok" else "error",
            motivo=result.motivo,
        )


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Worker subprocess: corre el pipeline y emite JSONL.",
    )
    parser.add_argument("source", type=Path, help="Carpeta de música a procesar.")
    parser.add_argument(
        "--models", type=Path, required=True, dest="models_dir",
        help="Carpeta con los modelos de Essentia.",
    )
    parser.add_argument(
        "--dest", type=Path, required=True,
        help="Carpeta destino para la fase organize.",
    )
    parser.add_argument(
        "--simulate", action="store_true",
        help="No escribe tags ni copia archivos; emite progreso igual.",
    )
    parser.add_argument(
        "--flat", action="store_true",
        help="organize: una sola carpeta por género (sin subnivel de energía).",
    )
    parser.add_argument(
        "--no-lossless", action="store_true", dest="no_lossless",
        help="organize: omite la subcarpeta Lossless/ intermedia.",
    )
    parser.add_argument(
        "--move", action="store_true",
        help="organize: MUEVE en vez de copiar (vacía el origen).",
    )
    args = parser.parse_args()

    emit(event="start")

    # Validaciones globales: barato, evita pagar los ~10-15 s de carga de TF
    # si algo obvio falta.
    if not args.source.is_dir():
        emit(event="error", message=f"Origen no existe o no es carpeta: {args.source}")
        sys.exit(1)

    missing = validate_models_dir(args.models_dir)
    if missing:
        emit(
            event="error",
            message=(
                f"Faltan modelos en {args.models_dir}: {', '.join(missing)}"
            ),
        )
        sys.exit(1)

    try:
        tag_ok, tag_err, report = run_tag(
            args.source, args.models_dir, args.simulate,
        )
        run_enrich(args.source, args.simulate)
        run_organize(
            args.source, args.dest, args.simulate,
            args.move, args.flat, args.no_lossless,
        )
    except Exception as e:  # noqa: BLE001 — boundary: error global, lo reporta la UI
        emit(event="error", message=f"{type(e).__name__}: {e}")
        sys.exit(1)

    done_event: dict[str, Any] = {
        "event": "done",
        "tag_ok": tag_ok,
        "tag_err": tag_err,
    }
    if report is not None:
        done_event["report"] = str(report)
    emit(**done_event)


if __name__ == "__main__":
    main()
