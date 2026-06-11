# CLAUDE.md

Contexto operativo para Claude Code al trabajar en este repo. Roadmap de producto y detalle de fases: `docs/ROADMAP.md`.

## Qué es

**Red111 Music Organizer** — "Smart library tools for DJs". Herramienta open source (MIT) para analizar, etiquetar y organizar bibliotecas musicales de DJ con deep learning (Essentia: MAEST + Discogs-400 para género, emoMusic para energía/arousal 1–9).

- Repo público: `github.com/Red111-VE/music-organizer`. Branch: **`master`**. Tag publicado: **`v0.1.0`** (pipeline CLI).
- Sucesor del repo `dj-organize` (mismo directorio padre): los 5 scripts originales viven allá y como snapshots en `reference_scripts/` aquí (**gitignored**, no se modifican ni lintan — son referencia comparativa del refactor). 4 fueron refactorizados a `core/`; el 5º (`organizar_biblioteca (1).py`, Camelot/BPM) es la base del v0.2.x pendiente.
- Probado sobre la biblioteca real de Jorge: 471 tracks FLAC house/tech-house en `/Volumes/Untitled/deemix_Music`.

## Estructura

```
core/    # 7 módulos de lógica pura (config, tagger, models, analyzer, enricher,
         #   organizer, recalibrator) — SIN Click, rich ni FastAPI
cli/     # 6 comandos Click (tag, enrich, organize, recalibrate, pipeline, serve)
         #   — thin wrappers sobre core, flags bilingües (--simulate/--simular)
web/     # Fase 2: worker.py (subprocess JSONL), state.py (estado + pub/sub),
         #   runner.py, main.py (FastAPI), routes/, templates/, static/
tests/   # 225 tests (224 del refactor + 1 regresión del deadlock web)
```

Imports de essentia/mutagen son **lazy en todo el proyecto** — mantener ese patrón. Los modelos `.pb`/`.json` van en `~/essentia_models`, **nunca** al repo (`models/` solo tiene `.gitkeep`).

## ESTADO CRÍTICO ACTUAL

La **Fase 2 (web) está CÓDIGO COMPLETO pero SIN COMMITEAR**: `cli/serve.py`, `tests/test_runner.py` y todo `web/` (salvo `__init__.py`) están untracked, más `cli/main.py` modificado. **NO commitear hasta que Jorge complete la validación e2e manual** (`music-organizer serve --open` → flujo completo con simulate). `mypy core/ cli/` da **19 errores, todos en `cli/serve.py`** (código nuevo sin commitear); el árbol committed está limpio.

## Decisiones técnicas CERRADAS (no reabrir sin causa)

Evaluadas, verificadas empíricamente y aprobadas:

- **Web local, 1 usuario, 1 job a la vez.** Sin Celery/Redis/colas. Subprocess + estado en memoria. No es multi-usuario y no debe serlo.
- **Opción B de integración**: la web usa `core/` directamente vía el worker JSONL (un evento por línea, flush inmediato). NUNCA parsear el stdout del CLI (frágil).
- **Stderr del worker → tempfile, no PIPE.** Essentia/TF escupen miles de warnings; un pipe de 64KB se llena y deadlockea (`tests/test_runner.py` es la regresión que lo protege). No volver a `stderr=PIPE`.
- **`mark_cancelled()` ANTES de `terminate()`** en `cancel_pipeline` — el orden evita que el EOF marque un cancel como error falso.
- **El WebSocket SIEMPRE hace `await gen.aclose()` en finally** — sin eso queda una cola zombie de suscriptor llenándose (verificado).
- **El subprocess sobrevive la desconexión del cliente.** Cancelación solo explícita (botón). Un análisis de 90 min no muere porque se cerró la pestaña; al recargar, la UI se reconstruye desde `GET /api/state`.
- **`start_pipeline` tiene `except Exception` (no BaseException)** que traduce bugs inesperados a `mark_error` — el state nunca queda colgado en `running`. CancelledError/KeyboardInterrupt se propagan a propósito.
- **Paridad numérica cliente↔servidor**: `histogram.js` replica exactamente `energy_bucket` (comparación `<` estricta) y `statistics.quantiles` exclusive de Python (verificado con los 471 arousals reales: 5.9/6.1/6.4, incluyendo posiciones enteras). Si se toca uno, sincronizar el otro.
- **`serve` default loopback (127.0.0.1)** — la web toca el filesystem sin auth; exponerla requiere `--host 0.0.0.0` explícito con warning.
- **Paridad de CSVs con los scripts originales** — columnas, estados y nomenclatura exactos (incluyendo inconsistencias del original como `ok` en tag-simulate vs `simulado` en enrich-simulate). Los reportes deben seguir siendo diff-eables contra los históricos.
- **`pipeline` NO incluye `recalibrate`** — es operación posterior y manual del usuario.

## Método de trabajo

- **División**: el chat de diseño/revisión decide y revisa; **Claude Code ejecuta**. Cada componente se construye → se revisa en el chat → se aprueba o ajusta. Componentes aprobados no se re-revisan enteros; los cambios acotados se revisan en aislado.
- **Las desviaciones del comportamiento original las decide Jorge**, no el código. Toda desviación se registra en `CHANGELOG.md`.
- Bugs sutiles (races, deadlocks, paridad numérica) se verifican **empíricamente** con scripts de prueba, no por argumentación.
- Antes de cualquier push: revisar el staging (`git status`) para no colar CSVs personales, audio, modelos ni `reference_scripts/`.

## Comandos de desarrollo

El venv del repo está en `venv/` (Python 3.11, con pytest/ruff/mypy). Usar `./venv/bin/<tool>`.

```bash
pip install -e '.[web,dev]'        # instalación de desarrollo
./venv/bin/pytest                  # 225 tests
./venv/bin/ruff check .
./venv/bin/mypy core/ cli/

# Uso
music-organizer pipeline <musica> --models ~/essentia_models --dest <salida>
music-organizer recalibrate <musica> --auto-calibrate
music-organizer serve --open       # web en http://127.0.0.1:8000
```

## Gotcha de essentia-tensorflow

Instalar **solo** `essentia-tensorflow`, nunca el paquete `essentia` a secas a la vez: si coexisten, `essentia.tensorflow` queda shadowed y los modelos TF no cargan. Fix: `pip uninstall essentia essentia-tensorflow -y && pip install essentia-tensorflow`.

## Datos de la biblioteca real (referencia para calibración)

- 471 tracks FLAC house/tech-house. Distribución: Tech House 37.4%, House 34.8%, Techno 13.8%, Deep House 4.2%.
- Arousal: min 5.1, max 7.6, mediana 6.1. Percentiles 25/50/75 = **5.9/6.1/6.4**.
- Con umbrales default (4.8/5.8/6.6) el 70% cae en ALTA → por eso existe `recalibrate --auto-calibrate`. La recalibración de la biblioteca real aún no se ha ejecutado (pendiente tras la validación web).
