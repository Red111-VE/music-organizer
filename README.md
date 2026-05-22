# Red111 Music Organizer

**Smart library tools for DJs** — Automatic genre and energy tagging for your music library, powered by audio analysis with deep learning models.

*Etiquetado automático de género y energía para tu biblioteca de música, con análisis de audio basado en deep learning.*

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![Status: Alpha](https://img.shields.io/badge/status-alpha-orange.svg)]()

---

## 🇪🇸 Español

### ¿Qué hace?

Pipeline de 3 pasos para preparar una biblioteca de DJ:

1. **Analiza el audio** y detecta:
   - **Género** (Tech House, Deep House, Minimal, Techno, etc.) usando MAEST con 400 etiquetas de Discogs.
   - **Energía** en escala 1-9 (BAJA, MEDIA, ALTA, MUY ALTA) usando emoMusic.
2. **Enriquece** el tag de género combinándolo con la energía (`"Tech House / ALTA"`).
3. **Organiza** los archivos en carpetas por género y nivel de energía.

No renombra archivos. No toca BPM ni tonalidad (esos vienen de Rekordbox). Cada paso es reversible e idempotente.

### Caso de uso real

Probado sobre una biblioteca real de **471 tracks** FLAC de house / tech-house.

#### Géneros detectados

```
Tech House           ████████████████████  37.4%  (176)
House                ██████████████████     34.8%  (164)
Techno               ███████                13.8%  (65)
Deep House           ██                      4.2%  (20)
Minimal / Deep Tech  █                       2.3%  (11)
Electro                                      1.7%  (8)
Progressive House                            1.3%  (6)
Speed Garage                                 1.3%  (6)
```

#### Por qué `recalibrate` importa

El modelo de energía (emoMusic) devuelve valores de *arousal* en escala 1–9.
Pero en una biblioteca de música de club, esos valores se concentran en un
rango estrecho — no se reparten por toda la escala. Con los umbrales por
defecto, casi todo cae en "ALTA" y la etiqueta deja de ser útil para mezclar:

```
Umbrales por defecto (4.8 / 5.8 / 6.6):

  BAJA       0   0.0%
  MEDIA     73  15.5%   ███████
  ALTA     331  70.3%   ███████████████████████████████████  ← todo aquí
  MUY ALTA  67  14.2%   ███████
```

`recalibrate --auto-calibrate` calcula los umbrales como los percentiles
25/50/75 de *tu* biblioteca (aquí: 5.9 / 6.1 / 6.4), produciendo una
distribución equilibrada donde cada nivel significa algo distinto:

```
Umbrales auto-calibrados (5.9 / 6.1 / 6.4):

  BAJA     101  21.4%   ██████████
  MEDIA     96  20.4%   ██████████
  ALTA     151  32.1%   ████████████████
  MUY ALTA 123  26.1%   █████████████
```

Sin re-analizar el audio (que toma ~60–90 min): la recalibración lee el
*arousal* ya guardado en cada archivo y reescribe solo el nivel, en segundos.

### Instalación rápida

```bash
git clone https://github.com/Red111-VE/music-organizer.git
cd music-organizer
python3 -m venv venv
source venv/bin/activate
pip install -e .
./scripts/download_models.sh
```

### Uso — pipeline de 3 pasos

**1. Analizar** (escribe género + energía dentro de cada archivo):
```bash
music-organizer tag "/ruta/a/musica" --models ~/essentia_models
```

**2. Enriquecer** (combina género + energía en un solo tag estructurado):
```bash
music-organizer enrich "/ruta/a/musica"
```

**3. Organizar** (copia archivos a carpetas por género/energía):
```bash
music-organizer organize --source "/ruta/a/musica" --dest "/destino"
```

**Todo en uno:**
```bash
music-organizer pipeline "/ruta/a/musica" \
    --models ~/essentia_models --dest "/destino"
```

**Recalibrar la energía** (ajusta los niveles a tu biblioteca, sin re-analizar):
```bash
music-organizer recalibrate "/ruta/a/musica" --auto-calibrate
```

### Documentación

- [Licencias de los modelos](docs/MODELS.md)
- [Cómo contribuir](CONTRIBUTING.md)

---

## 🇬🇧 English

### What it does

A 3-step pipeline to prepare a DJ library:

1. **Analyzes audio** and detects:
   - **Genre** (Tech House, Deep House, Minimal, Techno, etc.) using MAEST with 400 Discogs labels.
   - **Energy** on a 1-9 scale (LOW, MEDIUM, HIGH, VERY HIGH) using emoMusic.
2. **Enriches** the genre tag by combining it with energy (`"Tech House / HIGH"`).
3. **Organizes** files into folders by genre and energy level.

Doesn't rename files. Doesn't touch BPM or key (those come from Rekordbox). Each step is reversible and idempotent.

### Real-world example

Tested on a real library of **471 house / tech-house FLAC tracks**.

#### Detected genres

```
Tech House           ████████████████████  37.4%  (176)
House                ██████████████████     34.8%  (164)
Techno               ███████                13.8%  (65)
Deep House           ██                      4.2%  (20)
Minimal / Deep Tech  █                       2.3%  (11)
Electro                                      1.7%  (8)
Progressive House                            1.3%  (6)
Speed Garage                                 1.3%  (6)
```

#### Why `recalibrate` matters

The energy model (emoMusic) returns *arousal* values on a 1–9 scale. But in a
club-music library, those values cluster in a narrow range rather than
spreading across the scale. With the default thresholds, almost everything
lands in "HIGH" and the label stops being useful for mixing:

```
Default thresholds (4.8 / 5.8 / 6.6):

  LOW        0   0.0%
  MEDIUM    73  15.5%   ███████
  HIGH     331  70.3%   ███████████████████████████████████  ← everything here
  VERY HIGH 67  14.2%   ███████
```

`recalibrate --auto-calibrate` computes thresholds as the 25/50/75 percentiles
of *your* library (here: 5.9 / 6.1 / 6.4), producing a balanced distribution
where each level means something distinct:

```
Auto-calibrated thresholds (5.9 / 6.1 / 6.4):

  LOW      101  21.4%   ██████████
  MEDIUM    96  20.4%   ██████████
  HIGH     151  32.1%   ████████████████
  VERY HIGH 123  26.1%  █████████████
```

No audio re-analysis required (that takes ~60–90 min): recalibration reads the
*arousal* already stored in each file and rewrites just the level, in seconds.

### Quick install

```bash
git clone https://github.com/Red111-VE/music-organizer.git
cd music-organizer
python3 -m venv venv
source venv/bin/activate
pip install -e .
./scripts/download_models.sh
```

### Usage — 3-step pipeline

**1. Analyze** (writes genre + energy tags into each file):
```bash
music-organizer tag "/path/to/music" --models ~/essentia_models
```

**2. Enrich** (combines genre + energy into a single structured tag):
```bash
music-organizer enrich "/path/to/music"
```

**3. Organize** (copies files into genre/energy folder structure):
```bash
music-organizer organize --source "/path/to/music" --dest "/output"
```

**All in one:**
```bash
music-organizer pipeline "/path/to/music" \
    --models ~/essentia_models --dest "/output"
```

**Recalibrate energy** (tunes levels to your library, without re-analyzing):
```bash
music-organizer recalibrate "/path/to/music" --auto-calibrate
```

### Documentation

- [Model licenses](docs/MODELS.md)
- [How to contribute](CONTRIBUTING.md)

---

## Built by [RED111](https://red111.dev)

Open source project from RED111, makers of modular admin SaaS for LATAM businesses.
Created and maintained by [@red111](https://github.com/red111).
