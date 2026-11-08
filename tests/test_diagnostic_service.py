from __future__ import annotations

import json
import shutil
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from app_shell.server import AppShellHandler
from yara_app.auth_flow import record_session_validation
from yara_app.diagnostic_service import DiagnosticService
from yara_app.fileio import atomic_write_text, read_json
from yara_app.project_adapter import ProjectAdapter


@pytest.fixture
def diagnostic_app(tmp_path, monkeypatch):
    adapter = ProjectAdapter(tmp_path)
    adapter.ensure_runtime_files()
    for key in ('alpha', 'beta'):
        adapter.create_profile(key)
        adapter.add_target(key, key + '_friend', '@' + key + '_friend')
        root = adapter._bot_profile_root(key)
        cookies = root / 'browser/user_data/Default/Network/Cookies'
        cookies.parent.mkdir(parents=True, exist_ok=True)
        cookies.touch()
        record_session_validation(root / 'state', valid=True, source='test')
    adapter.update_control_state({'active_profile': 'alpha', 'paused': False, 'dry_run': False, 'stop_requested': False})
    adapter.message_pool_path.write_text('hello\n', encoding='utf-8')
    adapter.main_script_path.parent.mkdir(parents=True, exist_ok=True)
    adapter.main_script_path.write_text('# never run')
    monkeypatch.setattr(adapter, 'get_worker_schedule_status_cached', lambda **_kw: {
        'available': True, 'installed': True, 'enabled': True, 'at_logon': True, 'every_12_hours': True})
    monkeypatch.setattr(adapter, 'start_profile_worker', lambda *_a, **_kw: pytest.fail('diagnostics started worker'))
    monkeypatch.setattr(adapter, 'resume_worker_queue', lambda: pytest.fail('diagnostics started queue'))
    return adapter


def checks(report):
    return {item['key']: item for item in report['checks']}


def test_report_uses_selected_profile_own_text_and_does_not_write_settings(diagnostic_app, monkeypatch):
    adapter = diagnostic_app
    raw = read_json(adapter.profiles_path)
    raw['beta'] = {'targets': raw['beta'], 'message_settings': {'source': 'personal', 'messages': ['personal']}}
    atomic_write_text(adapter.profiles_path, json.dumps(raw))
    adapter.message_pool_path.write_text('')
    before = {p: p.read_bytes() for p in adapter.base_dir.rglob('*') if p.is_file()}
    monkeypatch.setattr(adapter, 'ensure_runtime_files', lambda: pytest.fail('diagnostics created defaults'))
    service = DiagnosticService(adapter)
    beta, alpha = service.collect('beta'), service.collect('alpha')
    assert beta['profile_key'] == 'beta' and beta['status'] == 'ok'
    assert checks(beta)['messages']['state'] == 'Сообщений: 1'
    assert checks(alpha)['messages']['blocking']
    assert 'score' not in beta and checks(beta)['schedule']['details'].endswith('Успешный запуск этим ещё не доказан.')
    assert before == {p: p.read_bytes() for p in adapter.base_dir.rglob('*') if p.is_file()}


@pytest.mark.parametrize('valid,expected', [(None, 'unknown'), (False, 'warning'), (True, 'ok')])
def test_login_never_infers_current_session_from_cookie_file(diagnostic_app, valid, expected):
    adapter = diagnostic_app
    record_session_validation(adapter._bot_profile_root('alpha') / 'state', valid=valid, source='test')
    report = DiagnosticService(adapter).collect('alpha')
    assert checks(report)['login']['status'] == expected
    if valid is not True:
        assert report['status'] != 'ok'
    else:
        assert 'не гарантирует' in checks(report)['login']['details']


def test_independent_failures_are_reported_and_old_log_errors_do_not_block(diagnostic_app):
    adapter = diagnostic_app
    adapter.worker_stdout_path.parent.mkdir(parents=True, exist_ok=True)
    adapter.worker_stdout_path.write_text('[ERROR] old failure\n' * 6)
    service = DiagnosticService(adapter)
    assert service.collect('alpha')['status'] == 'ok'
    adapter.worker_queue().path.write_text('{broken')
    adapter.telegram_config_path.write_text('{broken')
    report = service.collect('alpha')
    items = checks(report)
    assert items['queue']['status'] == items['telegram']['status'] == 'unknown'
    assert items['runtime']['status'] == items['messages']['status'] == 'ok'
    assert items['logs']['status'] == 'info'
    assert report['unknown'] == 2 and report['status'] != 'ok'
    assert adapter.worker_queue().path.read_text() == '{broken'


def test_self_test_and_audit_exclude_secrets_and_preserve_damaged_history(diagnostic_app, monkeypatch):
    adapter = diagnostic_app
    calls = []
    secret = 'PRIVATE_SESSION_VALUE'
    def probe(**kwargs):
        calls.append(kwargs)
        return {'ok': True, 'exit_code': 0, 'stdout': secret, 'stderr': secret}
    monkeypatch.setattr(adapter, 'worker_starter_self_test', probe)
    def broken_browser(_key):
        raise RuntimeError('sessionid=' + secret)
    monkeypatch.setattr(adapter, 'browser_profile_live_status', broken_browser)
    service = DiagnosticService(adapter)
    report = service.run('beta', self_test=True)
    assert calls == [{'no_browser': True, 'diagnostic_only': True, 'profile_key': 'beta'}]
    assert report['log_saved'] and secret not in json.dumps(report)
    assert service.history()[0]['profile_key'] == 'beta'
    path = adapter.common_logs_dir / 'diagnostics_history.json'
    assert secret not in path.read_text(encoding='utf-8')
    path.write_text('{broken')
    report = service.run('alpha')
    assert not report['log_saved'] and path.read_text() == '{broken'


def test_real_readonly_self_test_uses_existing_launcher_without_changing_profiles(diagnostic_app, monkeypatch):
    adapter = diagnostic_app
    original = Path(__file__).resolve().parents[1]
    monkeypatch.setenv('PYTHONPATH', str(original))
    starter = adapter.base_dir / 'scripts/start_worker_once.py'
    starter.parent.mkdir()
    shutil.copy2(original / 'scripts/start_worker_once.py', starter)
    before = {p: p.read_bytes() for p in (adapter.base_dir / 'profiles').rglob('*') if p.is_file()}
    config = adapter.control_state_path.read_bytes()
    result = adapter.worker_starter_self_test(no_browser=True, diagnostic_only=True, profile_key='beta')
    assert result['ok'], (result.get('report'), result.get('stderr'))
    assert result['report']['profile_key'] == 'beta' and result['safe_mode'] == 'no_send'
    assert config == adapter.control_state_path.read_bytes()
    assert before == {p: p.read_bytes() for p in (adapter.base_dir / 'profiles').rglob('*') if p.is_file()}
    assert not (adapter.base_dir / 'yara_app/__pycache__').exists()


def test_compact_home_does_not_load_history_or_full_message_text(diagnostic_app, monkeypatch):
    adapter = diagnostic_app
    monkeypatch.setattr(adapter, 'get_run_history_snapshot', lambda *_a, **_kw: pytest.fail('home loaded full history'))
    monkeypatch.setattr(adapter, 'get_profile_messages', lambda *_a, **_kw: pytest.fail('home loaded all text'))
    data = adapter.get_profile_overview('beta', compact=True)
    assert data['profile_key'] == 'beta' and not data['scheduled']
    assert data['messages'] == data['history'] == {}


@pytest.mark.parametrize('theme,width', [('light', 980), ('dark', 1280)])
def test_diagnostic_page_runs_checks_and_self_test_and_opens_history(diagnostic_app, monkeypatch, theme, width):
    from playwright.sync_api import sync_playwright

    adapter = diagnostic_app
    monkeypatch.setattr(adapter, 'worker_starter_self_test', lambda **_kw: {'ok': True, 'exit_code': 0})
    monkeypatch.setattr(AppShellHandler, 'adapter', adapter)
    AppShellHandler._clear_diagnostics_cache()
    server = ThreadingHTTPServer(('127.0.0.1', 0), AppShellHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as runtime:
            browser = runtime.chromium.launch(channel='chrome', headless=True)
            page = browser.new_page(viewport={'width': width, 'height': 1000})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            try:
                page.add_init_script(f'localStorage.setItem("tiktok-heart-theme", "{theme}")')
                page.goto(f'http://127.0.0.1:{server.server_port}/')
                page.locator('nav [data-view="diagnostics"]').click()
                page.locator('#diagnostic-profile option[value="beta"]').wait_for(state='attached')
                page.locator('#diagnostic-profile').select_option('beta')
                page.locator('#diagnostic-meta').get_by_text('beta', exact=False).wait_for()
                page.get_by_role('button', name='Проверить приложение', exact=True).click()
                page.locator('#diagnostic-history [data-check-history]').first.wait_for()
                assert DiagnosticService(adapter).history()[0]['profile_key'] == 'beta'
                page.get_by_role('button', name='Самотест запуска', exact=True).click()
                page.locator('#diagnostic-test-result').get_by_text('Самотест пройден', exact=False).wait_for()
                assert DiagnosticService(adapter).history()[0]['self_test']['ok']
                page.locator('#diagnostic-history [data-check-history]').first.click()
                page.locator('#diagnostic-summary').get_by_text('Сохранённая проверка.', exact=False).wait_for()
                assert page.locator('html').get_attribute('data-ui-mode') == 'simple'
                assert page.locator('.health-score').count() == 0
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                assert not errors
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)
        AppShellHandler._clear_diagnostics_cache()
