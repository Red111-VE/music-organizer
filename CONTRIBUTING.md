# Contributing to Red111 Music Organizer

Thanks for your interest in contributing! / ¡Gracias por tu interés en contribuir!

## Quick guidelines

- **Issues first**: open an issue before sending a big PR, so we can discuss the approach.
- **Small PRs**: prefer focused PRs that do one thing. Easier to review and merge.
- **Tests**: if you add a feature in `core/`, please include a test in `tests/`.
- **Style**: we use `ruff` for formatting and linting. Run `ruff check .` before committing.
- **Commit messages**: prefer English, short imperative ("Add X", "Fix Y", "Refactor Z").

## Development setup

```bash
git clone https://github.com/Red111-VE/music-organizer.git
cd music-organizer
python3 -m venv venv
source venv/bin/activate
pip install -e ".[dev,web]"
./scripts/download_models.sh
pytest
```

## Where to focus

Good areas for first contributions:

- Bug reports with reproduction steps.
- Improvements to the `GENRE_SIMPLIFY` mapping in `core/analyzer.py`.
- Translations of the README to other languages.
- Support for more audio formats.
- UI polish in `web/`.

## Code of conduct

Be kind. Assume good intent. We're a small project; let's keep it pleasant.
