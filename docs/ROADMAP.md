# Roadmap — Red111 Music Organizer

> Roadmap de producto para colaboradores. Última actualización: junio 2026.

## Visión

**Smart library tools for DJs.** Herramienta open source (MIT) para analizar,
etiquetar y organizar bibliotecas musicales de DJ usando deep learning con
[Essentia](https://essentia.upf.edu/):

- **Género**: MAEST + cabeza Discogs-400 (400 etiquetas de género/estilo).
- **Energía**: emoMusic (arousal) sobre embeddings VGGish, escala 1–9.

El proyecto nace del refactor de 4 scripts personales del autor (repo
`dj-organize` de jorgegonzalez10), convertidos en un paquete modular con
tests, CLI y web local.

## Estado actual

### v0.1.0 — Pipeline CLI (publicado)

Paquete modular `core/` + `cli/` con 5 operaciones: `tag`, `enrich`,
`organize`, `recalibrate` y `pipeline` (tag→enrich→organize encadenados).
225 tests (224 del refactor publicado + 1 regresión del runner web de la
Fase 2); paridad end-to-end verificada contra los scripts originales.

### Fase 2 — Interfaz web local (código completo, pendiente validación e2e)

Servida con `music-organizer serve` (loopback por defecto). Diseño: 1 usuario,
1 job a la vez, sin colas externas. Los 10 componentes ya construidos y
revisados:

| Componente | Qué hace |
|---|---|
| `web/worker.py` | Subprocess que corre el pipeline emitiendo eventos JSONL; reutiliza `core/` íntegro |
| `web/state.py` | Estado del job en memoria + pub/sub para WebSockets, con buffer de replay |
| `web/runner.py` | Orquesta el subprocess (stderr a tempfile, cancelación SIGTERM→SIGKILL) |
| `web/main.py` | Factory FastAPI (lifespan, static, Jinja2) |
| `web/routes/api.py` | `POST /api/start`, `POST /api/cancel`, `GET /api/state`, `WS /api/ws` |
| `web/routes/pages.py` | Shells HTML + design system (tema oscuro, acento RED111) |
| `index.html` | Pantalla de setup: paths y opciones del pipeline |
| `progress.html` | Progreso en vivo por WebSocket, con reconexión y resync |
| `results.html` + `histogram.js` | Histograma SVG interactivo con sliders de umbrales y auto-calibrado (paridad numérica exacta con Python) |
| `cli/serve.py` | Comando `serve` que levanta uvicorn (`--open`, `--reload`) |

Falta: validación e2e manual (instalar `.[web]`, flujo completo en modo
simulación y luego con biblioteca real), commit/push de la fase y posible tag.

## Roadmap por fases

| Fase | Contenido | Estado |
|---|---|---|
| v0.1.0 | Pipeline CLI (tag/enrich/organize/recalibrate/pipeline) | ✅ Publicado |
| Fase 2 | Interfaz web local | 🔵 Código completo → validación e2e → release |
| v0.2.x | Organizador clásico Camelot/BPM + modo `--plano` (migración del script original `organizar_biblioteca`) | ⬜ Pendiente |
| v0.3.x | Resolver de tracklists con IA + tracking de unreleased | ⬜ Diseñado |
| Web v2 | Mejoras post-validación (backlog abajo) | ⬜ Backlog |

### v0.3.x — Resolver de tracklists (decisiones de diseño ya tomadas)

- Acepta texto pegado y URLs de **fuentes legales** (APIs oficiales de
  SoundCloud/YouTube/Mixcloud). **No** se scrapea 1001Tracklists (viola ToS).
- Prioriza **Deezer** para resolución de tracks; luego Beatport/Bandcamp.
- IA **solo** para limpiar/normalizar el tracklist, con BYOK (el usuario
  aporta su propia API key; coste estimado ~$0.001 por lista).
- Tracking de tracks **unreleased** en archivo persistente para reconsultas.
- Nace como CLI; eventualmente integrable como 4ª pantalla de la web.

### Backlog web v2 (sin orden fijo)

- Conectar el botón **"Aplicar umbrales"** de results.html a un endpoint que
  ejecute `recalibrate` con los valores de los sliders.
- Exponer `--limit` en el formulario de setup (pruebas rápidas desde la web).
- Sección de **recalibrate standalone** (sin re-correr el pipeline completo).
- Tests del flujo web: añadir tests de `api.py` con TestClient.

## Datos de referencia (biblioteca real del autor)

El pipeline está probado sobre una biblioteca real de **471 tracks FLAC**
house/tech-house:

| Métrica | Valor |
|---|---|
| Distribución de géneros | Tech House 37.4% · House 34.8% · Techno 13.8% · Deep House 4.2% |
| Arousal (energía) | min 5.1 · max 7.6 · mediana 6.1 |
| Percentiles 25/50/75 | 5.9 / 6.1 / 6.4 |

Con los umbrales por defecto (4.8/5.8/6.6), el ~70% de la biblioteca caía en
el bucket ALTA: en una colección homogénea de un solo género el arousal se
concentra en un rango estrecho. Por eso existe `recalibrate --auto-calibrate`,
que recalcula los umbrales a partir de los percentiles de la propia biblioteca.
