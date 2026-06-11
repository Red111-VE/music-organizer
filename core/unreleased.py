"""Tracker persistente de tracks no encontrados (unreleased).

Cuarto componente del resolver de tracklists (v0.3.x). Los tracks con
nombre que ningún catálogo tiene hoy (típicamente unreleased que suenan en
sets meses antes de salir) se anotan en un JSON persistente; el comando
``resolve --recheck`` los re-consulta y avisa cuando alguno por fin salió.

**Qué se guarda y qué no**: solo tracks con artista Y título conocidos que
resolvieron ``no-encontrado``. Los ``ID - ID`` no se guardan — un ID es
por definición no identificable por texto, no hay query que re-intentar.
Los ``dudoso`` tampoco: ya tienen un candidato que el usuario puede
revisar a mano.

**Dónde**: ``~/.music-organizer/unreleased.json`` por defecto — a nivel
usuario, no por carpeta: el mismo track unreleased aparece en sets de
fuentes distintas y el tracker los deduplica (vía la normalización del
resolver, así "Trentemøller" y "Trentemoller" son la misma entrada;
``times_seen`` cuenta las apariciones — un unreleased que aparece en 5
sets distintos probablemente valga la pena seguirlo).

**Escritura atómica**: tempfile en el mismo directorio + ``os.replace`` —
un Ctrl-C a mitad de la escritura no corrompe el archivo.

**Archivo corrupto**: :class:`UnreleasedStoreError` con mensaje accionable
(qué archivo, qué hacer). NUNCA se pisa en silencio un archivo que no se
pudo leer — puede tener meses de datos del usuario. El CLI decide si el
error es fatal (modo ``--recheck``) o solo un warning (modo resolve
normal, donde el tracker es secundario).
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any, Callable

from core.resolver import TrackResolution, normalize, resolve_track
from core.tracklist import ParsedTrack


_STORE_VERSION = 1

DEFAULT_STORE_PATH = Path.home() / ".music-organizer" / "unreleased.json"


class UnreleasedStoreError(Exception):
    """El archivo del tracker no se pudo leer/escribir. Mensaje accionable
    (incluye el path); el caller decide si es fatal o warning."""


@dataclass
class UnreleasedEntry:
    """Un track con nombre que ningún catálogo tenía al momento de verlo.

    ``times_seen`` se incrementa cada vez que el track reaparece en otro
    tracklist — señal de cuán "buscado" es el unreleased.
    """
    artist: str
    title: str
    remix: str = ""
    first_seen: str = ""     # fecha ISO (YYYY-MM-DD)
    last_checked: str = ""   # fecha ISO del último recheck
    times_seen: int = 1

    @property
    def key(self) -> str:
        """Clave de deduplicación: normalización difusa del resolver, así
        variantes de grafía/acentos son la misma entrada.

        Fallback crudo para texto 100% no-latino: ``normalize`` solo
        conserva ``[a-z0-9]``, así que un título en cirílico/japonés
        normaliza a "" — sin el fallback, dos tracks distintos
        colisionarían en la clave vacía y se pisarían (verificado)."""
        normalized = normalize(f"{self.artist} {self.title} {self.remix}")
        if normalized:
            return normalized
        return f"{self.artist}|{self.title}|{self.remix}".casefold()

    def to_parsed_track(self) -> ParsedTrack:
        """Reconstruye el :class:`ParsedTrack` para re-resolver."""
        raw = f"{self.artist} - {self.title}"
        if self.remix:
            raw += f" ({self.remix})"
        return ParsedTrack(
            raw=raw, line_no=0,
            artist=self.artist, title=self.title, remix=self.remix,
        )


class UnreleasedStore:
    """Colección persistente de :class:`UnreleasedEntry`, keyed por la
    clave normalizada. Carga en el constructor (archivo ausente = store
    vacío); :meth:`save` es explícito y atómico.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or DEFAULT_STORE_PATH
        self._entries: dict[str, UnreleasedEntry] = {}
        self._load()

    # --- API pública -------------------------------------------------------

    @property
    def entries(self) -> list[UnreleasedEntry]:
        return list(self._entries.values())

    def __len__(self) -> int:
        return len(self._entries)

    def add(self, track: ParsedTrack, *, today: str | None = None) -> bool:
        """Anota un track no encontrado. Devuelve ``True`` si es nuevo,
        ``False`` si ya estaba (incrementa ``times_seen``) o si el track
        no es trackeable (ID, o sin artista/título)."""
        if track.is_id or not track.artist or not track.title:
            return False
        entry = UnreleasedEntry(
            artist=track.artist,
            title=track.title,
            remix=track.remix,
            first_seen=today or date.today().isoformat(),
            last_checked=today or date.today().isoformat(),
        )
        existing = self._entries.get(entry.key)
        if existing is not None:
            existing.times_seen += 1
            return False
        self._entries[entry.key] = entry
        return True

    def remove(self, key: str) -> None:
        self._entries.pop(key, None)

    def save(self) -> None:
        """Escritura atómica: tempfile en el mismo directorio +
        ``os.replace``. Un corte a mitad nunca deja el JSON corrupto."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": _STORE_VERSION,
                "tracks": [asdict(e) for e in self._entries.values()],
            }
            fd, tmp_path = tempfile.mkstemp(
                dir=str(self.path.parent),
                prefix=".unreleased-", suffix=".tmp",
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False, indent=2)
                os.replace(tmp_path, self.path)
            except BaseException:
                # No dejar tempfiles huérfanos ni en Ctrl-C.
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except OSError as e:
            raise UnreleasedStoreError(
                f"No se pudo escribir el tracker en {self.path}: {e}"
            ) from e

    # --- Interno -----------------------------------------------------------

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            with self.path.open(encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            raise UnreleasedStoreError(
                f"El tracker de unreleased está corrupto o ilegible: "
                f"{self.path}\n({e})\n"
                f"Renombralo o borralo para empezar de cero — NO se pisa "
                f"automáticamente porque puede tener datos tuyos."
            ) from e

        if not isinstance(data, dict) or data.get("version") != _STORE_VERSION:
            raise UnreleasedStoreError(
                f"El tracker {self.path} tiene un formato desconocido "
                f"(¿de una versión más nueva?). Renombralo para empezar "
                f"de cero."
            )

        for raw in data.get("tracks") or []:
            entry = _entry_from_dict(raw)
            if entry is not None:
                self._entries[entry.key] = entry


def _entry_from_dict(raw: Any) -> UnreleasedEntry | None:
    """Entry desde el JSON, defensivo: shapes inesperados se saltan (mejor
    perder una entrada ilegible que abortar el tracker entero)."""
    if not isinstance(raw, dict):
        return None
    artist = raw.get("artist")
    title = raw.get("title")
    if not isinstance(artist, str) or not isinstance(title, str):
        return None
    if not artist or not title:
        return None
    remix = raw.get("remix")
    first_seen = raw.get("first_seen")
    last_checked = raw.get("last_checked")
    times_seen = raw.get("times_seen")
    return UnreleasedEntry(
        artist=artist,
        title=title,
        remix=remix if isinstance(remix, str) else "",
        first_seen=first_seen if isinstance(first_seen, str) else "",
        last_checked=last_checked if isinstance(last_checked, str) else "",
        times_seen=times_seen if isinstance(times_seen, int) and times_seen > 0 else 1,
    )


# --------------------------------------------------------------------------- #
# Recheck
# --------------------------------------------------------------------------- #

def recheck_store(
    store: UnreleasedStore,
    *,
    youtube_api_key: str | None = None,
    on_result: Callable[[UnreleasedEntry, TrackResolution], None] | None = None,
) -> list[tuple[UnreleasedEntry, TrackResolution]]:
    """Re-consulta cada entrada del tracker contra los catálogos.

    - ``ok`` → ¡salió!: se remueve del tracker y se devuelve en la lista.
    - ``dudoso`` / ``no-encontrado`` → queda, con ``last_checked`` al día.

    Guarda el store al final (una sola escritura). ``on_result`` permite
    al CLI mostrar progreso por entrada sin que core sepa de rich.
    """
    found: list[tuple[UnreleasedEntry, TrackResolution]] = []
    today = date.today().isoformat()

    for entry in store.entries:
        resolution = resolve_track(
            entry.to_parsed_track(), youtube_api_key=youtube_api_key,
        )
        entry.last_checked = today
        if resolution.status == "ok":
            store.remove(entry.key)
            found.append((entry, resolution))
        if on_result is not None:
            on_result(entry, resolution)

    store.save()
    return found
