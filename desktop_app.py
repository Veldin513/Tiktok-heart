from __future__ import annotations

import tkinter as tk
import traceback
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
APP_ID = 'tiktokheartbot.controlcenter'
from tkinter import messagebox

from runtime_paths import bootstrap_site_packages

bootstrap_site_packages()

from diagnostics_app import DiagnosticsApp
from single_instance import SingleInstanceGuard
from tray_support import TrayCallbacks, TrayIconController


def _write_startup_error(exc_text: str) -> None:
    base_dir = Path(__file__).resolve().parent
    log_dir = base_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "desktop_app_startup_error.log").write_text(
        "Desktop application startup failed\n\n" + exc_text,
        encoding="utf-8",
    )


def _apply_windows_app_id() -> None:
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except Exception:
        pass


class DesktopApplication:
    """Desktop shell around the diagnostics/control panel."""

    def __init__(self, base_dir: Path | str | None = None) -> None:
        _apply_windows_app_id()
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.geometry('1260x800')
        self.root.minsize(1080, 700)
        try:
            self.root.configure(bg='#eef3f8')
        except Exception:
            pass
        self.root.title('TikTok Bot Control Center')
        self._icon_image = None
        self._icon_bitmap_path = None
        self._apply_window_icon()

        self.tray = TrayIconController(
            title='TikTok Bot Control Center',
            scheduler=lambda callback: self.root.after(0, callback),
            callbacks=TrayCallbacks(
                restore=self.restore_window,
                exit_app=self.exit_application,
                start_worker=self.safe_start_worker,
                stop_worker=self.safe_stop_worker,
                restart_worker=self.safe_restart_worker,
                start_telegram_bot=self.safe_start_telegram_bot,
                stop_telegram_bot=self.safe_stop_telegram_bot,
                restart_telegram_bot=self.safe_restart_telegram_bot,
                start_all=self.safe_start_all,
                stop_all=self.safe_stop_all,
                restart_all=self.safe_restart_all,
                open_diagnostics=self.restore_window,
            ),
        )
        self.panel = DiagnosticsApp(
            self.root,
            base_dir=base_dir,
            on_minimize_to_tray=self.hide_to_tray,
            on_exit_application=self.exit_application,
        )

        self._allow_minimize_to_tray = True
        self._icon_refresh_job = None
        self._bind_window_events()
        self._schedule_icon_refresh()
        self.root.deiconify()
        self.panel.set_status(self._startup_status_text())

    def _apply_window_icon(self) -> None:
        assets_dir = APP_DIR / 'assets'
        png_path = assets_dir / 'app_icon.png'
        ico_path = assets_dir / 'app_icon.ico'
        try:
            if png_path.exists():
                self._icon_image = tk.PhotoImage(file=str(png_path))
        except Exception:
            self._icon_image = None
        self._icon_bitmap_path = str(ico_path) if ico_path.exists() else None
        self._refresh_window_icon()

    def _refresh_window_icon(self) -> None:
        try:
            if self._icon_image is not None:
                self.root.iconphoto(True, self._icon_image)
                self.root.wm_iconphoto(True, self._icon_image)
        except Exception:
            pass
        if self._icon_bitmap_path:
            try:
                self.root.iconbitmap(default=self._icon_bitmap_path)
                self.root.wm_iconbitmap(self._icon_bitmap_path)
            except Exception:
                pass
        try:
            self.root.update_idletasks()
        except Exception:
            pass

    def _schedule_icon_refresh(self, delay_ms: int = 80) -> None:
        try:
            if self._icon_refresh_job is not None:
                self.root.after_cancel(self._icon_refresh_job)
        except Exception:
            pass
        try:
            self._icon_refresh_job = self.root.after(delay_ms, self._refresh_window_icon)
        except Exception:
            self._icon_refresh_job = None

    def _bind_window_events(self) -> None:
        self.root.protocol('WM_DELETE_WINDOW', self.on_close_request)
        self.root.bind('<Unmap>', self._on_unmap)
        self.root.bind('<Map>', self._on_map, add='+')

    def _startup_status_text(self) -> str:
        if self.tray.available:
            return 'Готово. Окно можно свернуть в трей.'
        return f'Готово. Трей сейчас недоступен: {self.tray.reason}'

    def _on_map(self, _event) -> None:
        self._schedule_icon_refresh(40)

    def _on_unmap(self, _event) -> None:
        if not self._allow_minimize_to_tray:
            return
        try:
            state = self.root.state()
        except tk.TclError:
            return
        if state == 'iconic':
            self.root.after(40, self.hide_to_tray)

    def hide_to_tray(self) -> None:
        if not self.tray.show():
            self.panel.set_status(f'Не удалось свернуть в трей: {self.tray.reason}')
            return
        self.root.withdraw()
        self.tray.notify('Приложение свернуто в трей')
        self.panel.set_status('Приложение свернуто в трей')
        self.panel._log_action('Приложение свернуто в трей')

    def restore_window(self) -> None:
        self.tray.hide()
        self.root.deiconify()
        self.root.state('normal')
        self._schedule_icon_refresh(40)
        self.root.lift()
        self.root.focus_force()
        self.panel.refresh_all()
        self.panel.set_status('Окно восстановлено')
        self.panel._log_action('Окно восстановлено из трея')

    def on_close_request(self) -> None:
        if self.tray.available:
            self.hide_to_tray()
            return
        self.exit_application()

    def _safe_action(self, action, error_title: str) -> None:
        try:
            action()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror(error_title, str(exc))

    def safe_start_worker(self) -> None:
        self._safe_action(self.panel.start_worker, 'Ошибка запуска worker')

    def safe_stop_worker(self) -> None:
        self._safe_action(self.panel.stop_worker, 'Ошибка остановки worker')

    def safe_restart_worker(self) -> None:
        self._safe_action(self.panel.restart_worker, 'Ошибка перезапуска worker')

    def safe_start_telegram_bot(self) -> None:
        self._safe_action(self.panel.start_telegram_bot, 'Ошибка запуска Telegram bot')

    def safe_stop_telegram_bot(self) -> None:
        self._safe_action(self.panel.stop_telegram_bot, 'Ошибка остановки Telegram bot')

    def safe_restart_telegram_bot(self) -> None:
        self._safe_action(self.panel.restart_telegram_bot, 'Ошибка перезапуска Telegram bot')

    def safe_start_all(self) -> None:
        self._safe_action(self.panel.start_all, 'Ошибка запуска')

    def safe_stop_all(self) -> None:
        self._safe_action(self.panel.stop_all, 'Ошибка остановки')

    def safe_restart_all(self) -> None:
        self._safe_action(self.panel.restart_all, 'Ошибка перезапуска')

    def exit_application(self) -> None:
        self._allow_minimize_to_tray = False
        self.tray.stop()
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def run(self) -> None:
        self.root.mainloop()


def _notify_already_running() -> None:
    root = tk.Tk()
    root.withdraw()
    try:
        messagebox.showinfo('Уже запущено', 'Control Center уже открыт. Второй экземпляр не будет запущен.')
    finally:
        try:
            root.destroy()
        except tk.TclError:
            pass


def main() -> None:
    lock_path = Path(__file__).resolve().parent / '.desktop_app.lock'
    guard = SingleInstanceGuard.acquire(lock_path, app_name='desktop_app')
    if guard is None:
        _notify_already_running()
        return
    try:
        app = DesktopApplication()
        app._single_instance_guard = guard
        app.run()
    finally:
        guard.release()


if __name__ == '__main__':
    try:
        main()
    except Exception:
        _write_startup_error(traceback.format_exc())
        raise
