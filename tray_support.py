from __future__ import annotations

import importlib
import sys
import threading
from pathlib import Path

from runtime_paths import bootstrap_site_packages

bootstrap_site_packages()
from dataclasses import dataclass
from typing import Callable

Scheduler = Callable[[Callable[[], None]], None]


_last_probe_error = 'ok'


def _import_module(name: str):
    if name in sys.modules and sys.modules.get(name) is not None:
        return sys.modules[name]
    return importlib.import_module(name)


def tray_supported() -> bool:
    global _last_probe_error
    try:
        _import_module('pystray')
        _import_module('PIL.Image')
        _import_module('PIL.ImageDraw')
        _last_probe_error = 'ok'
        return True
    except Exception as exc:  # noqa: BLE001
        _last_probe_error = str(exc)
        return False


def tray_support_reason() -> str:
    if tray_supported():
        return 'ok'
    if _last_probe_error and _last_probe_error != 'ok':
        return f'Трей недоступен: {_last_probe_error}'
    return 'Трей недоступен: pystray или Pillow не импортируются в текущем интерпретаторе.'


@dataclass(slots=True)
class TrayCallbacks:
    restore: Callable[[], None]
    exit_app: Callable[[], None]
    start_worker: Callable[[], None] | None = None
    stop_worker: Callable[[], None] | None = None
    restart_worker: Callable[[], None] | None = None
    start_telegram_bot: Callable[[], None] | None = None
    stop_telegram_bot: Callable[[], None] | None = None
    restart_telegram_bot: Callable[[], None] | None = None
    start_all: Callable[[], None] | None = None
    stop_all: Callable[[], None] | None = None
    restart_all: Callable[[], None] | None = None
    open_diagnostics: Callable[[], None] | None = None


class TrayIconController:
    def __init__(self, *, title: str, scheduler: Scheduler, callbacks: TrayCallbacks) -> None:
        self.title = title
        self._scheduler = scheduler
        self._callbacks = callbacks
        self._icon = None
        self._started = False
        self._visible = False
        self._last_reason = tray_support_reason()

    def _schedule(self, callback: Callable[[], None] | None) -> None:
        if callback is None:
            return
        self._scheduler(callback)

    def _action(self, callback: Callable[[], None] | None):
        def handler(icon, item) -> None:  # noqa: ARG001
            self._schedule(callback)
        return handler

    def _exit_action(self, icon, item) -> None:  # noqa: ARG001
        self._schedule(self._callbacks.exit_app)

    def _create_image(self):
        from PIL import Image, ImageDraw
        asset = Path(__file__).resolve().parent / 'assets' / 'app_icon.png'
        try:
            if asset.exists():
                return Image.open(asset).convert('RGBA').resize((64, 64))
        except Exception:
            pass
        image = Image.new('RGBA', (64, 64), (28, 28, 32, 0))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((6, 6, 58, 58), radius=16, fill=(124, 58, 237, 255))
        draw.ellipse((16, 14, 48, 46), fill=(14, 165, 233, 255))
        draw.text((19, 18), 'TT', fill=(255, 255, 255, 255))
        draw.text((24, 38), '❤', fill=(255, 255, 255, 255))
        return image

    def start(self) -> bool:
        if self._started:
            return True
        if not tray_supported():
            self._last_reason = tray_support_reason()
            return False
        try:
            import pystray
        except Exception:
            self._last_reason = tray_support_reason()
            return False
        menu = pystray.Menu(
            pystray.MenuItem('Открыть', self._action(self._callbacks.restore), default=True),
            pystray.MenuItem('Открыть центр управления', self._action(self._callbacks.open_diagnostics or self._callbacks.restore)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem('Запустить всё', self._action(self._callbacks.start_all), enabled=lambda item: self._callbacks.start_all is not None),
            pystray.MenuItem('Остановить всё', self._action(self._callbacks.stop_all), enabled=lambda item: self._callbacks.stop_all is not None),
            pystray.MenuItem('Перезапустить всё', self._action(self._callbacks.restart_all), enabled=lambda item: self._callbacks.restart_all is not None),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem('Запустить worker', self._action(self._callbacks.start_worker), enabled=lambda item: self._callbacks.start_worker is not None),
            pystray.MenuItem('Остановить worker', self._action(self._callbacks.stop_worker), enabled=lambda item: self._callbacks.stop_worker is not None),
            pystray.MenuItem('Перезапустить worker', self._action(self._callbacks.restart_worker), enabled=lambda item: self._callbacks.restart_worker is not None),
            pystray.MenuItem('TG bot ▶', self._action(self._callbacks.start_telegram_bot), enabled=lambda item: self._callbacks.start_telegram_bot is not None),
            pystray.MenuItem('TG bot ■', self._action(self._callbacks.stop_telegram_bot), enabled=lambda item: self._callbacks.stop_telegram_bot is not None),
            pystray.MenuItem('TG bot ↻', self._action(self._callbacks.restart_telegram_bot), enabled=lambda item: self._callbacks.restart_telegram_bot is not None),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem('Выход', self._exit_action),
        )
        self._icon = pystray.Icon('tiktok_heart_bot', self._create_image(), title=self.title, menu=menu)
        run_detached = getattr(self._icon, 'run_detached', None)
        if callable(run_detached):
            run_detached()
        else:
            thread = threading.Thread(target=self._icon.run, daemon=True)
            thread.start()
        self._started = True
        self._last_reason = 'ok'
        return True

    def show(self) -> bool:
        if not self.start() or self._icon is None:
            return False
        self._icon.visible = True
        self._visible = True
        return True

    def hide(self) -> None:
        if self._icon is None:
            return
        self._icon.visible = False
        self._visible = False

    def notify(self, message: str) -> None:
        if self._icon is None:
            return
        if getattr(self._icon, 'HAS_NOTIFICATION', False):
            try:
                self._icon.notify(message, self.title)
            except Exception:
                pass

    def stop(self) -> None:
        if self._icon is None:
            return
        try:
            self._icon.visible = False
        except Exception:
            pass
        try:
            self._icon.stop()
        except Exception:
            pass
        self._icon = None
        self._started = False
        self._visible = False

    @property
    def available(self) -> bool:
        return tray_supported()

    @property
    def visible(self) -> bool:
        return self._visible

    @property
    def reason(self) -> str:
        return self._last_reason
