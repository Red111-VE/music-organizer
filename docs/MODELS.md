# Model Licenses / Licencias de los modelos

This project uses pre-trained deep learning models from [Essentia](https://essentia.upf.edu/),
developed by the [Music Technology Group](https://www.upf.edu/web/mtg) at Universitat Pompeu Fabra.

**The models are NOT included in this repository.** They must be downloaded separately
from the official source by running `./scripts/download_models.sh`.

---

## Required models

### Genre detection (MAEST)

- `discogs-maest-30s-pw-2.pb` — audio embeddings (~348 MB)
- `genre_discogs400-discogs-maest-30s-pw-1.pb` — genre classifier
- `genre_discogs400-discogs-maest-30s-pw-1.json` — label list (400 Discogs genres)

**License:** CC BY-NC-SA 4.0 (Creative Commons Attribution-NonCommercial-ShareAlike)

### Energy detection (emoMusic)

- `audioset-vggish-3.pb` — VGGish audio embeddings
- `emomusic-audioset-vggish-2.pb` — arousal/valence classifier

**License:** CC BY-NC-SA 4.0

---

## What this means in practice

- ✅ **Personal use, research, learning** — fully allowed.
- ✅ **Open source projects** — allowed, with attribution.
- ❌ **Commercial use** — NOT allowed by the model licenses without explicit permission from UPF.

This means that while this **code** is MIT licensed (you can do anything with it),
the **models** it relies on have non-commercial restrictions. If you want to use
this tool in a commercial product, you need to either:

1. Contact UPF / MTG to negotiate a commercial license for the models, or
2. Replace the models with alternatives that have compatible commercial licenses.

---

## Why we chose these models

We evaluated several options and chose Essentia's MAEST + emoMusic because:

- **MAEST** outperforms older EffNet-based genre classifiers (0.60–0.77 confidence
  vs 0.31–0.54 in our tests on club music).
- **emoMusic arousal** distinguishes energy levels within electronic music, unlike
  danceability classifiers that saturate at maximum for all club tracks.
- Both are publicly available and well documented.

---

## Attribution

If you use this tool in academic work, please cite:

```
Alonso-Jiménez, P., Pérez-García, T., Bogdanov, D., & Serra, X. (2023).
Music representation learning based on editorial metadata from Discogs.
ISMIR 2023.
```

For full model documentation: https://essentia.upf.edu/models/
