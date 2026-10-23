# TikTok Heart

Windows-приложение на Tauri для локального TikTok worker: профили Chromium, адресаты и сообщения, расписание, диагностика, резервные копии авторизации и опциональное управление через Telegram.

**English:** Windows desktop control app for a local TikTok worker. Follow the numbered PowerShell setup below. For login recovery, see [Chrome session import](docs/CHROME_SESSION_IMPORT.md).

## Исправления запуска и входа

- Импорт из Chrome через Copy as cURL. Команда не выполняется: читаются только cookies первого запроса.
- Проверка новой сессии в отдельном профиле рабочего Chromium, резервная копия прежнего входа и сохранение старого профиля при ошибке.
- Проверка видимых элементов авторизованного интерфейса вместо одного адреса `/messages`.
- Отклонение копирования Chrome-профилей с недоступным ключом шифрования или cookies `v20`.
- Поддержка Chromium в `.playwright-browsers`: в этой установке это устранило `spawn UNKNOWN` / SideBySide при запуске браузера из AppData.
- Скрипты Windows выбирают проектную `.venv` после явно заданного `YARA_PYTHON`.

Подробности: [CHANGELOG.md](CHANGELOG.md).

## Установка с нуля: Windows 10/11

Все команды выполняйте в **PowerShell из корня проекта**, где находятся `README.md` и `start_app.bat`. Активация виртуального окружения не требуется.

### 1. Установите программы

- [Python](https://www.python.org/downloads/windows/) 3.12+, 64 bit — настоящую установку, а не только псевдоним Microsoft Store.
- [Node.js](https://nodejs.org/) LTS с npm.
- [Rust через rustup](https://rustup.rs/), стандартный Windows MSVC toolchain.
- Microsoft C++ Build Tools: **Desktop development with C++** и Windows SDK.
- Microsoft Edge WebView2 Runtime для окна Tauri.
- Google Chrome с действующим входом в TikTok.
- [Git for Windows](https://git-scm.com/downloads/win) для клонирования и обновления через Git.

Зависимости сборки Windows: [официальная инструкция Tauri](https://v2.tauri.app/start/prerequisites/#windows). Rust и C++ нужны для сборки; Python и Chromium — для работы worker. После установки откройте новый PowerShell.

```powershell
py --version
node --version
npm.cmd --version
rustc --version
cargo --version
```

Если команды `py` нет, используйте `python` на шаге 3, предварительно проверив `python --version`.

### 2. Скачайте исходники

```powershell
git clone https://github.com/Veldin513/Tiktok-heart.git
Set-Location .\Tiktok-heart
```

Альтернатива: GitHub → Code → Download ZIP. Распакуйте архив в постоянную папку. Не запускайте проект из архива и не перемещайте папку после настройки расписания без повторной регистрации задач.

### 3. Создайте Python-окружение

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-desktop.txt
.\.venv\Scripts\python.exe -c "import playwright, requests, psutil; print('Python dependencies OK')"
```

Скрипты запуска автоматически используют `.venv\Scripts\python.exe`. Переменная `YARA_PYTHON`, если задана, имеет приоритет.

### 4. Установите рабочий Chromium в папку проекта

```powershell
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $PWD '.playwright-browsers'
.\.venv\Scripts\python.exe -m playwright install chromium
```

Это Chromium от Playwright. Google Chrome его не заменяет. Приложение автоматически использует папку `.playwright-browsers`, если она существует; явно заданная переменная `PLAYWRIGHT_BROWSERS_PATH` имеет приоритет.

После обновления Playwright повторите установку браузера: новая версия может требовать другую ревизию Chromium.

### 5. Установите Node-зависимости и соберите Tauri

```powershell
npm.cmd ci
$env:YARA_PYTHON = (Resolve-Path '.\.venv\Scripts\python.exe').Path
npm.cmd run build -- --no-bundle
```

Результат: `src-tauri\target\release\yara-control.exe`. Для разработки вместо сборки можно выполнить `npm.cmd run dev` с тем же `YARA_PYTHON`.

Готовый exe в Git не хранится. Текущая оболочка использует Python и исходники из папки проекта: перенос одного exe на другой компьютер не является полной установкой.

### 6. Запустите приложение

```powershell
.\start_app.bat
```

После сборки откроется Tauri. В дальнейшем запускайте этот файл двойным щелчком. Для запуска без консоли:

```powershell
wscript.exe .\start_app.vbs
```

Тихий launcher запускает готовый Tauri exe напрямую с видимым окном приложения.
Консоль отключена и в debug-сборке. Если используете сборку до этого исправления
и рядом с приложением остаётся cmd, один раз пересоберите Tauri по шагу 5.
Сам `start_app.vbs` не нужно менять или вставлять в него команду запуска.

Для диагностики отдельно откройте тот же локальный интерфейс в браузере:

```powershell
.\scripts\windows\start_app_shell.bat
```

Обычный локальный адрес — `http://127.0.0.1:5874/`. Основной способ запуска приложения — Tauri.

### 7. Перенесите авторизацию из обычного Chrome

Остановите worker. В приложении откройте **«Браузер» → «Импорт из Chrome»** и выберите профиль приложения.

1. В обычном Chrome войдите в TikTok и откройте `https://www.tiktok.com/messages`.
2. Нажмите F12 → Network (Сеть) → Ctrl+R.
3. Найдите первую строку `messages` с типом `document`.
4. Правая кнопка → Copy → Copy as cURL (bash). Обычный Copy as cURL также поддерживается.
5. Вставьте запрос в поле **«Запрос из Chrome»** приложения → **«Проверить и сохранить вход»**.
6. Дождитесь успешного результата. Worker автоматически не запускается.

Chrome закрывать не требуется. Пароль приложению не нужен. Запрос содержит действующую сессию: вставляйте его только в приложение, не отправляйте в чат, issues или GitHub.

Если TikTok блокирует вход непосредственно в Chromium, используйте этот перенос из Chrome. Старое копирование каталога Chrome не переносит современные cookies `v20`.

**Если авторизация слетит**, повторите пункты 1–6 со свежим запросом. Перенос при каждом запуске не нужен. Автоматическое восстановление истёкшей сессии без участия пользователя не реализовано. Локальный срок cookies после импорта — до 30 дней; TikTok может отозвать сессию раньше.

Подробная инструкция: [перенос и восстановление входа](docs/CHROME_SESSION_IMPORT.md).

### 8. Настройте и проверьте worker

- Укажите своих адресатов и сообщения.
- Включите **dry_run** для первоначальной проверки и выполните self-test. Self-test не доказывает успешную отправку сообщения.
- Проверьте активный профиль и журнал worker.
- Для реальной отправки отключите dry_run и запустите worker.

Локальные настройки создаются приложением. Примеры: `control/*.example.json`. Не заменяйте ими свои существующие настройки при обновлении.

### 9. Включите расписание, если оно нужно

Используйте управление расписанием в приложении либо:

```powershell
.\scripts\windows\register_worker_schedule.bat
```

Предусмотрены запуск при входе в Windows с задержкой две минуты и каждые 12 часов. Если регистрация задач требует дополнительных прав, используйте терминал с нужными правами. Проверьте статус задач в приложении.

Удаление расписания:

```powershell
.\scripts\windows\unregister_worker_schedule.bat
```

После перемещения проекта зарегистрируйте расписание заново: задачи содержат пути к скриптам.

## Telegram — необязательно

Настройте локальный `control/telegram_bot_v2.json` по `control/telegram_bot_v2.example.json` либо задайте переменные окружения `TG_TOKEN` и `TG_CHAT_IDS`. Файл `.env.example` перечисляет переменные; автоматическая загрузка `.env` приложением не предусмотрена. Токены не публикуйте.

## Обновление существующей установки

1. Остановите worker и закройте приложение.
2. Создайте резервную копию авторизации и локальных настроек.
3. В Git-клоне выполните `git pull --ff-only`. Собственные изменения сначала сохраните отдельно. При использовании ZIP обновите исходники в той же папке.
4. Сохраните свои `control/`, `profiles/`, `logs/`, `backups/` и локальные секреты.
5. Повторите установку Python-зависимостей и `npm.cmd ci`. Если обновился Playwright, повторите шаг 4 установки.
6. Если менялась Rust/Tauri-оболочка, повторите сборку. Для изменений Python и локального интерфейса существующий exe обычно подходит.
7. Запустите `start_app.bat`, проверьте вход и при необходимости повторите импорт из Chrome.

## Известные проблемы

| Проблема | Решение |
| --- | --- |
| Python открывает Microsoft Store | Установить Python; проверить `py --version`; использовать явный путь к Python из `.venv`. |
| `ModuleNotFoundError` | Установить зависимости в `.venv`; проверить, что `YARA_PYTHON` не указывает на другой Python. |
| `npm.ps1 cannot be loaded` | Использовать `npm.cmd`. |
| Не найдены `link.exe` / Windows SDK | Установить C++ Build Tools и SDK, открыть новый терминал. |
| Пустое окно Tauri | Проверить WebView2 и запуск backend через `start_app_shell.bat`. |
| Chromium: `spawn UNKNOWN` / SideBySide | Установить браузер в `.playwright-browsers` по шагу 4. Это решило проблему здесь; возможны и другие причины. |
| `Executable doesn't exist` | Установить Chromium той же версией Playwright и в ту же папку, которую использует приложение. |
| TikTok блокирует вход в Chromium | Войти в Chrome и выполнить перенос по шагу 7. |
| Импорт каталога сообщает про `v20` или ключ | Использовать импорт запроса вместо копирования каталога. |
| Импорт запроса не подтвердил вход | Проверить сообщения в Chrome и подключение; скопировать свежий `messages`. При ошибке старый профиль сохраняется. |
| Worker не запускается или запускается часто | Проверить активный профиль, паузы, расписание и журналы в приложении. |

## Проверка кода

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check .
node --check app_shell/web/app.js
.\.venv\Scripts\python.exe scripts/security_scan.py
```

Последняя команда проверяет файлы Git, поэтому запускается в Git-клоне.

## Структура и приватные данные

- `app_shell/` — интерфейс и локальный API.
- `yara_app/` — Python, worker, импорт сессий и диагностика.
- `scripts/windows/` — запуск, расписание и обслуживание.
- `src-tauri/` — Tauri.
- `tests/` — тесты.
- `control/`, `profiles/`, `logs/`, `backups/` — локальные данные.
- `.venv/`, `.playwright-browsers/`, `node_modules/`, `src-tauri/target/` — локальные зависимости и сборки.

В GitHub публикуется только код. Cookies, токены, runtime JSON, базы браузера, логи и архивы авторизации должны оставаться приватными.
