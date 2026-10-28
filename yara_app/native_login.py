"""A manual login in installed Chrome, without copying another browser's cookies."""
from __future__ import annotations

import json
import logging
import os
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

BROWSER_MARKER = '.heart-browser.json'
LOGIN_PENDING = '.manual_login.json'


def uses_google_chrome(user_data_dir: Path) -> bool:
    try:
        data = json.loads((Path(user_data_dir) / BROWSER_MARKER).read_text(encoding='utf-8'))
        return data.get('channel') == 'chrome'
    except (OSError, ValueError, AttributeError):
        return False


def mark_google_chrome(user_data_dir: Path) -> None:
    user_data_dir.mkdir(parents=True, exist_ok=True)
    (user_data_dir / BROWSER_MARKER).write_text(
        json.dumps({'channel': 'chrome', 'login_method': 'dedicated'}), encoding='utf-8',
    )


@dataclass
class BrowserLease:
    fd: int | None
    browser_dir: Path | None = None

    def release(self) -> None:
        if self.fd is not None:
            try:
                if self.browser_dir is not None:
                    from yara_app.profile_vault import seal_browser
                    seal_browser(self.browser_dir)
            except Exception:
                logging.getLogger(__name__).error('Не удалось зашифровать профиль после закрытия. Исходные файлы сохранены.')
                from yara_app.fileio import atomic_write_text
                with suppress(OSError):
                    atomic_write_text(self.browser_dir / '.vault-error.json', json.dumps({'error': 'Не удалось завершить защиту. Исходные файлы сохранены.'}))
            finally:
                os.close(self.fd)  # The OS also releases the lock on a crash.
                self.fd = None


def acquire_browser_lease(browser_dir: Path) -> BrowserLease:
    browser_dir.mkdir(parents=True, exist_ok=True)
    # Keep this tiny file in place: unlinking an advisory lock can let another
    # process lock a different inode. Ownership is an OS lock, not a saved PID.
    fd = os.open(browser_dir / '.browser_session.lock', os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if os.fstat(fd).st_size == 0:
            os.write(fd, b'0')
        os.lseek(fd, 0, os.SEEK_SET)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        raise RuntimeError('Профиль браузера занят входом, проверкой или worker. Дождитесь завершения.') from None
    try:
        from yara_app.profile_vault import unseal_browser
        unseal_browser(browser_dir)
    except BaseException:
        os.close(fd)
        raise
    return BrowserLease(fd, browser_dir)


@contextmanager
def browser_lease(browser_dir: Path):
    lease = acquire_browser_lease(browser_dir)
    try:
        yield
    finally:
        lease.release()


def find_google_chrome() -> Path:
    for variable, suffix in (
        ('PROGRAMFILES', 'Google/Chrome/Application/chrome.exe'),
        ('PROGRAMFILES(X86)', 'Google/Chrome/Application/chrome.exe'),
        ('LOCALAPPDATA', 'Google/Chrome/Application/chrome.exe'),
    ):
        root = os.environ.get(variable)
        if root:
            candidate = Path(root) / suffix
            if candidate.is_file():
                return candidate
    raise RuntimeError('Google Chrome не найден. Установите обычный Google Chrome и повторите вход.')


def chrome_profile_running(user_data_dir: Path) -> bool:
    import psutil

    expected = Path(user_data_dir).resolve()
    for proc in psutil.process_iter(['name', 'cmdline']):
        try:
            if str(proc.info.get('name') or '').lower() not in {'chrome.exe', 'chrome'}:
                continue
            args = proc.info.get('cmdline') or []
            for index, arg in enumerate(args):
                if arg.startswith('--user-data-dir='):
                    value = arg.split('=', 1)[1]
                elif arg == '--user-data-dir' and index + 1 < len(args):
                    value = args[index + 1]
                else:
                    continue
                if Path(value.strip('"')).resolve() == expected:
                    return True
        except (psutil.Error, OSError, ValueError):
            continue
    return False


def verify_chrome_session(user_data_dir: Path) -> None:
    """Caller holds the active profile lease; do not send messages or export state."""
    from playwright.sync_api import sync_playwright

    from yara_app.auth_flow import _launch_persistent_chromium, wait_for_authenticated_page

    try:
        with sync_playwright() as playwright:
            context = _launch_persistent_chromium(
                playwright, str(user_data_dir), headless=True, _acquire_lease=False,
            )
            try:
                page = context.pages[0] if context.pages else context.new_page()
                response = page.goto('https://www.tiktok.com/messages', wait_until='domcontentloaded', timeout=45000)
                if response is None or response.status >= 400 or not wait_for_authenticated_page(page):
                    raise RuntimeError('not authenticated')
            finally:
                context.close()
    except Exception:
        # Browser errors can include page content and session-bearing URLs.
        raise RuntimeError(
            'Вход пока не подтверждён. Откройте отдельный Chrome, убедитесь, что сообщения доступны, '
            'закройте его окно и повторите проверку. Возможны также ограничение TikTok или проблема сети. '
            'Автоматический повтор не выполняется.'
        ) from None
