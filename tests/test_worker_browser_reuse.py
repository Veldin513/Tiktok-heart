from __future__ import annotations

import yara_app.tiktok_checker as checker


class FakePage:
    def __init__(self) -> None:
        self.goto_calls: list[str] = []

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


def test_open_logged_in_work_browser_reuses_authorized_context(monkeypatch) -> None:
    context = FakeContext()

    monkeypatch.setattr(checker, "recover_browser_profile_after_reinstall", lambda _paths: None)
    monkeypatch.setattr(checker, "init_work_browser", lambda _playwright, _user_data_dir: context)
    monkeypatch.setattr(checker, "apply_stealth_script", lambda _page: None)
    monkeypatch.setattr(checker, "handle_captcha", lambda *_args: None)
    monkeypatch.setattr(checker, "is_logged_in", lambda _page: True)
    monkeypatch.setattr(checker, "clear_auth_backoff", lambda _path: None)

    logged_in, returned_context, returned_page = checker.open_logged_in_work_browser(object())

    assert logged_in is True
    assert returned_context is context
    assert returned_page is context.page
    assert context.closed is False
    assert context.page.goto_calls == ["https://www.tiktok.com/messages"]


def test_ensure_logged_in_closes_reused_context(monkeypatch) -> None:
    context = FakeContext()
    monkeypatch.setattr(checker, "open_logged_in_work_browser", lambda _playwright: (True, context, context.page))

    assert checker.ensure_logged_in(object()) is True
    assert context.closed is True
