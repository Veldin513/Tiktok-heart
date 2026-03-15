# TikTok Heart Bot — Control Center

Desktop-панель и набор рантайм-скриптов для локального управления TikTok worker, Telegram control bot и связанных служебных файлов проекта.

## Что это за проект

**TikTok Heart Bot** — это десктопный центр управления для полуавтоматической работы с TikTok через Playwright. В основном используеться для отправки какого то сообщения пользователю, с целью поддержать серию(серийчик).  Проект объединяет четыре основных слоя:

1. **Worker** — процесс, который открывает TikTok, проверяет авторизацию, заходит в чат адресата и отправляет сообщение из пула.
2. **Telegram control bot** — отдельный процесс для удалённого управления состоянием проекта.
3. **Desktop Control Center** — GUI на Tkinter для локального управления, диагностики, просмотра логов и редактирования данных.
4. **ProjectAdapter / file-based runtime** — прослойка, которая связывает UI и runtime через файлы состояния, PID-файлы, логи и конфиги.

Цель проекта — дать оператору единый интерфейс для:
- запуска и остановки worker-процесса;
- переключения активного профиля;
- просмотра состояния профилей, целей, cooldown и streak;
- редактирования пула сообщений;
- удалённого контроля через Telegram;
- диагностики проекта без ручного обхода десятков файлов.

Проект особенно важен тем, что **UI не управляет TikTok напрямую**. Он управляет файлами состояния и внешними процессами, а фактическая работа с TikTok вынесена в worker-слой. Это хорошее архитектурное решение: GUI остаётся тонкой оболочкой, а автоматизация браузера не смешивается с кодом интерфейса.

---

## Как проект устроен в целом

Архитектура проекта событийная и файлово-ориентированная.

### Основная идея

- **Состояние** хранится в JSON- и TXT-файлах.
- **UI** читает и изменяет это состояние через `ProjectAdapter`.
- **Worker** читает те же runtime-файлы и действует по ним.
- **Telegram bot** использует те же данные и ту же прослойку управления.

### Упрощённая схема

```text
Desktop UI (desktop_app.py + diagnostics_app.py)
        │
        ▼
ProjectAdapter (project_adapter.py)
        │
        ├── control/control_state.json
        ├── control/profiles.json
        ├── message_pool.txt
        ├── profiles/<profile>/state/*
        ├── profiles/<profile>/logs/*
        └── logs/*

Worker (tiktok_checker.py / ttbot.dispatch.DispatchService)
        │
        └── Playwright + TikTok UI

Telegram control bot (telegram_control_bot.py)
        │
        └── команды оператора → ProjectAdapter
```

### Почему это хорошо

- легко диагностировать систему по файлам;
- процессы можно запускать независимо;
- UI не зависит от внутренностей Playwright;
- тестировать runtime-логику проще, чем монолитный GUI.

### Цена такого подхода

- нужно аккуратно работать с race conditions вокруг PID-файлов и lock-файлов;
- любые повреждения JSON могут ломать отдельные части системы;
- важно, чтобы все модули одинаково трактовали пути и имена файлов.

## Что есть в репозитории

- `desktop_app.py` — вход в desktop UI.
- `diagnostics_app.py` — основная панель управления, логов и диагностики.
- `launcher.py` — объединённый запуск worker + Telegram control bot.
- `tiktok_checker.py` — основной worker Playwright.
- `telegram_control_bot.py` — Telegram bot для удалённого управления.
- `project_adapter.py` — файловый и процессный адаптер проекта.
- `ttbot/` — модели состояния и сервисы.

## Что удалено перед публикацией

Из публичной версии удалены:

- реальные Telegram токены и chat IDs;
- локальные пути пользователя;
- рабочие логи, backups и runtime-state;
- browser profile / `user_data`;
- персональные профили и адресаты.

## Зависимости

Минимальный runtime-стек:

- Python 3.13+ (проект ориентирован на Windows desktop);
- Playwright;
- requests;
- pystray;
- Pillow;
- psutil.

Установка:

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

Для тестов:

```bash
pip install -r requirements-dev.txt
pytest
```

## Быстрый старт

### 1. Подготовьте конфиги

Скопируйте примеры:

```bash
copy control\profiles.example.json control\profiles.json
copy control\control_state.example.json control\control_state.json
copy control\telegram_bot_v2.example.json control\telegram_bot_v2.json
copy control\ui_state.example.json control\ui_state.json
```

Заполните `control/telegram_bot_v2.json`, если нужен Telegram control bot.

### 2. Запустите приложение

Desktop UI:

```bash
python desktop_app.py
```

Или через bat-файлы:

- `start_app.bat` — UI
- `start_bot.bat` — launcher
- `run_telegram_bot_background.bat` — Telegram bot в фоне

### 3. Настройте профили

Пример `control/profiles.json`:

```json
{
  "default": [
    {"name": "sample_target", "url": "@sample_target"}
  ]
}
```


## Полезные замечания

- Если Playwright установлен, но Chromium не скачан, worker не стартует корректно. Выполните `python -m playwright install chromium`.
- `tkinter` входит в стандартную библиотеку Python на Windows. На Linux он может требовать отдельного системного пакета.
- При первом запуске `ProjectAdapter.ensure_runtime_files()` может создать отсутствующие runtime-файлы автоматически.
- Desktop UI и tray-функции используют `pystray` и `Pillow`; без них tray-режим будет недоступен.


## FAQ

### Почему в репозитории нет `control/*.json` с рабочими значениями?
Потому что это runtime-state и секреты. Для GitHub оставлены только `*.example.json`.

### Какие библиотеки нужно установить обязательно?
Для обычного запуска: `playwright`, `requests`, `pystray`, `Pillow`, `psutil`.

### Нужно ли ставить браузер отдельно?
Нужен именно браузерный runtime Playwright:

```bash
python -m playwright install chromium
```

### Можно ли запускать без Telegram?
Да. Оставьте `control/telegram_bot_v2.json` незаполненным или не создавайте его.
