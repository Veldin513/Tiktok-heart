"""Exercise the real HTML/JS with synthetic local data; no TikTok or Telegram calls."""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

WEB = Path(__file__).resolve().parents[1] / 'app_shell/web'
pytestmark = pytest.mark.skipif(not (WEB.parents[1] / '.playwright-browsers').exists(), reason='local Chromium not installed')


def test_home_profile_selection_and_personal_text_do_not_change_schedule(tmp_path):
    from playwright.sync_api import sync_playwright

    calls = []
    errors = []
    profiles = [{'key': key, 'label': key, 'active': key == 'alpha', 'enabled': True,
        'targets': [], 'target_count': 0} for key in ['alpha', 'beta']]
    diag = {'state': {'active_profile': 'alpha'}, 'profiles': {'items': profiles, 'total': 2},
        'worker_queue': {'waiting': [], 'current': None}, 'worker': {}, 'telegram_bot': {},
        'telegram_ready': False, 'browser_profile': {}, 'health': {'score': 100, 'issues': []},
        'worker_schedule': {}, 'message_pool_details': {}, 'run': {}, 'app': {}}

    def route(request):
        parsed = urlparse(request.request.url)
        path = parsed.path
        if path.startswith('/api/'):
            payload = json.loads(request.request.post_data or '{}')
            calls.append((request.request.method, path, payload))
            key = (parse_qs(parsed.query).get('profile_key') or [''])[0]
            if path in {'/api/live', '/api/diagnostics'}:
                data = diag
            elif path == '/api/profile-overview':
                data = {'label': key, 'readiness': {'label': 'Можно запустить', 'kind': 'ok'}, 'scheduled': key == 'alpha',
                        'queue': {'target_count': 1, 'ready_count': int(key == 'beta'),
                                  'cooldown_count': int(key == 'alpha'), 'next_send_in_s': 7200},
                        'run': {'completed_at': 1793556099, 'reason': 'chat_blocked',
                                'outcome': {'code': 'failed', 'label': f'{key}: отправка не выполнена', 'kind': 'bad'}}}
            elif path == '/api/profile-messages':
                data = {'text': payload.get('text', 'Own text'), 'revision': 'revision', 'source': 'personal'}
            elif path == '/api/message-pool':
                data = {'text': 'Shared text', 'stats': {}}
            else:
                data = {}
            request.fulfill(json={'ok': True, 'data': data})
            return
        target = WEB / ('index.html' if path == '/' else path.lstrip('/'))
        if target.is_file() and target.resolve().is_relative_to(WEB):
            mime = {'.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.png': 'image/png'}[target.suffix]
            request.fulfill(body=target.read_bytes(), content_type=mime)
        else:
            request.fulfill(status=404)

    with sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': 1280, 'height': 820})
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.route('**/*', route)
        page.goto('http://heart.test/')
        page.locator('#launch-profile option[value="beta"]').wait_for(state='attached')
        page.locator('#home-last-meta').get_by_text('Когда:', exact=False).wait_for()
        assert 'Чат не удалось открыть' in page.locator('#home-last-meta').inner_text()
        assert page.locator('#home-current-state').inner_text() == 'Сейчас отправка не выполняется.'
        page.select_option('#launch-profile', 'beta')
        page.wait_for_function("document.querySelector('#home-last-result').textContent.includes('beta')")
        assert page.locator('#launch-selected').is_enabled()
        assert page.locator('#queue-value').inner_text() == 'Можно отправить'
        assert 'alpha' in page.locator('#home-schedule-profile').inner_text()
        assert page.locator('#view-overview #open-checks').count() == 1
        assert 'Профилей: 2' in page.locator('.home-system-summary').inner_text()
        page.click('[data-view="messages"]')
        page.select_option('#message-scope', 'beta')
        page.wait_for_function("document.querySelector('#message-editor').value === 'Own text'")
        page.fill('#message-editor', 'My text')
        page.click('#message-save')
        page.wait_for_function("document.querySelector('#message-editor-state').textContent === 'Сохранено'")
        writes = [(path, payload) for method, path, payload in calls if method == 'POST']
        assert writes == [('/api/profile-messages', {'text': 'My text', 'profile_key': 'beta',
            'revision': 'revision', 'backup': True, 'normalize': False})]
        assert diag['state']['active_profile'] == 'alpha'
        page.click('[data-view="overview"]')
        for theme in ['light', 'dark']:
            if page.locator('html').get_attribute('data-theme') != theme:
                page.click('#theme-toggle')
            for width in [980, 1280]:
                page.set_viewport_size({'width': width, 'height': 820})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                assert page.locator('#launch-selected').is_visible()
                assert page.locator('#home-telegram').is_visible()
                assert page.locator('#view-overview [data-action="stop_all"]').is_visible()
                assert page.locator('#view-overview details').count() == 0
                assert page.locator('#view-overview [data-action="stop_all"]').bounding_box()['y'] < 500
                assert page.locator('#queue-value').bounding_box()['y'] < 720
            page.screenshot(path=str(tmp_path / f'home-{theme}.png'), full_page=True, animations='disabled')
        assert not errors
        browser.close()
