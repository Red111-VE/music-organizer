#!/bin/bash
# Download Essentia models required by Red111 Music Organizer.
# Models are NOT bundled in the repo because (1) they're heavy (~500 MB total)
# and (2) they have a different license than the code (see docs/MODELS.md).

set -e

MODELS_DIR="${MODELS_DIR:-./models}"
BASE_URL="https://essentia.upf.edu/models"

mkdir -p "$MODELS_DIR"
cd "$MODELS_DIR"

echo "Downloading Essentia models into $MODELS_DIR"
echo "Total size: ~500 MB. This may take several minutes."
echo ""

download() {
    local url="$1"
    local filename="$2"
    if [ -f "$filename" ]; then
        echo "  ✓ $filename already exists, skipping"
    else
        echo "  → Downloading $filename"
        curl -L -o "$filename" "$url"
    fi
}

# Genre — MAEST
download "$BASE_URL/feature-extractors/discogs-maest/discogs-maest-30s-pw-2.pb" \
    "discogs-maest-30s-pw-2.pb"
download "$BASE_URL/classifiers/genre_discogs400/genre_discogs400-discogs-maest-30s-pw-1.pb" \
    "genre_discogs400-discogs-maest-30s-pw-1.pb"
download "$BASE_URL/classifiers/genre_discogs400/genre_discogs400-discogs-maest-30s-pw-1.json" \
    "genre_discogs400-discogs-maest-30s-pw-1.json"

# Energy — emoMusic on VGGish
download "$BASE_URL/feature-extractors/vggish/audioset-vggish-3.pb" \
    "audioset-vggish-3.pb"
download "$BASE_URL/classifiers/emomusic/emomusic-audioset-vggish-2.pb" \
    "emomusic-audioset-vggish-2.pb"

echo ""
echo "✓ All models downloaded into $MODELS_DIR"
echo ""
echo "Note: these models are licensed CC BY-NC-SA 4.0 (non-commercial)."
echo "See docs/MODELS.md for details."
