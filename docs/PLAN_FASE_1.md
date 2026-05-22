# Plan de refactor — Fase 1 (v0.1.0)

Guía para Claude Code sobre cómo migrar los 4 scripts actuales a una
estructura modular bajo `core/` + `cli/`.

## Estado actual

En la carpeta del proyecto local (`/Users/jorge/code/jorgegonzalez10/proyectos/dj-organize/`)
hay 4 scripts independientes:

1. **`etiquetar_genero.py`** — análisis con Essentia (MAEST + emoMusic/VGGish).
   YA CORRIDO sobre las 657 pistas reales.
2. **`enriquecer_genero.py`** — reescribe tag `genre` como `"<Genero> / <NIVEL>"`.
3. **`organizar_genero_energia.py`** — copia archivos a `Lossless/<Genero>/<Nivel>/`.
4. **`organizar_biblioteca (1).py`** — organizador viejo por Camelot/BPM
   (mantener como referencia, va en v0.2.0 NO en v0.1.0).

El flujo principal son los **pasos 1 → 2 → 3**. Cada uno es:
- Reversible (paso 2 tiene `--revertir`; paso 3 no toca el origen al copiar).
- Idempotente (re-correr no rompe nada).
- Genera CSV de reporte.

## Lo que se conserva en el refactor

Estas decisiones de diseño del código actual son correctas y NO deben cambiar:

- Tag `genre` como fuente de verdad estructurada (`"Tech House / ALTA"`).
- Tag `comment` con energía detallada + top-3 de género con porcentajes.
- Defaults: COPIA (no mover), NO renombrar archivos.
- Filtro de sidecars macOS (`._*`) y subcarpetas con prefijo `_`.
- 4 niveles de energía: BAJA / MEDIA / ALTA / MUY ALTA.
- Umbrales actuales de `energy_bucket` (4.8 / 5.8 / 6.6).
- `GENRE_SIMPLIFY` tal como está.
- Reportes CSV en la carpeta de origen (no en el repo).

## Estructura objetivo

```
music-organizer/
├── core/
│   ├── __init__.py
│   ├── config.py        ← constantes compartidas (ver abajo)
│   ├── tagger.py        ← lectura/escritura unificada de tags
│   ├── models.py        ← carga de modelos Essentia
│   ├── analyzer.py      ← paso 1: análisis MAEST + emoMusic
│   ├── enricher.py      ← paso 2: combinar genre + energy
│   ├── organizer.py     ← paso 3: organizar por carpetas
│   └── recalibrator.py  ← módulo 7: recalcular energía sin re-analizar
├── cli/
│   ├── __init__.py
│   ├── main.py          ← entry point con Click
│   ├── tag.py           ← comando 'tag'
│   ├── enrich.py        ← comando 'enrich'
│   ├── organize.py      ← comando 'organize'
│   ├── pipeline.py      ← comando 'pipeline' (corre los 3 en orden)
│   └── recalibrate.py   ← comando 'recalibrate' (no va en pipeline)
├── web/                 ← se llena en Fase 2
├── tests/
└── (archivos ya creados: README.md, LICENSE, pyproject.toml, etc.)
```

## Detalle por módulo

### `core/config.py`

Consolidar de los 3 scripts (cada uno tiene su copia):

```python
AUDIO_EXTS = {'.mp3', '.flac', '.wav', '.aiff', '.aif', '.m4a', '.aac', '.ogg'}

NIVELES = ('BAJA', 'MEDIA', 'ALTA', 'MUY ALTA')

# Regex compartidos
RE_ENERGIA = re.compile(r'^Energia:\s*(' + '|'.join(NIVELES) + r')\b')
RE_SUFIJO = re.compile(r'\s*/\s*(' + '|'.join(NIVELES) + r')\s*$', re.IGNORECASE)

# Umbrales (de etiquetar_genero.py:energy_bucket)
UMBRAL_BAJA = 4.8
UMBRAL_MEDIA = 5.8
UMBRAL_ALTA = 6.6

# Mapeo de Discogs a etiquetas DJ (de etiquetar_genero.py)
GENRE_SIMPLIFY = { ... }  # copiar tal cual

# Nombres de archivo de los modelos Essentia
EMB_MAEST = 'discogs-maest-30s-pw-2.pb'
GENRE_HEAD = 'genre_discogs400-discogs-maest-30s-pw-1.pb'
GENRE_LABELS = 'genre_discogs400-discogs-maest-30s-pw-1.json'
EMB_VGGISH = 'audioset-vggish-3.pb'
ENERGY_HEAD = 'emomusic-audioset-vggish-2.pb'
```

### `core/tagger.py`

Consolidar las 3 versiones actuales de `leer_genre*` y `escribir_*` en
funciones unificadas. Estructura sugerida:

```python
@dataclass
class TrackTags:
    genre: str = ''
    comment: str = ''

def read_tags(path: Path) -> TrackTags:
    """Lee genre + comment de FLAC, MP3, AIFF, WAV, M4A, AAC, OGG."""
    
def write_genre(path: Path, genre: str) -> tuple[bool, str]:
    """Escribe solo el tag genre. Devuelve (ok, motivo)."""

def write_genre_and_comment(path: Path, genre: str, comment: str) -> tuple[bool, str]:
    """Escribe ambos tags (usado por el paso 1)."""

def safe_dirname(s: str) -> str:
    """Sanitiza string para nombre de carpeta (de organizar_genero_energia.py)."""
```

**Importante:** preservar el manejo especial de WAV con ID3 directo
(no usar `MutagenFile()`, falla con "can't sync to MPEG frame").

### `core/models.py`

De `etiquetar_genero.py:main()`, separar la carga de modelos:

```python
@dataclass
class EssentiaModels:
    loader: MonoLoader
    maest: TensorflowPredictMAEST
    genre_head: TensorflowPredict
    vggish: TensorflowPredictVGGish
    energy_head: TensorflowPredict2D
    genre_labels: list[str]

def validate_models_dir(models_dir: Path) -> list[str]:
    """Devuelve lista de archivos que faltan."""

def load_models(models_dir: Path) -> EssentiaModels:
    """Carga los 5 modelos. Toma ~10-15 segundos."""
```

### `core/analyzer.py` (paso 1)

```python
@dataclass
class TrackAnalysis:
    genre: str               # simplificado: "Tech House"
    genre_raw: str           # crudo: "Electronic---Tech House"
    genre_top3_text: str     # "Tech House 63% | House 43% | ..."
    genre_confidence: float
    arousal: float
    energy_level: str        # 'BAJA' | 'MEDIA' | 'ALTA' | 'MUY ALTA'
    energy_value: float      # arousal redondeado a 1 decimal

def analyze_track(path: Path, models: EssentiaModels) -> TrackAnalysis:
    """Corre MAEST + emoMusic sobre un archivo."""

def simplify_genre(label: str) -> str:
    """De 'Electronic---Tech House' a 'Tech House'."""

def energy_bucket(arousal: float) -> tuple[str, float]:
    """De arousal float a (nivel, valor)."""
```

### `core/enricher.py` (paso 2)

```python
def compute_enriched_genre(
    current_genre: str,
    current_comment: str,
    revert: bool = False
) -> tuple[str, str]:
    """Devuelve (nuevo_genre, motivo).
    Motivos: 'enriquecer', 'revertir', 'sin-cambio', 'skip: ...'.
    Idempotente."""
```

### `core/organizer.py` (paso 3)

```python
@dataclass
class OrganizeResult:
    source: Path
    target: Path
    status: str  # 'ok' | 'skip' | 'error'
    motivo: str

def parse_enriched_genre(genre_tag: str) -> tuple[str, str | None]:
    """De 'Tech House / ALTA' a ('Tech House', 'ALTA').
    Si no hay sufijo, devuelve (genre, None)."""

def compute_target(
    source: Path,
    genre_tag: str,
    dest_base: Path,
    flat: bool = False
) -> tuple[Path, str]:
    """Calcula ruta de destino y motivo."""

def organize_file(source: Path, target: Path, move: bool = False, dry_run: bool = False) -> OrganizeResult:
    """Ejecuta copy/move con manejo de errores."""
```

### `cli/main.py`

```python
import click
from cli.tag import tag_command
from cli.enrich import enrich_command
from cli.organize import organize_command
from cli.pipeline import pipeline_command

@click.group()
@click.version_option("0.1.0")
def cli():
    """Red111 Music Organizer — Smart library tools for DJs."""
    pass

cli.add_command(tag_command, name="tag")
cli.add_command(enrich_command, name="enrich")
cli.add_command(organize_command, name="organize")
cli.add_command(pipeline_command, name="pipeline")

@cli.command()
def serve():
    """Start the local web interface."""
    # se implementa en Fase 2
    ...
```

### `cli/pipeline.py` (comando nuevo)

Conveniencia para usuarios que quieren todo el flujo en un solo comando:

```python
@click.command()
@click.argument('source', type=click.Path(exists=True))
@click.option('--models', required=True, type=click.Path(exists=True))
@click.option('--dest', required=True, type=click.Path())
@click.option('--dry-run', is_flag=True)
def pipeline_command(source, models, dest, dry_run):
    """Run the full 3-step pipeline: tag → enrich → organize."""
    # 1. Analizar y escribir tags
    # 2. Enriquecer genre con energy
    # 3. Organizar a estructura final
```

## Criterios de aceptación de Fase 1

- [ ] `pip install -e ".[web,dev]"` instala sin errores.
- [ ] `music-organizer --help` muestra: tag, enrich, organize, pipeline, serve.
- [ ] `music-organizer tag --simulate --limit 5 <musica> --models <ruta>`
      produce un CSV idéntico al del script original sobre las mismas 5 pistas.
- [ ] `music-organizer enrich --simulate --limit 5 <musica>`
      idem para el paso 2.
- [ ] `music-organizer organize --simulate --source X --dest Y`
      idem para el paso 3.
- [ ] Tests básicos pasan:
      - `test_config.py`: GENRE_SIMPLIFY y umbrales bien definidos.
      - `test_tagger.py`: lectura/escritura sobre FLAC fake.
      - `test_enricher.py`: idempotencia, reversión, casos edge.
      - `test_organizer.py`: parseo del sufijo, cálculo de target paths.

## Lo que NO se hace en Fase 1

- NO implementar `web/` (Fase 2).
- NO migrar el organizador viejo de Camelot/BPM (v0.2.0).
- NO cambiar la lógica de Essentia ni los modelos.
- NO cambiar el formato de los CSV (mantener compatibilidad).
- NO añadir features nuevas.

## Validación final antes del commit

```bash
# Limpiar y reinstalar
deactivate 2>/dev/null || true
rm -rf venv
python3 -m venv venv
source venv/bin/activate
pip install -e ".[web,dev]"
./scripts/download_models.sh

# Correr los 3 pasos en modo simulado sobre 5 archivos de prueba
music-organizer tag --simulate --limit 5 \
    "/Volumes/Untitled/deemix_Music" --models ~/essentia_models
music-organizer enrich --simulate --limit 5 "/Volumes/Untitled/deemix_Music"
music-organizer organize --simulate --limit 5 \
    --source "/Volumes/Untitled/deemix_Music" \
    --dest "/tmp/test_organizada"

# Comparar resultados con los CSVs originales que ya tienes.
# Deben ser idénticos.

# Pipeline completo
music-organizer pipeline --dry-run \
    "/Volumes/Untitled/deemix_Music" \
    --models ~/essentia_models \
    --dest "/tmp/test_pipeline"

# Tests
pytest

# Si todo pasa: commit, tag v0.0.1-refactor, push
```

## Nota sobre nomenclatura

Los scripts originales mezclan español e inglés. En el refactor:

- **Nombres de comandos CLI**: en inglés (`tag`, `enrich`, `organize`).
- **Flags**: aceptar tanto inglés como español como aliases (`--simulate` y `--simular`).
- **Mensajes al usuario**: inglés por defecto, con flag `--lang es` para español.
- **Código (funciones, clases, variables)**: en inglés. Los comentarios y
  docstrings pueden quedar bilingües donde aclare.
- **CSVs de reporte**: mantener nombres originales (`_REPORTE_GENERO.csv`,
  `_REPORTE_ENRIQUECER.csv`, `_REPORTE_ORGANIZACION.csv`,
  `_REPORTE_RECALCULO.csv`) para no romper flujos existentes del usuario.

---

## Módulo 7: `core/recalibrator.py`

Reajusta el nivel de energía sin re-analizar el audio. Lee el arousal numérico
ya escrito en el `comment` por `analyzer` (formato
`"Energia: NIVEL (X.X/9) | Genero: ..."`), aplica umbrales nuevos y devuelve el
cambio a aplicar al `genre` (si tiene sufijo `/ NIVEL`) y al `comment`.

Sirve para recalibrar después de la primera corrida: cuando el usuario ve que
la distribución real de su biblioteca no calza con los umbrales por defecto.
Toma <1 min sobre cientos de pistas — vs. ~60–90 min de re-análisis con
Essentia, porque el arousal ya está escrito. Soporta auto-calibración:
percentiles 25/50/75 de los arousals leídos.

```python
@dataclass
class RecalibrationResult:
    arousal: float | None          # leído del comment, None si no parseó
    old_level: EnergyLevel | None
    new_level: EnergyLevel | None
    old_genre: str
    new_genre: str                 # sufijo " / NIVEL" actualizado si tenía
    new_comment: str               # mismo formato, nivel actualizado
    motivo: str                    # 'recalcular' | 'sin-cambio' | 'skip: ...'

def parse_arousal_comment(comment: str) -> tuple[EnergyLevel, float, str] | None:
    """De 'Energia: ALTA (6.0/9) | Genero: ...' a (nivel, arousal, resto).
    Devuelve None si el comment no tiene el formato esperado."""

def energy_bucket_custom(
    arousal: float,
    thresholds: tuple[float, float, float],
) -> EnergyLevel:
    """Aplica umbrales custom (B, M, A). Equivalente a analyzer.energy_bucket
    pero parametrizable. No redondea el valor, solo devuelve el nivel."""

def auto_calibrate(arousals: list[float]) -> tuple[float, float, float]:
    """Percentiles 25/50/75 redondeados a 2 decimales."""

def compute_recalibration(
    genre: str,
    comment: str,
    thresholds: tuple[float, float, float],
) -> RecalibrationResult:
    """Idempotente: re-correr con los mismos umbrales devuelve 'sin-cambio'."""
```

**Importante**:
- El `comment` se reescribe **siempre** cuando cambia el nivel (formato
  normalizado, arousal intacto, resto intacto).
- El `genre` se reescribe **solo si** ya tenía sufijo `/ NIVEL` previo.
  No enriquece pistas que estaban sin enriquecer — eso es trabajo de
  `enricher`, no de `recalibrator`.

## Módulo 8: comando `recalibrate` en CLI

`cli/recalibrate.py` — equivalente del `recalcular_energia.py` original.

```python
@click.command()
@click.argument('source', type=click.Path(exists=True))
@click.option('--thresholds', '--umbrales', 'thresholds',
              help='Tres umbrales B,M,A separados por coma (ej. 5.90,6.10,6.40)')
@click.option('--auto-calibrate', '--auto-calibrar', 'auto_calibrate',
              is_flag=True,
              help='Calcula umbrales como percentiles 25/50/75')
@click.option('--simulate', '--simular', 'simulate', is_flag=True)
@click.option('--limit', '--limite', 'limit', type=int, default=0)
def recalibrate_command(source, thresholds, auto_calibrate, simulate, limit):
    """Recalculate energy levels without re-analyzing audio."""
    # 1. Validar: exactamente uno de --thresholds o --auto-calibrate
    # 2. Leer (genre, comment) de todos los archivos del source -> arousals
    # 3. Si --auto-calibrate: thresholds = recalibrator.auto_calibrate(arousals)
    # 4. Por archivo: compute_recalibration -> aplicar si not simulate
    # 5. Escribir _REPORTE_RECALCULO.csv
    # 6. Resumen: transiciones de nivel + nueva distribución (barra ASCII)
```

**No se incluye en `pipeline`**. `recalibrate` es una operación **posterior**:
el usuario primero corre `pipeline` (tag → enrich → organize), revisa la
distribución real, y luego decide si quiere recalibrar. Encadenarlo dentro
de `pipeline` cambiaría los niveles que el usuario acaba de ver y aceptar.

Tests adicionales (se suman a los del bloque "Criterios de aceptación"):
- `test_recalibrator.py`: parseo del comment, idempotencia, auto-calibración
  con arousals sintéticos, transiciones de nivel correctas.
