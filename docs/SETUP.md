# Setup

For numbered Windows installation, build, update and login recovery steps,
see [INSTALL.md](../INSTALL.md). For modern Chrome use
[Copy as cURL session import](CHROME_SESSION_IMPORT.md).
The file-copy importer below is a legacy option.

## Runtime dependencies

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

On Windows, a browser installed from a packaged terminal can end up in a
virtualized AppData folder. If Chromium fails with `spawn UNKNOWN` or a
SideBySide activation error, install the same browser in the project folder:

```powershell
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $PWD '.playwright-browsers'
.\.venv\Scripts\python.exe -m playwright install chromium
```

The worker automatically uses this folder when present. An explicitly set
`PLAYWRIGHT_BROWSERS_PATH` takes precedence. Browser profiles and login data
remain in `profiles/`.

## Chrome session import

The desktop importer and `scripts/windows/import_chrome_session.bat` use the
same validation and preserve the current profile if the copy is unusable.
Chrome cookies encrypted with an application-bound key (`v20`) cannot be
migrated to Chromium by copying the cookie database. A profile from a previous
Windows installation can also have an unreadable encryption key. Such imports
are rejected before replacing the current profile. Importing files does not
clear an active login retry pause; the worker clears it after confirming an
authenticated session.

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
