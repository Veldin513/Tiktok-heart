# TikTok Heart

**Установка с нуля, с пояснениями для новичка: [INSTALL.md](INSTALL.md).**

**Setup guide: [INSTALL.md](INSTALL.md).**

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
- Diagnostics, logs, launch readiness checks, and maintenance tools.

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

Windows 10/11, Python 3.12+, Playwright Chromium, and Google Chrome for session transfer. Building the Tauri shell also requires Node.js LTS, Rust MSVC, C++ Build Tools, Windows SDK, and WebView2.

Follow [INSTALL.md](INSTALL.md) for downloads, installation, build commands, and checks after each step. A ready-made executable is not included in Git; build the desktop shell before first use.

### First Run

Start the desktop app:

```powershell
.\start_app.bat
```

Silent launcher:

```powershell
wscript.exe .\start_app.vbs
```

Fallback web shell:

```powershell
scripts\windows\start_app_shell.bat
```

The Tauri window uses a local backend, usually `http://127.0.0.1:5874/`. For daily use, double-click `start_app.vbs` to open the app without a console.

### Configuration

`.env.example` lists supported environment variables. Set them in the process environment; the app does not automatically load a `.env` file.

Important variables:

- `TG_TOKEN` - Telegram bot token.
- `TG_CHAT_IDS` - chat IDs for notifications.
- `TIKTOK_DEFAULT_PROFILE` - default local profile name.
- `TIKTOK_BOT_PROFILE` - profile used by helper scripts.

The app can also read Telegram settings from `control/telegram_bot_v2.json`. That file is local-only and must never be committed.

Telegram setup is available in **Advanced mode → Management**: open BotFather, check a token, find your chat ID after `/start`, select allowed users, and save or edit settings. Stop the bot before editing, then start it again. See [the step-by-step setup](INSTALL.md#14-создайте-telegram-бота--по-желанию).

### Browser Authorization

Use the Browser tab to:

- choose the active worker profile;
- import a TikTok session from Google Chrome;
- open the Chromium profile folder;
- compact profile storage;
- create or delete authorization backups for the selected profile.

Use **Browser → Open Chrome for login** to sign in manually in a separate Google Chrome profile. Close that window, then click **Verify login**. Routine runs reuse this profile in hidden Chrome; your regular Chrome session is not copied. If login expires, use the same buttons to reopen the saved profile. See [the login guide](docs/CHROME_SESSION_IMPORT.md). cURL transfer remains a secondary option; file copying cannot transfer Chrome `v20` cookies to Chromium.

Authorization backup archives may contain cookies, browser session files, Telegram config, targets, and local state. Keep them private.

### Scheduler

Use the app scheduler controls or the scripts in `scripts/windows/`:

```powershell
scripts\windows\register_worker_schedule.bat
scripts\windows\unregister_worker_schedule.bat
```

The intended schedule is Windows sign-in plus every 12 hours. The sign-in task waits two minutes for the desktop session to become available.

The next repeat is aligned once with the actual send cooldown, including manual runs. There is no frequent background queue polling. If an older task cannot be saved because of access permissions, register it once as administrator as described in [INSTALL.md](INSTALL.md#11-включите-расписание--по-желанию).

After updating an existing installation, the scheduled tasks continue using the scripts in this project folder. Replace the source files in place; keep your local `control/` and `profiles/` folders. If you move the project, register the schedule again so its paths are updated.

### Tests

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check .
node --check app_shell\web\app.js
```

### Build

```powershell
$env:YARA_PYTHON = (Resolve-Path ".\.venv\Scripts\python.exe").Path
npm.cmd ci
npm.cmd run build -- --no-bundle
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
.\.venv\Scripts\python.exe scripts\security_scan.py
git status --ignored
```

If a token was ever pushed to GitHub, rotate it and rewrite Git history or recreate the repository.

### Troubleshooting

- App does not open: check [INSTALL.md](INSTALL.md#13-если-что-то-не-получилось), then try `.\start_app.bat` and the fallback web shell.
- Browser authorization is missing: open Browser, sign in through the separate Chrome window, close it, and verify login.
- Telegram control is not ready: verify local `control/telegram_bot_v2.json`.
- Worker opens too often: check scheduler status and worker logs.
- UI data looks stale: use Refresh or check whether auto-refresh is enabled.
- Profile folder is too large: compact the browser profile and prune old backups.
- Short freezes in the UI: check `logs/app_shell_perf.log` for slow local API calls.

## Русский

В разделе «Профили» доступны создание профиля и добавление адресата по имени из чатов и ссылке / @username. Оттуда можно перейти к переносу входа для выбранного профиля. При потере авторизации worker останавливается до нового подтверждённого импорта из Chrome, сохраняя профиль.

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
- Диагностика, логи, готовность к запуску и обслуживание.

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

Для работы нужны Windows 10/11, Python 3.12+ и отдельный Chromium от Playwright. Google Chrome используется для переноса входа. Для сборки окна Tauri нужны Node.js LTS, Rust MSVC, C++ Build Tools, Windows SDK и WebView2.

**Пошаговая установка с нуля вынесена в [INSTALL.md](INSTALL.md): ссылки на загрузку, действия в установщиках, команды и ожидаемые результаты.** Готового exe в Git нет — перед первым запуском нужно собрать оболочку по инструкции.

### Запуск

Обычный запуск:

```powershell
.\start_app.bat
```

Запуск без окна консоли:

```powershell
wscript.exe .\start_app.vbs
```

Fallback web-оболочка:

```powershell
scripts\windows\start_app_shell.bat
```

Окно Tauri работает через локальный backend, обычно `http://127.0.0.1:5874/`. Для ежедневного запуска без cmd дважды щёлкните `start_app.vbs`. Команды в содержимое этого файла вставлять не нужно.

### Настройка

Файл `.env.example` перечисляет поддерживаемые переменные окружения. Автоматической загрузки `.env` нет: задайте переменные в окружении процесса или используйте локальные настройки приложения.

Основные переменные:

- `TG_TOKEN` - токен Telegram-бота.
- `TG_CHAT_IDS` - chat ID для уведомлений.
- `TIKTOK_DEFAULT_PROFILE` - профиль по умолчанию.
- `TIKTOK_BOT_PROFILE` - профиль для helper-скриптов.

Telegram можно также настроить через `control/telegram_bot_v2.json`. Это локальный файл, его нельзя коммитить.

В **Расширенном режиме → Управление** есть мастер настройки Telegram: переход к BotFather, проверка токена, поиск своего ID после `/start`, выбор пользователей и сохранение или редактирование настроек. Перед изменением остановите бота, после сохранения запустите снова. [Пошаговая инструкция](INSTALL.md#14-создайте-telegram-бота--по-желанию).

### Авторизация браузера

Во вкладке "Браузер" можно:

- выбрать профиль worker;
- импортировать TikTok-авторизацию из Google Chrome;
- открыть папку Chromium-профиля;
- сжать профиль;
- создать или удалить backup авторизации выбранного профиля.

Используйте **«Браузер» → «Открыть Chrome для входа»**. Войдите в TikTok вручную в отдельном окне, закройте его и нажмите **«Проверить вход»**. Дальше worker использует этот же профиль в скрытом Google Chrome; основная сессия обычного Chrome не копируется. При потере входа те же кнопки откроют сохранённый профиль. Подробности: [инструкция входа](docs/CHROME_SESSION_IMPORT.md). Перенос cURL оставлен дополнительным способом; простое копирование папки не переносит cookies Chrome `v20` в Chromium.

Backup авторизации может содержать cookies, browser session, Telegram config, адресатов и локальное состояние. Его нельзя публиковать.

### Расписание

Расписание можно включать из приложения или через скрипты:

```powershell
scripts\windows\register_worker_schedule.bat
scripts\windows\unregister_worker_schedule.bat
```

Основной режим: запуск после входа в Windows и далее каждые 12 часов. Задача входа ждёт две минуты, чтобы рабочий стол успел загрузиться.

Время следующего повтора один раз согласуется с фактической отправкой, включая ручные запуски. Частых фоновых проверок очереди нет. Если старые задачи не сохраняются из-за прав доступа, один раз перерегистрируй их от имени администратора по [инструкции установки](INSTALL.md#11-включите-расписание--по-желанию).

При обновлении на месте задачи продолжают использовать скрипты из этой папки. Замени исходники, сохранив локальные папки `control/` и `profiles/`. Если переносишь проект в другую папку, зарегистрируй расписание заново для обновления путей.

### Проверки

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check .
node --check app_shell\web\app.js
```

### Сборка

```powershell
$env:YARA_PYTHON = (Resolve-Path ".\.venv\Scripts\python.exe").Path
npm.cmd ci
npm.cmd run build -- --no-bundle
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
.\.venv\Scripts\python.exe scripts\security_scan.py
git status --ignored
```

Если токен уже попадал в GitHub, его нужно перевыпустить и переписать историю Git или пересоздать репозиторий.

### Частые проблемы

- Приложение не открывается: проверь [раздел решения проблем](INSTALL.md#13-если-что-то-не-получилось), затем попробуй `.\start_app.bat` и диагностическую web-оболочку.
- Слетела авторизация: во вкладке «Браузер» открой отдельный Chrome, войди вручную, закрой окно и проверь вход.
- Telegram не готов: проверь локальный `control/telegram_bot_v2.json`.
- Worker запускается лишний раз: проверь расписание и worker logs.
- Информация в UI не обновляется: нажми Refresh или проверь автообновление.
- Профиль слишком большой: сожми Chromium-профиль и удали старые backup.
- Короткие фризы в интерфейсе: проверь `logs/app_shell_perf.log`, там пишутся медленные локальные API-запросы.
