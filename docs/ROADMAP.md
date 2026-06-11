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
tests, CLI, web local y un resolver de tracklists.

## Estado actual

### v0.1.0 — Pipeline CLI (publicado)

Paquete modular `core/` + `cli/` con 5 operaciones: `tag`, `enrich`,
`organize`, `recalibrate` y `pipeline` (tag→enrich→organize encadenados).
Paridad end-to-end verificada contra los scripts originales.

### Fase 2 — Interfaz web local (publicada, `e6baae3`)

Servida con `music-organizer serve` (loopback por defecto). Diseño: 1 usuario,
1 job a la vez, sin colas externas. Subprocess worker con eventos JSONL,
estado en memoria con pub/sub para WebSockets, progreso en vivo con
histograma de energía creciendo en tiempo real, y pantalla de resultados con
sliders de umbrales (paridad numérica exacta con
`recalibrate --auto-calibrate`). Validada e2e sobre la biblioteca real.

Mejoras post-validación ya incorporadas: validación de rutas en vivo,
explorador de carpetas integrado, guard de origen↔destino anidados, y
`organize` desde la web siempre con una carpeta por género sin `Lossless/`
(decisión de producto; el CLI conserva ambos flags).

### v0.3.0 — Resolver de tracklists (publicado)

CLI (`music-organizer resolve`) + panel web (embebido en la principal al
lado del análisis, y vista dedicada en `/resolver`):

| Componente | Qué hace |
|---|---|
| `core/tracklist.py` | Parser de tracklists pegados (numeración, timestamps, sellos, remixes, IDs, filtro de ruido social) — también actúa de *detector* |
| `core/sources.py` | URL → texto: YouTube (descripción + comentarios, key gratuita) y Mixcloud (API abierta) |
| `core/resolver.py` | Cascada Deezer → iTunes → YouTube con matching difuso (rapidfuzz) calibrado contra las APIs reales; throttle por proveedor |
| `core/unreleased.py` | Tracker persistente de unreleased con recheck |
| `cli/resolve.py` | Comando: archivo/URL/stdin/texto, tabla, CSV, `--recheck` |
| `web/routes/resolver.py` | Endpoints finos: parse, track-por-track (progreso en vivo sin WS), tracker |

Decisiones de diseño cumplidas: solo fuentes legales (sin scraping de
1001Tracklists; SoundCloud/Beatport fuera por APIs cerradas), costo cero
(sin LLM en v1 — parser determinístico; key de YouTube opcional y
gratuita), expectativas explícitas (los IDs/unreleased no existen en
catálogos — el resumen lo dice siempre).

Publicado como **v0.3.0** ("Local web UI + tracklist resolver") junto con la
Fase 2 web, en un solo tag. No hubo v0.2.x — ese número correspondía al
Camelot/BPM descartado.

### Descartado

- **Organizador Camelot/BPM** (el viejo "v0.2.x", migración del 5º script
  original): rekordbox ya cubre clave y BPM — no aporta valor. Decisión de
  Jorge, junio 2026.

## Backlog (sin orden fijo)

- Conectar el botón **"Aplicar umbrales"** de results.html a un endpoint que
  ejecute `recalibrate` con los valores de los sliders.
- Exponer `--limit` en el formulario de setup (pruebas rápidas desde la web).
- Sección de **recalibrate standalone** en la web (sin re-correr el pipeline).
- Normalización LLM opcional (BYOK) como pre-procesador del parser de
  tracklists, para los formatos que el regex no cubre.
- Cruce del tracklist resuelto contra la biblioteca local ("ya tenés 7 de
  estos 23 tracks") — `collect_audio_files` + `read_tags` ya existen.
- Export del tracklist resuelto a playlist (M3U / rekordbox XML).

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
La recalibración de la biblioteca real del autor sigue pendiente de ejecutarse.
