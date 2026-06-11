# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Interfaz web local** (`music-organizer serve --open`): el pipeline
  completo con progreso en vivo por WebSocket (fase, archivo en curso,
  histograma de energía en tiempo real — el análisis sobrevive si se
  cierra la pestaña), setup con validación de rutas en vivo y explorador
  de carpetas integrado, y pantalla de resultados con histograma
  interactivo de arousal y sliders de umbrales (paridad numérica exacta
  con `recalibrate --auto-calibrate`, verificada sobre los 471 arousals
  reales). Loopback por defecto; exponer requiere `--host 0.0.0.0`
  explícito. Desde la web, `organize` siempre usa una carpeta por género
  (la energía queda en el tag) y sin subcarpeta `Lossless/`; el CLI
  conserva ambos flags.
- **Resolver de tracklists** (`music-organizer resolve` + panel web al
  lado del análisis): resuelve tracklists pegados o desde URL
  (descripción Y comentarios de YouTube, descripción de Mixcloud) contra
  Deezer → iTunes → YouTube con matching difuso calibrado contra las APIs
  reales; reporte CSV (`_REPORTE_TRACKLIST.csv`); tracker persistente de
  unreleased (`~/.music-organizer/unreleased.json`) con
  `resolve --recheck`. Sin costos: Deezer/iTunes/Mixcloud no requieren
  credenciales; la key de YouTube es opcional y gratuita
  (`YOUTUBE_API_KEY`). SoundCloud/Beatport quedan fuera (APIs cerradas) y
  1001Tracklists no se scrapea (ToS).
- Nueva dependencia: `rapidfuzz` (matching difuso del resolver).

### Fixed

- `organize`: el contador `sin tag / nivel` del resumen ya no sobre-cuenta
  archivos que fallaron en la operación de filesystem. En el script original,
  un archivo con targeting "sin tag" que luego fallaba al copiar/mover se
  contaba en ambos contadores (`sin_tag` y `errores`). Ahora cada archivo
  cuenta en exactamente un contador. Solo afecta los números del resumen en
  pantalla; las filas del CSV no cambian.

### Changed

- `organize`: la distribución del resumen ahora usa `pathlib.Path.parts` en
  vez de split por `/`, para que funcione en Windows. También descarta el
  componente `Lossless/` del primer nivel para que el resumen sea consistente
  con y sin `--no-lossless`.
- `organize`: el parseo del sufijo de nivel ahora es case-insensitive y
  normaliza a mayúsculas (`/ alta` → carpeta `ALTA/`). El organizador
  original era case-sensitive y mandaba variantes no-canónicas a
  `_SIN_NIVEL/`. Este cambio consolida el comportamiento con el de `enrich`,
  que ya era case-insensitive. No afecta bibliotecas con tags canónicos
  (los que escribe el propio pipeline siempre son mayúsculas).
- `enrich`: un archivo ilegible/corrupto ahora se reporta como `skip:
  sin tag de genero` en vez del `error: lectura: <detalle>` del script
  original. Consecuencia de que `core.tagger.read_tags` es defensiva (nunca
  lanza). El outcome práctico es idéntico (no se escribe, queda registrado
  en el CSV); solo cambia el texto del motivo. Caso raro: en el flujo normal
  `tag` ya habría fallado sobre un archivo corrupto antes de llegar a `enrich`.
- `organize`: un archivo con tags ilegibles pero bytes copiables (ej. MP3
  sin header ID3) ahora se copia/mueve a `_SIN_TAG/` en vez de quedar
  marcado como `error: lectura: <detalle>` y saltado en el origen. Misma
  raíz que la divergencia de `enrich` (`read_tags` defensiva). Diferencia
  más visible que en `enrich`: en `--move` el archivo realmente se moverá
  a `_SIN_TAG/` en vez de quedarse en el origen. Mitigación: el archivo
  termina en `_SIN_TAG/` con motivo `sin tag genre` en el CSV, el usuario
  puede revisarlo ahí. Caso raro: requiere que el archivo no haya pasado
  exitosamente por `tag` antes.
