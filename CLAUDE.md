# CLAUDE.md

Contexto operativo para Claude Code al trabajar en este repo. Roadmap de producto y detalle de fases: `docs/ROADMAP.md`.

## Qué es

**Red111 Music Organizer** — "Smart library tools for DJs". Herramienta open source (MIT) para analizar, etiquetar y organizar bibliotecas musicales de DJ con deep learning (Essentia: MAEST + Discogs-400 para género, emoMusic para energía/arousal 1–9), más un **resolver de tracklists** (Deezer/iTunes/YouTube) con tracker de unreleased.

- Repo público: `github.com/Red111-VE/music-organizer`. Branch: **`master`**. Tag publicado: **`v0.1.0`** (pipeline CLI). La Fase 2 (web) está commiteada y pusheada (`e6baae3`).
- Sucesor del repo `dj-organize` (mismo directorio padre): los 5 scripts originales viven allá y como snapshots en `reference_scripts/` aquí (**gitignored**, no se modifican ni lintan). 4 fueron refactorizados a `core/`; el 5º (Camelot/BPM) está **DESCARTADO** — Jorge: rekordbox ya lo hace, no vale la pena.
- Probado sobre la biblioteca real de Jorge: 471 tracks FLAC house/tech-house en `/Volumes/Untitled/deemix_Music`.

## Estructura

```
core/    # 11 módulos de lógica pura — SIN Click, rich ni FastAPI:
         #   pipeline:  config, tagger, models, analyzer, enricher,
         #              organizer, recalibrator
         #   resolver:  tracklist (parser), sources (URL→texto),
         #              resolver (Deezer/iTunes/YouTube + scoring),
         #              unreleased (tracker persistente)
cli/     # 7 comandos Click (tag, enrich, organize, recalibrate, pipeline,
         #   serve, resolve) — thin wrappers, flags bilingües
web/     # worker.py (subprocess JSONL), state.py (estado + pub/sub),
         #   runner.py, main.py (FastAPI), routes/ (api, pages, resolver),
         #   templates/ (base + 4 pantallas + _resolver_panel partial),
         #   static/ (app.css, histogram.js)
tests/   # 459 tests
```

Imports de essentia/mutagen/rapidfuzz son **lazy en todo el proyecto** — mantener ese patrón. Los modelos `.pb`/`.json` van en `~/essentia_models`, **nunca** al repo (`models/` solo tiene `.gitkeep`).

## ESTADO ACTUAL

El **v0.3.x (resolver de tracklists) está completo, validado por Jorge y commiteado** junto con la actualización de docs (README/ROADMAP/CHANGELOG/CLAUDE.md). Pendiente: **decidir la numeración del tag** (el roadmap llamaba "v0.2.x" al Camelot/BPM descartado — la web + resolver podrían salir como v0.2.0 o v0.3.0, decisión de Jorge) y publicar el release en GitHub.

`mypy core/ cli/` y `ruff check .` están **limpios en todo el árbol** (la deuda de 19 errores en `cli/serve.py` se pagó).

Otro pendiente de producto: la recalibración de la biblioteca real (471 tracks) con `recalibrate --auto-calibrate` aún no se ejecutó.

## Decisiones técnicas CERRADAS (no reabrir sin causa)

Evaluadas, verificadas empíricamente y aprobadas.

### Pipeline + web (Fase 2)

- **Web local, 1 usuario, 1 job a la vez.** Sin Celery/Redis/colas. Subprocess + estado en memoria. No es multi-usuario y no debe serlo.
- **Opción B de integración**: la web usa `core/` directamente vía el worker JSONL (un evento por línea, flush inmediato). NUNCA parsear el stdout del CLI (frágil).
- **Stderr del worker → tempfile, no PIPE.** Essentia/TF escupen miles de warnings; un pipe de 64KB se llena y deadlockea (`tests/test_runner.py` es la regresión). No volver a `stderr=PIPE`.
- **`mark_cancelled()` ANTES de `terminate()`** en `cancel_pipeline` — el orden evita que el EOF marque un cancel como error falso.
- **El WebSocket SIEMPRE hace `await gen.aclose()` en finally** — sin eso queda una cola zombie de suscriptor llenándose (verificado).
- **El subprocess sobrevive la desconexión del cliente.** Cancelación solo explícita (botón). Al recargar, la UI se reconstruye desde `GET /api/state`.
- **`start_pipeline` tiene `except Exception` (no BaseException)** que traduce bugs inesperados a `mark_error` — el state nunca queda colgado en `running`.
- **Paridad numérica cliente↔servidor**: `histogram.js` replica exactamente `energy_bucket` (`<` estricto) y `statistics.quantiles` exclusive de Python (verificado con los 471 arousals reales: 5.9/6.1/6.4). Si se toca uno, sincronizar el otro.
- **`serve` default loopback (127.0.0.1)** — exponer requiere `--host 0.0.0.0` explícito con warning.
- **Paridad de CSVs con los scripts originales** — columnas, estados y nomenclatura exactos. Diff-eables contra reportes históricos.
- **`pipeline` NO incluye `recalibrate`** — operación posterior y manual.
- **La web del pipeline hardcodea `flat=True` y `no_lossless=True`** (decisión de Jorge: solo carpeta por género, la energía queda en el tag, sin subcarpeta `Lossless/`). El CLI conserva ambos flags.
- **`dest` no puede ser igual ni estar anidado con `source`** — validado en server (400) y cliente (warning + submit deshabilitado). Sin esto, organize procesa 0 archivos en silencio.
- **Paths del form se normalizan** (strip de comillas/espacios, expand `~`) — Finder pega con comillas y los usuarios escriben `~`.

### Resolver de tracklists (v0.3.x)

- **Costo cero, sin LLM**: el parser es regex/heurístico determinístico. Si algún día entra normalización LLM (BYOK), va como pre-procesador opcional, nunca en reemplazo.
- **El parser descarta ruido social** (líneas cuyo artista/título contiene URL o email): son universales en descripciones de YouTube y sin el filtro 3 links sociales parecen un tracklist. El parser es también el *detector* (gana el texto con más líneas parseables).
- **Cascada de proveedores**: Deezer (sin auth, rate generoso) → iTunes (sin auth, ~20 req/min → solo fallback, throttle 3.1s) → YouTube (key gratuita, `search.list` cuesta 100 unidades → último recurso, score capeado a 88 por metadata no curada). Siempre se adjunta el link de búsqueda manual de YouTube (costo cero).
- **Fuentes de URL**: YouTube (descripción Y comentarios — gana el de más tracks; cortar temprano por la descripción elige los links sociales) y Mixcloud (API abierta; `sections` llega vacío hace años por licencias, se usa `description`). **SoundCloud y Beatport tienen APIs cerradas y 1001Tracklists prohíbe scraping — NO se integran**; el fallback es pegar el texto.
- **Scoring calibrado contra las APIs reales** (QA de 23 casos + regresiones): núcleo del remix (sin palabras genéricas) contra los paréntesis del candidato; títulos base-vs-base de AMBOS lados (los feat no penalizan); la presencia del remix solo evita la penalización, no infla; Live/[Mixed] no pedidos se degradan ×0.85; piso de título (artista 100% no convierte "Home" en "Domine"). Umbrales ok≥82/dudoso≥60. **No tocar los números sin re-calibrar contra las APIs.** Limitación aceptada: títulos a 1 carácter con el mismo remixer (~86) — distinguirlos mataría la tolerancia a typos.
- **Tracker de unreleased** (`~/.music-organizer/unreleased.json`): solo no-encontrados **con nombre** y **sin errores de proveedor** (un corte de red no debe contaminar el tracker con tracks que existen); los IDs nunca (nada que re-buscar); archivo corrupto JAMÁS se pisa en silencio (error accionable); escritura atómica; clave normalizada con fallback crudo para texto no-latino.
- **Web del resolver: track-por-track desde el frontend** (un endpoint por track, filas pintándose en vivo). Sin WS ni subprocess — cerrar la pestaña corta el loop, no hay job huérfano. Handlers `def` (no `async`): el throttle duerme y FastAPI los corre en threadpool sin bloquear el event loop del WS del pipeline.
- **El panel del resolver es un partial compartido** (`_resolver_panel.html`): embebido en `/` (lado a lado con el análisis, grilla `tools-grid` + `main--wide`) y en la vista dedicada `/resolver`. `YOUTUBE_API_KEY` por env, compartida CLI/web.

## Método de trabajo

- **División**: el chat de diseño/revisión decide y revisa; **Claude Code ejecuta**. Cada componente se construye → se revisa → se aprueba o ajusta. Componentes aprobados no se re-revisan enteros; los cambios acotados se revisan en aislado.
- **Las desviaciones del comportamiento original las decide Jorge**, no el código. Toda desviación se registra en `CHANGELOG.md`.
- Bugs sutiles (races, deadlocks, paridad numérica, calibración de matching) se verifican **empíricamente** — scripts de repro y QA contra las APIs reales, no argumentación. Cada fix entra con su test de regresión.
- Antes de cualquier push: revisar el staging (`git status`) para no colar CSVs personales, audio, modelos ni `reference_scripts/`.

## Comandos de desarrollo

El venv del repo está en `venv/` (Python 3.11, con pytest/ruff/mypy). Usar `./venv/bin/<tool>`.

```bash
pip install -e '.[web,dev]'        # instalación de desarrollo
./venv/bin/pytest                  # 459 tests
./venv/bin/ruff check .
./venv/bin/mypy core/ cli/

# Pipeline
music-organizer pipeline <musica> --models ~/essentia_models --dest <salida>
music-organizer recalibrate <musica> --auto-calibrate
music-organizer serve --open       # web en http://127.0.0.1:8000

# Resolver de tracklists
music-organizer resolve tracklist.txt
music-organizer resolve "https://youtu.be/…"   # necesita YOUTUBE_API_KEY
pbpaste | music-organizer resolve -
music-organizer resolve --recheck              # ¿salió algún unreleased?
```

En tests, el tracker de unreleased SIEMPRE se apunta a `tmp_path` (CLI: `--store`; web: `web.routes.resolver.STORE_PATH`) — jamás tocar `~/.music-organizer` real.

## Gotcha de essentia-tensorflow

Instalar **solo** `essentia-tensorflow`, nunca el paquete `essentia` a secas a la vez: si coexisten, `essentia.tensorflow` queda shadowed y los modelos TF no cargan. Fix: `pip uninstall essentia essentia-tensorflow -y && pip install essentia-tensorflow`.

## Datos de la biblioteca real (referencia para calibración)

- 471 tracks FLAC house/tech-house. Distribución: Tech House 37.4%, House 34.8%, Techno 13.8%, Deep House 4.2%.
- Arousal: min 5.1, max 7.6, mediana 6.1. Percentiles 25/50/75 = **5.9/6.1/6.4**.
- Con umbrales default (4.8/5.8/6.6) el 70% cae en ALTA → por eso existe `recalibrate --auto-calibrate`. La recalibración de la biblioteca real aún no se ha ejecutado.
