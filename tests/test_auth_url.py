from types import SimpleNamespace

import pytest

from yara_app.auth_flow import is_logged_in


@pytest.mark.parametrize('url,expected', [
    ('https://www.tiktok.com/messages', True),
    ('https://www.tiktok.com/login?redirect_url=https://www.tiktok.com/messages', False),
    ('https://example.test/?url=https://www.tiktok.com/messages', False),
])
def test_login_redirect_url_is_not_proof_of_authentication(url, expected):
    page = SimpleNamespace(url=url, locator=lambda _selector: SimpleNamespace(is_visible=lambda: False))
    assert is_logged_in(page) is expected
