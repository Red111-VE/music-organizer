"""Comando ``resolve``: resuelve un tracklist contra catálogos públicos.

::

    music-organizer resolve tracklist.txt          # archivo
    music-organizer resolve "https://youtu.be/…"   # URL (YouTube/Mixcloud)
    pbpaste | music-organizer resolve -            # stdin
    music-organizer resolve "Artist - Title"       # texto literal
    music-organizer resolve --recheck              # re-consultar unreleased

Thin wrapper: el parseo vive en :mod:`core.tracklist`, la obtención por
URL en :mod:`core.sources`, la resolución en :mod:`core.resolver` y el
tracker en :mod:`core.unreleased`. Aquí: detección del tipo de input,
barra de progreso, tabla de resultados, CSV de reporte y resumen.

Detección del input (en orden): ``-`` → stdin; URL (http/www o dominio
soportado) → :func:`core.sources.fetch_tracklist_text`; archivo existente
→ se lee; cualquier otra cosa → texto literal (útil para un solo track).

**Expectativas explícitas**: los IDs y los tracks unreleased no existen en
catálogos públicos — el resumen lo dice siempre. Esta herramienta ayuda a
encontrar lo encontrable; el resto va al tracker para ``--recheck``.

La key de YouTube es opcional (``--youtube-key`` o env ``YOUTUBE_API_KEY``):
sin ella no hay búsqueda en YouTube ni lectura de URLs de YouTube, pero
Deezer/iTunes/Mixcloud funcionan igual y cada track lleva siempre su link
de búsqueda manual de YouTube (costo cero).

Genera ``_REPORTE_TRACKLIST.csv`` en el directorio actual (no hay "carpeta
de origen" cuando el input es stdin/URL), UTF-8 con BOM como los demás
reportes del proyecto.
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path
from typing import Any

import click
from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table

from core.resolver import TrackResolution, resolve_track
from core.sources import SourceError, detect_source, fetch_tracklist_text
from core.tracklist import parse_tracklist
from core.unreleased import (
    DEFAULT_STORE_PATH,
    UnreleasedStore,
    UnreleasedStoreError,
    recheck_store,
)


_CSV_NAME = "_REPORTE_TRACKLIST.csv"

_CSV_FIELDNAMES = [
    "estado",
    "linea",
    "artista",
    "titulo",
    "remix",
    "proveedor",
    "match_artista",
    "match_titulo",
    "confianza",
    "url",
    "busqueda_youtube",
    "notas",
]

# Colores por estado para la tabla.
_STATUS_STYLE = {
    "ok": "green",
    "dudoso": "yellow",
    "no-encontrado": "red",
    "id": "dim",
}


@click.command("resolve")
@click.argument("source", required=False)
@click.option(
    "--youtube-key",
    "youtube_key",
    envvar="YOUTUBE_API_KEY",
    default=None,
    help="API key de YouTube (gratuita). También por env YOUTUBE_API_KEY. "
         "Sin ella: no se leen URLs de YouTube ni se busca en YouTube, "
         "pero Deezer/iTunes/Mixcloud funcionan igual.",
)
@click.option(
    "--limit", "--limite",
    "limit",
    type=int,
    default=0,
    help="Resolver solo los primeros N tracks (0 = todos).",
)
@click.option(
    "--recheck", "--reverificar",
    "recheck",
    is_flag=True,
    help="Re-consultar los tracks del tracker de unreleased y avisar "
         "cuáles ya salieron. No lleva input.",
)
@click.option(
    "--store",
    "store_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help=f"Archivo del tracker de unreleased (default: {DEFAULT_STORE_PATH}).",
)
def resolve_command(
    source: str | None,
    youtube_key: str | None,
    limit: int,
    recheck: bool,
    store_path: Path | None,
) -> None:
    """Resuelve un tracklist: encuentra cada track en Deezer/iTunes/YouTube.

    SOURCE puede ser un archivo, una URL (YouTube/Mixcloud), "-" para
    stdin, o el texto del track directamente. Con --recheck no lleva
    SOURCE: re-consulta los unreleased anotados.
    """
    console = Console()

    if recheck:
        if source is not None:
            raise click.ClickException(
                "--recheck no lleva input — re-consulta el tracker existente."
            )
        _run_recheck(console, store_path, youtube_key)
        return

    if source is None:
        raise click.ClickException(
            "Falta el input. Usos:\n"
            "  music-organizer resolve tracklist.txt\n"
            '  music-organizer resolve "https://youtu.be/…"\n'
            "  pbpaste | music-organizer resolve -\n"
            "  music-organizer resolve --recheck"
        )

    # --- Obtener el texto del tracklist ------------------------------------
    text, origin = _read_input(source, youtube_key)
    tracks = parse_tracklist(text)
    if limit:
        tracks = tracks[:limit]
    if not tracks:
        raise click.ClickException(
            "No encontré líneas de track parseables en el input. "
            "Formato esperado por línea: «Artista - Título»."
        )

    console.print(f"Tracklist: {origin}")
    console.print(f"Tracks a resolver: {len(tracks)}")
    if not youtube_key:
        console.print(
            "[dim]Sin YouTube API key — solo Deezer/iTunes "
            "(export YOUTUBE_API_KEY para sumar YouTube).[/dim]"
        )
    console.print("=" * 64)

    # --- Resolver con progreso ----------------------------------------------
    resolutions: list[TrackResolution] = []
    progress = Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        TextColumn("•"),
        TimeElapsedColumn(),
        console=console,
    )
    with progress:
        task_id = progress.add_task("Resolviendo", total=len(tracks))
        for track in tracks:
            resolutions.append(
                resolve_track(track, youtube_api_key=youtube_key)
            )
            progress.advance(task_id)

    # --- Tabla ---------------------------------------------------------------
    console.print()
    console.print(_build_table(resolutions))

    # --- Tracker de unreleased ------------------------------------------------
    new_tracked = _update_store(console, store_path, resolutions)

    # --- CSV -------------------------------------------------------------------
    report_path = _write_csv(console, resolutions)

    # --- Resumen ----------------------------------------------------------------
    counts = {"ok": 0, "dudoso": 0, "no-encontrado": 0, "id": 0}
    for r in resolutions:
        counts[r.status] = counts.get(r.status, 0) + 1

    console.print()
    console.print("=" * 64)
    console.print("  RESUMEN")
    console.print("=" * 64)
    console.print(f"  Encontrados    : {counts['ok']}")
    console.print(f"  Dudosos        : {counts['dudoso']}")
    console.print(f"  No encontrados : {counts['no-encontrado']}")
    console.print(f"  IDs            : {counts['id']}")
    if new_tracked:
        console.print(
            f"\n  {new_tracked} track(s) anotado(s) en el tracker de "
            "unreleased."
        )
        console.print(
            "  Re-consultalos cuando quieras: music-organizer resolve --recheck"
        )
    if report_path:
        console.print(f"\n  Reporte: {report_path}")
    console.print(
        "\n  [dim]Los IDs y los tracks unreleased no existen en catálogos "
        "públicos —\n  encontrar todo es imposible por diseño, no un error "
        "de la herramienta.[/dim]"
    )
    console.print("=" * 64)


# --------------------------------------------------------------------------- #
# Input
# --------------------------------------------------------------------------- #

def _read_input(source: str, youtube_key: str | None) -> tuple[str, str]:
    """Texto del tracklist + descripción de su origen (para el header).

    Orden de detección: stdin → URL → archivo → texto literal. El error de
    una URL soportada-pero-fallida (key faltante, video sin tracklist) se
    presenta accionable, sin traceback.
    """
    if source == "-":
        text = sys.stdin.read()
        return text, "stdin"

    looks_like_url = (
        detect_source(source) is not None
        or source.lower().startswith(("http://", "https://", "www."))
    )
    if looks_like_url:
        try:
            result = fetch_tracklist_text(source, youtube_api_key=youtube_key)
        except SourceError as e:
            raise click.ClickException(str(e)) from e
        title = f" — «{result.title}»" if result.title else ""
        return result.text, f"{result.origin}{title}"

    path = Path(source)
    try:
        if path.is_file():
            return (
                path.read_text(encoding="utf-8", errors="replace"),
                str(path),
            )
        if path.exists():
            # Existe pero no es archivo (directorio, socket, …).
            raise click.ClickException(
                f"{source} existe pero no es un archivo legible."
            )
    except OSError as e:
        raise click.ClickException(f"No pude leer {path}: {e}") from e

    # Antes de tratarlo como texto literal: si parece una RUTA (sin
    # separador artista-título ni saltos de línea), el usuario casi seguro
    # tipeó mal un archivo — el error de "0 tracks parseables" sería
    # confuso.
    looks_like_text = "\n" in source or " - " in source
    if not looks_like_text:
        raise click.ClickException(
            f"No existe el archivo «{source}» (y no parece texto de "
            "tracklist — esos llevan «Artista - Título» por línea)."
        )

    # Texto literal (un track suelto o varios pegados con \n del shell).
    return source, "texto directo"


# --------------------------------------------------------------------------- #
# Salida
# --------------------------------------------------------------------------- #

def _build_table(resolutions: list[TrackResolution]) -> Table:
    table = Table(show_lines=False, pad_edge=False)
    table.add_column("#", justify="right", style="dim")
    table.add_column("Estado")
    table.add_column("Track buscado", overflow="fold")
    table.add_column("Match", overflow="fold")
    table.add_column("Conf", justify="right")
    table.add_column("Link", overflow="fold", style="dim")

    for r in resolutions:
        style = _STATUS_STYLE.get(r.status, "")
        wanted = r.track.query if not r.track.is_id else r.track.raw
        if r.best is not None:
            match = f"{r.best.artist} - {r.best.title} [{r.best.provider}]"
            conf = f"{r.best.score:.0f}"
        else:
            match = "—"
            conf = ""
        # Link: el match si es confiable/dudoso; si no, la búsqueda manual.
        link = (
            r.best.url
            if r.best is not None and r.status in ("ok", "dudoso")
            else r.youtube_search_url
        )
        table.add_row(
            str(r.track.line_no),
            f"[{style}]{r.status}[/{style}]" if style else r.status,
            wanted,
            match,
            conf,
            link or "",
        )
    return table


def _update_store(
    console: Console,
    store_path: Path | None,
    resolutions: list[TrackResolution],
) -> int:
    """Anota los no-encontrados con nombre en el tracker. Devuelve cuántos
    son nuevos. Un tracker ilegible es un warning, no un error fatal — acá
    el tracker es secundario al resolve."""
    try:
        store = UnreleasedStore(store_path)
    except UnreleasedStoreError as e:
        console.print(f"[yellow]AVISO:[/yellow] {e}")
        return 0

    new_count = 0
    for r in resolutions:
        # Solo anotamos no-encontrados LIMPIOS: si hubo errores de
        # proveedor (notes), el track pudo no encontrarse por un corte de
        # red — anotarlo contaminaría el tracker con tracks que sí
        # existen. Se reintentará en el próximo resolve.
        if r.status == "no-encontrado" and not r.notes:
            if store.add(r.track):
                new_count += 1
    try:
        store.save()
    except UnreleasedStoreError as e:
        console.print(f"[yellow]AVISO:[/yellow] {e}")
        return 0
    return new_count


def _write_csv(
    console: Console, resolutions: list[TrackResolution],
) -> Path | None:
    """CSV en el directorio actual (no hay carpeta de origen con
    stdin/URL). UTF-8 con BOM, como todos los reportes del proyecto."""
    rows: list[dict[str, Any]] = []
    for r in resolutions:
        rows.append({
            "estado": r.status,
            "linea": r.track.line_no,
            "artista": r.track.artist,
            "titulo": r.track.title,
            "remix": r.track.remix,
            "proveedor": r.best.provider if r.best else "",
            "match_artista": r.best.artist if r.best else "",
            "match_titulo": r.best.title if r.best else "",
            "confianza": f"{r.best.score:.1f}" if r.best else "",
            "url": r.best.url if r.best else "",
            "busqueda_youtube": r.youtube_search_url,
            "notas": " | ".join(r.notes),
        })

    target = Path.cwd() / _CSV_NAME
    try:
        with target.open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=_CSV_FIELDNAMES)
            writer.writeheader()
            writer.writerows(rows)
        return target
    except OSError as e:
        console.print(f"AVISO: no se pudo escribir el reporte: {e}")
        return None


# --------------------------------------------------------------------------- #
# Recheck
# --------------------------------------------------------------------------- #

def _run_recheck(
    console: Console,
    store_path: Path | None,
    youtube_key: str | None,
) -> None:
    """Modo --recheck: re-consulta el tracker. Acá un tracker ilegible SÍ
    es fatal — es la única razón de ser del modo."""
    try:
        store = UnreleasedStore(store_path)
    except UnreleasedStoreError as e:
        raise click.ClickException(str(e)) from e

    if len(store) == 0:
        console.print(
            "El tracker de unreleased está vacío — nada que re-consultar."
        )
        console.print(
            "[dim]Se llena solo: cada `resolve` anota los tracks con "
            "nombre que ningún catálogo tuvo.[/dim]"
        )
        return

    console.print(f"Re-consultando {len(store)} track(s) del tracker…")
    console.print("=" * 64)

    progress = Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        TextColumn("•"),
        TimeElapsedColumn(),
        console=console,
    )
    with progress:
        task_id = progress.add_task("Re-consultando", total=len(store))
        found = recheck_store(
            store,
            youtube_api_key=youtube_key,
            on_result=lambda entry, res: progress.advance(task_id),
        )

    console.print()
    if found:
        console.print(f"[green]¡{len(found)} track(s) ya salieron![/green]")
        table = Table(show_lines=False, pad_edge=False)
        table.add_column("Track", overflow="fold")
        table.add_column("Match", overflow="fold")
        table.add_column("Link", overflow="fold", style="dim")
        for entry, res in found:
            assert res.best is not None  # status ok ⇒ best presente
            table.add_row(
                f"{entry.artist} - {entry.title}"
                + (f" ({entry.remix})" if entry.remix else ""),
                f"{res.best.artist} - {res.best.title} [{res.best.provider}]",
                res.best.url,
            )
        console.print(table)
    else:
        console.print("Ninguno salió todavía — siguen en el tracker.")
    console.print(f"\nQuedan {len(store)} track(s) en seguimiento.")
