"""Entry point del CLI ``music-organizer``.

Grupo Click que agrega los subcomandos del pipeline. Cada subcomando vive
en su propio módulo (``cli/<comando>.py``), importa de ``core/`` y se
registra aquí.

Separación estricta de responsabilidades:

- ``cli/`` parsea flags, recolecta archivos, escribe CSV, imprime resumen.
- ``core/`` hace el trabajo computacional sin saber nada de Click ni de CSV.

Comandos registrados: ``tag``, ``enrich``, ``organize``, ``recalibrate``,
``pipeline``, ``serve``, ``resolve``.
"""

from __future__ import annotations

import click

from cli.enrich import enrich_command
from cli.organize import organize_command
from cli.pipeline import pipeline_command
from cli.recalibrate import recalibrate_command
from cli.resolve import resolve_command
from cli.serve import serve_command
from cli.tag import tag_command


@click.group()
@click.version_option("0.1.0", prog_name="music-organizer")
def cli() -> None:
    """Red111 Music Organizer — Smart library tools for DJs.

    \b
    Pipeline de 3 pasos:
      1. tag      — analiza audio, escribe género + energía en los tags.
      2. enrich   — combina género + nivel de energía en un solo tag.
      3. organize — copia archivos a carpetas por género/nivel.

    \b
    Utilidades:
      pipeline    — corre los 3 pasos en orden.
      recalibrate — reajusta niveles de energía sin re-analizar audio.
      serve       — interfaz web local (histograma + sliders).
      resolve     — resuelve un tracklist contra Deezer/iTunes/YouTube.
    """


cli.add_command(tag_command)
cli.add_command(enrich_command)
cli.add_command(organize_command)
cli.add_command(recalibrate_command)
cli.add_command(pipeline_command)
cli.add_command(serve_command)
cli.add_command(resolve_command)


if __name__ == "__main__":
    cli()
