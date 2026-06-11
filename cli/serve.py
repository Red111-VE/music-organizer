"""Comando ``serve``: levanta la interfaz web local con uvicorn.

Decisiones
----------

- **Default ``host=127.0.0.1``** (loopback). La web opera sobre el filesystem
  local con permisos del usuario y no tiene auth ni rate limit; exponerla a
  la red sería un riesgo. Si el usuario quiere acceso desde otra máquina,
  tiene que pasar explícitamente ``--host 0.0.0.0`` (mostramos un warning).
- **Default ``port=8000``** — convención. ``--port`` para cambiarlo.
- **``--reload`` opt-in**: modo desarrollo, uvicorn vigila ``web/``, ``core/``
  y ``cli/`` (paths absolutos resueltos desde este archivo, así funciona
  desde cualquier CWD).
- **``--open`` opt-in**: abre el navegador con ``webbrowser.open`` desde un
  ``Timer`` daemon (no bloquea ``uvicorn.run``, no impide el shutdown).
- **Validación de deps**: fallamos rápido con mensaje accionable si falta
  algo del extra ``[web]`` — sin esto el ImportError de uvicorn explota
  feo y no orienta al usuario.

El comando se registra en :mod:`cli.main` junto a los otros subcomandos.
"""

from __future__ import annotations

import threading
import webbrowser
from pathlib import Path

import click
from rich.console import Console


# Módulos del extra [web] que necesitamos validar antes de arrancar.
_WEB_DEPS = ("fastapi", "uvicorn", "jinja2")

# Paths absolutos para ``reload_dirs``. Si el usuario corre el comando
# desde otra carpeta, las paths relativas no funcionarían.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_RELOAD_DIRS = [str(_PROJECT_ROOT / d) for d in ("web", "core", "cli")]


def _check_web_deps() -> None:
    """Falla con un mensaje accionable si falta algo del extra ``[web]``.

    Sin esto, el ``import uvicorn`` de abajo lanzaría ``ModuleNotFoundError``
    sin contexto. Acá listamos exactamente qué falta y cómo instalarlo.
    """
    missing = []
    for mod in _WEB_DEPS:
        try:
            __import__(mod)
        except ImportError:
            missing.append(mod)
    if missing:
        raise click.ClickException(
            f"Faltan dependencias del extra [web]: {', '.join(missing)}.\n"
            f"Instalalas con:\n"
            f"    pip install -e '.[web]'"
        )


@click.command("serve")
@click.option(
    "--host",
    default="127.0.0.1",
    show_default=True,
    help=(
        "Interfaz de red. Default loopback (solo accesible desde esta "
        "máquina). Cambiar a 0.0.0.0 expone la web — solo en red de "
        "confianza."
    ),
)
@click.option(
    "--port",
    default=8000,
    type=int,
    show_default=True,
    help="Puerto en el que escuchar.",
)
@click.option(
    "--reload",
    is_flag=True,
    help="Modo desarrollo: reinicia el server cuando cambian web/ core/ cli/.",
)
@click.option(
    "--open",
    "open_browser",
    is_flag=True,
    help="Abre el navegador automáticamente al arrancar.",
)
def serve_command(
    host: str,
    port: int,
    reload: bool,
    open_browser: bool,
) -> None:
    """Inicia la interfaz web local."""
    _check_web_deps()

    # Lazy import — el resto del CLI no debe pagar el costo de uvicorn.
    import uvicorn

    console = Console()

    # Para el mensaje y el navegador, siempre mostramos 127.0.0.1 cuando
    # el host es 0.0.0.0 (un browser abriendo http://0.0.0.0:port es
    # comportamiento undefined).
    display_host = "127.0.0.1" if host == "0.0.0.0" else host
    url = f"http://{display_host}:{port}"

    # Banner.
    console.print()
    console.print(
        "[bold red]●[/bold red] [bold]RED111 · music-organizer[/bold]"
    )
    console.print(f"  Web UI corriendo en [link={url}]{url}[/link]")
    if host == "0.0.0.0":
        console.print(
            "  [yellow]Atención:[/yellow] --host 0.0.0.0 expone la web a la red."
        )
    if reload:
        console.print(
            "  [dim]Modo reload activo: cambios en web/ core/ cli/ reinician el server.[/dim]"
        )
    console.print("  Detener con [dim]Ctrl+C[/dim].")
    console.print()

    # Apertura opcional del navegador. ``daemon=True`` para que el Timer
    # no impida el shutdown si el usuario Ctrl-Cs antes de los 1.2s.
    if open_browser:
        t = threading.Timer(1.2, lambda: webbrowser.open(url))
        t.daemon = True
        t.start()

    # Uvicorn bloquea hasta SIGINT/SIGTERM. Pasamos la app como string
    # porque es lo que ``reload=True`` requiere (uvicorn re-importa cada
    # vez que detecta cambios). Sin reload también funciona como string.
    run_kwargs: dict[str, object] = {
        "host": host,
        "port": port,
        "log_level": "info",
    }
    if reload:
        run_kwargs["reload"] = True
        run_kwargs["reload_dirs"] = _RELOAD_DIRS

    uvicorn.run("web.main:app", **run_kwargs)
