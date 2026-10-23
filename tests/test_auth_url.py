from types import SimpleNamespace

import pytest

from yara_app.auth_flow import is_logged_in


@pytest.mark.parametrize('url,visible,login,expected', [
    ('https://www.tiktok.com/messages', True, False, True),
    ('https://www.tiktok.com/messages', False, False, False),
    ('https://www.tiktok.com/messages', True, True, False),
    ('https://www.tiktok.com/login?redirect_url=https://www.tiktok.com/messages', True, False, False),
    ('https://example.test/?url=https://www.tiktok.com/messages', True, False, False),
])
def test_login_requires_real_host_and_visible_authenticated_interface(url, visible, login, expected):
    def locator(selector):
        return SimpleNamespace(count=lambda: int(login), is_visible=lambda: visible)
    page = SimpleNamespace(url=url, locator=locator)
    assert is_logged_in(page) is expected
