# TikTok Heart

TikTok Heart is a Windows desktop control app for a local TikTok worker. It keeps a Chromium session, runs scheduled message checks, shows diagnostics, manages profiles/messages, and can be controlled through an optional Telegram bot.

> This repository contains application code only. Real cookies, browser sessions, Telegram tokens, local state, logs, backups, and release binaries are intentionally ignored by Git.

## English

### Features

- Local Tauri desktop shell with a clean web UI.
- Chromium profile management for the worker session.
- Import TikTok authorization from an existing Google Chrome profile.
- Per-profile targets, message pool, cooldowns, and streak state.
- Worker controls: start, stop, restart, pause, reset flags, self-test.
- Windows Task Scheduler integration for startup and 12-hour runs.
- Optional Telegram control bot.
- Authorization backups for the selected profile.
- Public-safe source backups that exclude private runtime data.
- Diagnostics, logs, health checks, and maintenance tools.

### 2.2 Highlights

- Scheduled runs verify the worker script and process creation time; reused Windows PIDs no longer block a run.
- Worker locks recover after reboot, and completed workers clear their saved PID.
- The silent Windows launcher correctly passes startup and self-test switches to PowerShell.
- Authorization backup restore, worker lifecycle history, and lightweight live status are included.
- Existing local profiles, authorization, and schedule settings are preserved during an update.

See [CHANGELOG.md](CHANGELOG.md) for release details.

### Project Layout

```text
app_shell/          Local web UI and HTTP API used by the desktop shell
yara_app/           Python runtime, worker adapter, diagnostics, Telegram control
scripts/windows/    Windows helper scripts for startup, scheduling, Chrome import
src-tauri/          Tauri desktop wrapper
tests/              Python test suite
control/            Local runtime config, ignored except example files
profiles/           Local browser/session data, ignored
backups/            Local private backups, ignored
logs/               Runtime logs, ignored
release/            Local build output, ignored
```

### Requirements

- Windows 10/11
- Python 3.12+
- Node.js LTS
- Rust toolchain
- Google Chrome or Chromium

Install dependencies:

```powershell
python -m pip install -r requirements.txt
python -m playwright install chromium
npm install
```

### First Run

Start the desktop app:

```powershell
.\start_app.bat
```

Silent launcher:

```powershell
wscript .\start_app.vbs
```

Fallback web shell:

```powershell
scripts\windows\start_app_shell.bat
```

The app opens a local URL such as `http://127.0.0.1:5874/`.

### Configuration

Copy `.env.example` to `.env` only for local use:

```powershell
Copy-Item .env.example .env
```

Important variables:

- `TG_TOKEN` - Telegram bot token.
- `TG_CHAT_IDS` - chat IDs for notifications.
- `TIKTOK_DEFAULT_PROFILE` - default local profile name.
- `TIKTOK_BOT_PROFILE` - profile used by helper scripts.

The app can also read Telegram settings from `control/telegram_bot_v2.json`. That file is local-only and must never be committed.

### Browser Authorization

Use the Browser tab to:

- choose the active worker profile;
- import a TikTok session from Google Chrome;
- open the Chromium profile folder;
- compact profile storage;
- create or delete authorization backups for the selected profile.

Authorization backup archives may contain cookies, browser session files, Telegram config, targets, and local state. Keep them private.

### Scheduler

Use the app scheduler controls or the scripts in `scripts/windows/`:

```powershell
scripts\windows\register_worker_schedule.bat
scripts\windows\unregister_worker_schedule.bat
```

The intended schedule is Windows sign-in plus every 12 hours. The sign-in task waits two minutes for the desktop session to become available.

After updating an existing installation, the scheduled tasks continue using the scripts in this project folder. Replace the source files in place; keep your local `control/` and `profiles/` folders. If you move the project, register the schedule again so its paths are updated.

### Tests

```powershell
python -m pytest
python -m ruff check .
node --check app_shell\web\app.js
```

### Build

```powershell
npm run build
```

Build output is created under `src-tauri/target/release/`. Local release copies can be stored under `release/`; that folder is ignored by Git.

### Privacy And Security

Never commit:

- `control/*.json`
- `.env` or `.env.*`
- `profiles/`
- `logs/`
- `backups/`
- `release/`
- `node_modules/`
- `src-tauri/target/`
- browser files such as `Cookies`, `Login Data`, `Local Storage`, `IndexedDB`

Before publishing:

```powershell
python scripts\security_scan.py
git status --ignored
```

If a token was ever pushed to GitHub, rotate it and rewrite Git history or recreate the repository.

### Troubleshooting

- App does not open: run `.\start_app.bat`, then check `logs/launcher.log`.
- Browser authorization is missing: open Browser tab and import from Chrome again.
- Telegram control is not ready: verify local `control/telegram_bot_v2.json`.
- Worker opens too often: check scheduler status and worker logs.
- UI data looks stale: use Refresh or check whether auto-refresh is enabled.
- Profile folder is too large: compact the browser profile and prune old backups.
- Short freezes in the UI: check `logs/app_shell_perf.log` for slow local API calls.

## Русский

### Что это

TikTok Heart - Windows-приложение для локального TikTok worker. Оно хранит Chromium-сессию, запускает проверки по расписанию, показывает диагностику, управляет профилями, сообщениями, логами и может дополнительно управляться через Telegram-бота.

В репозитории должен храниться только код. Реальные cookies, авторизации браузера, Telegram-токены, локальное состояние, логи, backup и сборки не должны попадать в Git.

### Возможности

- Локальное desktop-приложение на Tauri.
- Управление Chromium-профилем worker.
- Импорт авторизации TikTok из Google Chrome.
- Профили, адресаты, пул сообщений, cooldown и streak-состояние.
- Управление worker: запуск, остановка, перезапуск, пауза, сброс флагов, self-test.
- Автозапуск Windows и запуск каждые 12 часов через Task Scheduler.
- Опциональный Telegram-бот для управления.
- Backup авторизации выбранного профиля.
- Backup исходников без приватных runtime-данных.
- Диагностика, логи, здоровье проекта и обслуживание.

### Что нового в 2.2

- Запуск по расписанию проверяет скрипт и время создания процесса: повторно выданный Windows номер процесса больше не блокирует работу.
- Блокировки worker восстанавливаются после перезагрузки, а завершённый worker очищает свой сохранённый PID.
- Тихий Windows-запуск правильно передаёт параметры автозапуска и самопроверки в PowerShell.
- Добавлены восстановление backup авторизации, история этапов worker и облегчённое обновление статуса.
- При обновлении сохраняются локальные профили, авторизация и параметры расписания.

Подробности версии: [CHANGELOG.md](CHANGELOG.md).

### Структура проекта

```text
app_shell/          Локальный web UI и HTTP API
yara_app/           Python runtime, worker, диагностика, Telegram control
scripts/windows/    Windows-скрипты запуска, расписания и импорта Chrome
src-tauri/          Desktop-обертка Tauri
tests/              Тесты
control/            Локальные настройки, в Git только example-файлы
profiles/           Локальные browser/session данные, игнорируются
backups/            Приватные backup, игнорируются
logs/               Логи, игнорируются
release/            Локальные сборки, игнорируются
```

### Установка зависимостей

Нужны Windows 10/11, Python 3.12+, Node.js LTS, Rust toolchain, Chrome или Chromium.

```powershell
python -m pip install -r requirements.txt
python -m playwright install chromium
npm install
```

### Запуск

Обычный запуск:

```powershell
.\start_app.bat
```

Запуск без окна консоли:

```powershell
wscript .\start_app.vbs
```

Fallback web-оболочка:

```powershell
scripts\windows\start_app_shell.bat
```

Приложение открывает локальный адрес вида `http://127.0.0.1:5874/`.

### Настройка

Для локальных переменных можно скопировать пример:

```powershell
Copy-Item .env.example .env
```

Основные переменные:

- `TG_TOKEN` - токен Telegram-бота.
- `TG_CHAT_IDS` - chat ID для уведомлений.
- `TIKTOK_DEFAULT_PROFILE` - профиль по умолчанию.
- `TIKTOK_BOT_PROFILE` - профиль для helper-скриптов.

Telegram можно также настроить через `control/telegram_bot_v2.json`. Это локальный файл, его нельзя коммитить.

### Авторизация браузера

Во вкладке "Браузер" можно:

- выбрать профиль worker;
- импортировать TikTok-авторизацию из Google Chrome;
- открыть папку Chromium-профиля;
- сжать профиль;
- создать или удалить backup авторизации выбранного профиля.

Backup авторизации может содержать cookies, browser session, Telegram config, адресатов и локальное состояние. Его нельзя публиковать.

### Расписание

Расписание можно включать из приложения или через скрипты:

```powershell
scripts\windows\register_worker_schedule.bat
scripts\windows\unregister_worker_schedule.bat
```

Основной режим: запуск после входа в Windows и далее каждые 12 часов. Задача входа ждёт две минуты, чтобы рабочий стол успел загрузиться.

При обновлении на месте задачи продолжают использовать скрипты из этой папки. Замени исходники, сохранив локальные папки `control/` и `profiles/`. Если переносишь проект в другую папку, зарегистрируй расписание заново для обновления путей.

### Проверки

```powershell
python -m pytest
python -m ruff check .
node --check app_shell\web\app.js
```

### Сборка

```powershell
npm run build
```

Результат сборки появляется в `src-tauri/target/release/`. Локальную папку `release/` можно использовать для копий артефактов, она игнорируется Git.

### Приватность и безопасность

Нельзя коммитить:

- `control/*.json`
- `.env` и `.env.*`
- `profiles/`
- `logs/`
- `backups/`
- `release/`
- `node_modules/`
- `src-tauri/target/`
- browser-файлы `Cookies`, `Login Data`, `Local Storage`, `IndexedDB`

Перед публикацией:

```powershell
python scripts\security_scan.py
git status --ignored
```

Если токен уже попадал в GitHub, его нужно перевыпустить и переписать историю Git или пересоздать репозиторий.

### Частые проблемы

- Приложение не открывается: запусти `.\start_app.bat` и проверь `logs/launcher.log`.
- Слетела авторизация: заново импортируй авторизацию во вкладке "Браузер".
- Telegram не готов: проверь локальный `control/telegram_bot_v2.json`.
- Worker запускается лишний раз: проверь расписание и worker logs.
- Информация в UI не обновляется: нажми Refresh или проверь автообновление.
- Профиль слишком большой: сожми Chromium-профиль и удали старые backup.
- Короткие фризы в интерфейсе: проверь `logs/app_shell_perf.log`, там пишутся медленные локальные API-запросы.
