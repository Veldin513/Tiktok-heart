# Setup

## Runtime dependencies

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

## Optional test dependencies

```bash
pip install -r requirements-dev.txt
pytest
```

## Config files

Create runtime files from examples in `control/`:

- `profiles.json`
- `control_state.json`
- `telegram_bot_v2.json`
- `ui_state.json`

The repository tracks only `*.example.json` files to avoid leaking private data and secrets.
