from __future__ import annotations

import os
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

import yara_app.tiktok_checker as checker


class FakePage:
    def __init__(self) -> None:
        self.goto_calls: list[str] = []

    def on(self, *_args):
        pass

    def goto(self, url: str, **_kwargs) -> None:
        self.goto_calls.append(url)


class FakeContext:
    def __init__(self) -> None:
        self.page = FakePage()
        self.pages = [self.page]
        self.closed = False

    def new_page(self) -> FakePage:
        return self.page

    def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize('navigation_fails', [False, True])
def test_browser_self_test_closes_context_before_playwright_stops(monkeypatch, navigation_fails):
    events = []
    context = FakeContext()
    context.page.url = 'https://www.tiktok.com/messages'

    @contextmanager
    def fake_playwright():
        events.append('start')
        try:
            yield object()
        finally:
            events.append('stop')

    if navigation_fails:
        def fail_navigation(*_args, **_kwargs):
            raise RuntimeError('navigation failed')
        context.page.goto = fail_navigation
    monkeypatch.setattr(checker, 'sync_playwright', fake_playwright)
    monkeypatch.setattr(checker, 'init_work_browser', lambda *_args: context)
    monkeypatch.setattr(checker, 'detect_captcha', lambda *_args, **_kwargs: {'found': False})
    monkeypatch.setattr(checker, 'wait_for_authenticated_page', lambda _page: True)
    monkeypatch.setattr(checker, 'safe_close_context', lambda *_args: events.append('close'))

    result = checker.run_browser_self_test()

    assert events == ['start', 'close', 'stop']
    assert result['ok'] is not navigation_fails


def test_open_logged_in_work_browser_reuses_authorized_context(monkeypatch) -> None:
    context = FakeContext()

    monkeypatch.setattr(checker, "browser_profile_needs_recovery", lambda _paths: False)
    monkeypatch.setattr(checker, "init_work_browser", lambda _playwright, _user_data_dir: context)
    monkeypatch.setattr(checker, "wait_for_authenticated_page", lambda _page: True)
    monkeypatch.setattr(checker, "detect_captcha", lambda *_a, **_kw: {"found": False})
    monkeypatch.setattr(checker, "clear_auth_backoff", lambda _path: None)

    logged_in, returned_context, returned_page = checker.open_logged_in_work_browser(object())

    assert logged_in is True
    assert returned_context is context
    assert returned_page is context.page
    assert context.closed is False
    assert context.page.goto_calls == ["https://www.tiktok.com/messages"]


def test_lost_session_stops_future_browser_attempts_and_preserves_profile(tmp_path, monkeypatch):
    context = FakeContext()
    data = tmp_path / 'browser' / 'user_data'
    data.mkdir(parents=True)
    (data / 'keep').write_text('existing session')
    state_dir = tmp_path / 'state'
    monkeypatch.setattr(checker, 'PATHS', SimpleNamespace(state_dir=state_dir, user_data_dir=data))
    monkeypatch.setattr(checker, 'AUTH_BACKOFF_FILE', state_dir / 'auth_backoff.json')
    monkeypatch.setattr(checker, 'browser_profile_needs_recovery', lambda _path: False)
    launches = []
    monkeypatch.setattr(checker, 'init_work_browser', lambda *_a: launches.append(True) or context)
    monkeypatch.setattr(checker, 'detect_captcha', lambda *_a, **_kw: {'found': False})
    monkeypatch.setattr(checker, 'wait_for_authenticated_page', lambda _page: False)
    monkeypatch.setattr(checker, 'explicitly_logged_out', lambda _page: True)
    monkeypatch.setattr(checker, 'safe_close_context', lambda *_a: context.close())

    assert checker.open_logged_in_work_browser(object()) == (False, None, None)
    assert checker.open_logged_in_work_browser(object()) == (False, None, None)
    assert launches == [True]
    assert context.closed
    assert (data / 'keep').read_text() == 'existing session'
    assert (state_dir / 'session_import_required.json').exists()


def test_mid_run_session_loss_stops_dispatch(tmp_path, monkeypatch):
    monkeypatch.setattr(checker, 'PATHS', SimpleNamespace(state_dir=tmp_path))
    monkeypatch.setattr(checker, 'is_logged_in', lambda _page: False)
    monkeypatch.setattr(checker, 'explicitly_logged_out', lambda _page: True)
    with pytest.raises(checker.AuthSessionUnavailable):
        checker.require_work_session(object())
    assert (tmp_path / 'session_import_required.json').exists()


def test_missing_dom_without_logout_is_unknown_not_a_revoked_session(tmp_path, monkeypatch):
    monkeypatch.setattr(checker, 'PATHS', SimpleNamespace(state_dir=tmp_path))
    monkeypatch.setattr(checker, 'is_logged_in', lambda _p: False)
    monkeypatch.setattr(checker, 'explicitly_logged_out', lambda _p: False)
    monkeypatch.setattr(checker, 'detect_captcha', lambda *_a, **_k: {'found': False})
    with pytest.raises(checker.AuthCheckInconclusive):
        checker.require_work_session(object())
    assert not (tmp_path / 'session_import_required.json').exists()


def test_missing_metadata_does_not_block_authenticated_browser(tmp_path, monkeypatch):
    root = tmp_path / 'browser'
    cookies = root / 'Default' / 'Network' / 'Cookies'
    cookies.parent.mkdir(parents=True)
    cookies.write_bytes(b'existing-encrypted-cookies')
    monkeypatch.setattr(checker, 'PATHS', SimpleNamespace(state_dir=tmp_path / 'state', user_data_dir=root))
    monkeypatch.setattr(checker, 'AUTH_BACKOFF_FILE', tmp_path / 'state' / 'auth_backoff.json')
    context = FakeContext()
    monkeypatch.setattr(checker, 'browser_profile_needs_recovery', lambda _path: False)
    monkeypatch.setattr(checker, 'init_work_browser', lambda *_args: context)
    monkeypatch.setattr(checker, 'detect_captcha', lambda *_a, **_kw: {'found': False})
    monkeypatch.setattr(checker, 'wait_for_authenticated_page', lambda _page: True)
    assert checker.open_logged_in_work_browser(object()) == (True, context, context.page)
    assert cookies.read_bytes() == b'existing-encrypted-cookies'
    assert not (root / 'Local State').exists()
    assert not (tmp_path / 'state' / 'session_import_required.json').exists()


def test_ready_targets_for_run_skips_cooldown_before_browser(monkeypatch) -> None:
    monkeypatch.delenv("WORKER_FORCE_SEND_ON_BOOT", raising=False)

    class FakeStore:
        def get_cooldown_status(self, name: str):
            return SimpleNamespace(
                allowed=name == "ready",
                hours_passed=0.25,
                seconds_left=42300,
            )

    history: list[tuple[str, dict]] = []
    monkeypatch.setattr(checker, "store", FakeStore())
    monkeypatch.setattr(checker, "_append_run_history", lambda event, **data: history.append((event, data)))

    summary = checker.RunSummary(profile_name="test", total_targets=2)
    targets = [checker.Target(name="cooldown"), checker.Target(name="ready")]

    ready = checker.ready_targets_for_run(targets, summary)

    assert [target.name for target in ready] == ["ready"]
    assert summary.skipped_count == 1
    assert summary.failed_count == 0
    assert history == [
        (
            "target_result",
            {
                "target": "cooldown",
                "success": False,
                "skipped": True,
                "reason": "cooldown",
                "seconds_left": 42300,
                "preflight": True,
            },
        )
    ]


def test_ready_targets_for_run_bypasses_cooldown_on_boot(monkeypatch) -> None:
    class FakeStore:
        def get_cooldown_status(self, _name: str):
            return SimpleNamespace(
                allowed=False,
                hours_passed=0.25,
                seconds_left=42300,
            )

    history: list[tuple[str, dict]] = []
    monkeypatch.setenv("WORKER_FORCE_SEND_ON_BOOT", "1")
    monkeypatch.setattr(checker, "store", FakeStore())
    monkeypatch.setattr(checker, "_append_run_history", lambda event, **data: history.append((event, data)))

    summary = checker.RunSummary(profile_name="test", total_targets=1)
    targets = [checker.Target(name="cooldown")]

    ready = checker.ready_targets_for_run(targets, summary)

    assert [target.name for target in ready] == ["cooldown"]
    assert summary.skipped_count == 0
    assert history == [
        (
            "target_cooldown_bypass",
            {
                "target": "cooldown",
                "reason": "pc_startup",
                "seconds_left": 42300,
                "preflight": True,
            },
        )
    ]


def test_run_summary_counts_non_cooldown_skip_as_skipped() -> None:
    summary = checker.RunSummary(profile_name="test", total_targets=1)

    summary.add_result(
        target="__run__",
        success=False,
        skipped=True,
        reason="stop_requested",
    )

    assert summary.skipped_count == 1
    assert summary.failed_count == 0
    assert summary.to_dict()["results"][0]["skipped"] is True


def test_final_run_status_preserves_explicit_failure_and_derives_success() -> None:
    summary = checker.RunSummary(profile_name="test", total_targets=1)
    summary.add_result(target="target", success=True)

    assert checker.final_run_status(summary) == "success"
    assert checker.final_run_status(summary, "failed") == "failed"


def test_target_send_guard_blocks_recent_duplicate(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(checker, "TARGET_GUARD_DIR", tmp_path)
    monkeypatch.setattr(checker, "TARGET_SEND_GUARD_SECONDS", 300)

    first_ok, first = checker.acquire_target_send_guard("target")
    second_ok, second = checker.acquire_target_send_guard("target")

    assert first_ok is True
    assert first["target"] == "target"
    assert second_ok is False
    assert second["reason"] == "delivery_unconfirmed"


@pytest.mark.parametrize('saved_pid', [None, -1, os.getpid()])
def test_worker_exit_clears_only_its_own_pid(monkeypatch, saved_pid) -> None:
    changes = []
    released = []
    monkeypatch.setattr(checker, 'control_store', SimpleNamespace(
        load_state=lambda: SimpleNamespace(last_run_pid=saved_pid),
        update_state=lambda **patch: changes.append(patch),
    ))
    monkeypatch.setattr(checker, 'run_lock', SimpleNamespace(release=lambda: released.append(True)))

    checker.release_worker_run()

    assert released == [True]
    assert changes == ([{'last_run_pid': None, 'stop_requested': False}] if saved_pid == os.getpid() else [])


def test_worker_exit_releases_lock_even_if_state_cleanup_fails(monkeypatch) -> None:
    def fail_load():
        raise OSError('state is unavailable')

    released = []
    monkeypatch.setattr(checker, 'control_store', SimpleNamespace(load_state=fail_load))
    monkeypatch.setattr(checker, 'run_lock', SimpleNamespace(release=lambda: released.append(True)))

    with pytest.raises(OSError):
        checker.release_worker_run()

    assert released == [True]
