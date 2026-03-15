from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from ttbot.dispatch import BrowserResources, DispatchService
from ttbot.models import BrowserRuntimePolicy, ChatOpenMethod, ChatOpenResult, DispatchSettings, Target
from ttbot.models import AppSettings, NotificationSettings, ProfileConfig


class DummyTelegram:
    def __init__(self):
        self.texts = []
        self.photos = []

    def send_text(self, text):
        self.texts.append(text)

    def send_photo(self, path):
        self.photos.append(path)


class DummyStore:
    def __init__(self):
        self.marked = []

    def get_cooldown_status(self, _name):
        return SimpleNamespace(allowed=True, hours_passed=None, seconds_left=0)

    def mark_sent_now(self, name):
        self.marked.append(name)

    def update_streak_stats(self, _name):
        return SimpleNamespace(current_count=5, is_new_day=True)

    def safe_filename(self, name):
        return name

    def describe_hours_passed(self, value):
        return 'неизвестно'

    def format_duration(self, seconds):
        return f'{seconds}м'


class DummySession:
    def __init__(self, ok=True):
        self.ok = ok
        self.sent = []
        self.page = None

    def open_chat(self, _target):
        if self.ok:
            return ChatOpenResult(ok=True, method=ChatOpenMethod.LEGACY_LIST)
        return ChatOpenResult(ok=False, reason='chat_open_failed')

    def send_symbol(self, symbol):
        self.sent.append(symbol)


class FakePage:
    def __init__(self):
        self.unroute_calls = []
        self.closed = False

    def unroute(self, pattern):
        self.unroute_calls.append(pattern)

    def close(self):
        self.closed = True


class FakeContext:
    def __init__(self, pages):
        self.pages = pages
        self.closed = False

    def close(self):
        self.closed = True


class DispatchServiceTests(unittest.TestCase):
    def build_service(self, dry_run=False):
        tmp = tempfile.TemporaryDirectory()
        runtime = SimpleNamespace(
            store=DummyStore(),
            telegram=DummyTelegram(),
            artifacts_dir=Path(tmp.name),
        )
        settings = AppSettings(
            dispatch=DispatchSettings(cooldown_hours=12, dry_run=dry_run),
            messages=('❤️',),
            notifications=NotificationSettings(token='', chat_ids=(), enabled=False),
        )
        profile = ProfileConfig(name='test', targets=(Target('alice', '@alice'),))
        service = DispatchService(runtime=runtime, settings=settings, profile_config=profile)
        service._tmp = tmp
        return service

    def test_process_target_updates_store_on_success(self):
        service = self.build_service(dry_run=False)
        session = DummySession(ok=True)
        result = service.process_target(session, Target('alice', '@alice'))
        self.assertTrue(result.success)
        self.assertEqual(service.runtime.store.marked, ['alice'])
        self.assertEqual(session.sent, ['❤️'])

    def test_process_target_dry_run_skips_send(self):
        service = self.build_service(dry_run=True)
        session = DummySession(ok=True)
        result = service.process_target(session, Target('alice', '@alice'))
        self.assertTrue(result.success)
        self.assertEqual(session.sent, [])
        self.assertEqual(service.runtime.store.marked, [])
        self.assertEqual(result.reason, 'dry_run')

    def test_run_summary_file_written(self):
        service = self.build_service(dry_run=True)
        from ttbot.models import RunSummary
        summary = RunSummary(profile_name='test', total_targets=1)
        path = service.write_run_summary(summary)
        self.assertTrue(path.exists())

    def test_close_browser_detaches_routes_from_all_pages(self):
        service = self.build_service(dry_run=True)
        pages = [FakePage(), FakePage()]
        context = FakeContext(pages)
        resources = BrowserResources(browser=context, page=pages[0])
        service.close_browser(resources)
        self.assertTrue(all(page.unroute_calls == ['**/*'] for page in pages))
        self.assertTrue(all(page.closed for page in pages))
        self.assertTrue(context.closed)

    def test_browser_runtime_policy_is_mutable(self):
        policy = BrowserRuntimePolicy()
        policy.block_media = False
        self.assertFalse(policy.block_media)

    def test_close_browser_clears_resources_after_shutdown(self):
        service = self.build_service(dry_run=True)
        pages = [FakePage()]
        context = FakeContext(pages)
        resources = BrowserResources(browser=context, page=pages[0])
        service.close_browser(resources)
        self.assertIsNone(resources.browser)
        self.assertIsNone(resources.page)



if __name__ == '__main__':
    unittest.main()
