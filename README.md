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

Probado sobre 657 pistas FLAC de una biblioteca de house / tech-house:
- Confianza de género: 0.60–0.77.
- Energía: distribución real (no satura como otros modelos en música de club).
- Tiempo: ~60–90 minutos en un MacBook Air M4.

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

**Interfaz web local:**
```bash
music-organizer serve
# abre http://localhost:8000
```

### Documentación

- [Guía de instalación detallada](docs/INSTALL.md)
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

### Real-world results

Tested on 657 FLAC tracks from a house / tech-house library:
- Genre confidence: 0.60–0.77.
- Energy: real distribution (doesn't saturate like other models do on club music).
- Time: ~60–90 minutes on a MacBook Air M4.

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

**Local web interface:**
```bash
music-organizer serve
# open http://localhost:8000
```

---

## Built by [RED111](https://red111.dev)

Open source project from RED111, makers of modular admin SaaS for LATAM businesses.
Created and maintained by [@red111](https://github.com/red111).
