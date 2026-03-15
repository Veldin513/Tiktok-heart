from __future__ import annotations

import sys
import types

import pytest

from tray_support import TrayCallbacks, TrayIconController, tray_supported


class FakeMenuItem:
    def __init__(self, text, action, **kwargs):
        self.text = text
        self.action = action
        self.kwargs = kwargs


class FakeMenu:
    SEPARATOR = object()

    def __init__(self, *items):
        self.items = items


class FakeIcon:
    HAS_NOTIFICATION = True

    def __init__(self, name, image, title=None, menu=None):
        self.name = name
        self.image = image
        self.title = title
        self.menu = menu
        self.visible = False
        self.run_detached_called = False
        self.notifications = []
        self.stopped = False

    def run_detached(self):
        self.run_detached_called = True

    def notify(self, message, title=None):
        self.notifications.append((message, title))

    def stop(self):
        self.stopped = True


@pytest.fixture()
def fake_pystray(monkeypatch):
    module = types.SimpleNamespace(Icon=FakeIcon, Menu=FakeMenu, MenuItem=FakeMenuItem)
    monkeypatch.setitem(sys.modules, 'pystray', module)
    return module


def test_tray_supported_reflects_import_availability(monkeypatch):
    monkeypatch.delitem(sys.modules, 'pystray', raising=False)
    assert tray_supported() is False


def test_tray_controller_can_show_restore_and_stop(fake_pystray):
    scheduled = []
    events = []

    controller = TrayIconController(
        title='Test Tray',
        scheduler=lambda callback: scheduled.append(callback),
        callbacks=TrayCallbacks(
            restore=lambda: events.append('restore'),
            exit_app=lambda: events.append('exit'),
            start_worker=lambda: events.append('start'),
            stop_worker=lambda: events.append('stop'),
        ),
    )

    assert controller.show() is True
    assert controller.visible is True
    assert controller._icon.run_detached_called is True

    open_item = controller._icon.menu.items[0]
    open_item.action(controller._icon, open_item)
    scheduled.pop()()
    assert events == ['restore']

    controller.notify('hidden')
    assert controller._icon.notifications == [('hidden', 'Test Tray')]

    controller.hide()
    assert controller.visible is False
    assert controller._icon.visible is False

    controller.stop()
    assert controller._icon is None


def test_tray_controller_exit_uses_scheduler(fake_pystray):
    scheduled = []
    events = []

    controller = TrayIconController(
        title='Test Tray',
        scheduler=lambda callback: scheduled.append(callback),
        callbacks=TrayCallbacks(
            restore=lambda: None,
            exit_app=lambda: events.append('exit'),
        ),
    )
    controller.show()

    exit_item = controller._icon.menu.items[-1]
    exit_item.action(controller._icon, exit_item)
    scheduled.pop()()
    assert events == ['exit']
