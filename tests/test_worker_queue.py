from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from yara_app.fileio import file_lock
from yara_app.project_adapter import ProjectAdapter, WorkerStatus
from yara_app.worker_queue import LoginRequired, WorkerQueue


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    adapter = ProjectAdapter(tmp_path)
    adapter.ensure_runtime_files()
    adapter.profiles_path.write_text(json.dumps({key: [{'name': key + '_target', 'url': '@' + key + '_target'}]
                                                for key in ['alpha', 'beta', 'gamma']}))
    adapter.update_control_state({'active_profile': 'alpha', 'paused': False, 'stop_requested': False})
    adapter.telegram_config_path.write_text(json.dumps({'allowed_chat_ids': [100, 200], 'admin_chat_ids': [100],
                                                       'profile_owners': {'beta': [200]}}))
    state = {'running': False, 'pid': None, 'profile': None, 'starts': [], 'reports': []}
    monkeypatch.delenv('WORKER_FORCE_SEND_ON_BOOT', raising=False)
    monkeypatch.setattr(adapter, 'get_worker_status', lambda: WorkerStatus(running=state['running'], pid=state['pid']))
    monkeypatch.setattr(adapter, '_running_worker_profile', lambda _s: state['profile'])
    def start(profile, **options):
        assert not state['running'], 'two workers launched together'
        state.update(running=True, pid=1000 + len(state['starts']), profile=profile)
        state['starts'].append((profile, options))
        return adapter.get_worker_status()
    monkeypatch.setattr(adapter, '_start_worker_profile', start)
    monkeypatch.setattr(WorkerQueue, 'kick', lambda _self: True)
    monkeypatch.setattr(WorkerQueue, 'wait_for_process', staticmethod(lambda _pid: state.update(running=False, pid=None)))
    monkeypatch.setattr(WorkerQueue, '_report_failed', lambda _self, job, text: state['reports'].append((job['profile'], text)))
    return adapter, state


def enqueue(adapter, profile, **options):
    profile = next(p for p in adapter.get_profiles() if p.key == profile)
    with file_lock(adapter.control_dir / 'worker_launch'):
        return adapter.worker_queue().enqueue_locked(profile, **options)


def test_fifo_duplicate_and_manual_permission_survive_reload(runtime):
    adapter, state = runtime
    adapter.start_profile_worker('alpha')
    assert adapter.start_profile_worker('beta', requester_id=200).queue_position == 1
    assert adapter.start_profile_worker('gamma', requester_id=100).queue_position == 2
    assert adapter.start_profile_worker('beta', ignore_cooldown=True, requester_id=200).queue_position == 1
    assert not adapter.start_profile_worker('alpha').queued
    assert len(adapter.worker_queue().read()['waiting']) == 2
    # A new dispatcher reads persisted jobs, not the old object's memory.
    WorkerQueue(adapter).run()
    assert [p for p, _ in state['starts']] == ['alpha', 'beta', 'gamma']
    assert state['starts'][1][1] == {'ignore_cooldown': True, 'boot_force': False}
    assert state['starts'][2][1] == {'ignore_cooldown': False, 'boot_force': False}
    assert adapter.get_control_state()['active_profile'] == 'alpha'
    assert not adapter.worker_queue().read()['waiting']
    assert not adapter.worker_queue().read()['current']


def test_boot_force_is_saved_per_job_only(runtime, monkeypatch):
    adapter, state = runtime
    monkeypatch.setenv('WORKER_FORCE_SEND_ON_BOOT', '1')
    enqueue(adapter, 'alpha')
    monkeypatch.delenv('WORKER_FORCE_SEND_ON_BOOT')
    enqueue(adapter, 'beta')
    WorkerQueue(adapter).run()
    assert [options['boot_force'] for _, options in state['starts']] == [True, False]


def test_queued_dry_run_cannot_become_live_send(runtime):
    adapter, state = runtime
    adapter.update_control_state({'dry_run': True})
    enqueue(adapter, 'alpha')
    adapter.update_control_state({'dry_run': False})
    WorkerQueue(adapter).run()
    assert not state['starts']
    assert len(state['reports']) == 1


@pytest.mark.parametrize('change', ['revoked', 'recipient', 'disabled', 'deleted', 'login', 'no_targets'])
def test_invalid_job_is_reported_and_next_profile_starts(runtime, change):
    adapter, state = runtime
    enqueue(adapter, 'beta', requester=200)
    enqueue(adapter, 'gamma')
    if change == 'revoked':
        config = json.loads(adapter.telegram_config_path.read_text())
        config['allowed_chat_ids'] = [100]
        adapter.telegram_config_path.write_text(json.dumps(config))
    elif change == 'login':
        folder = adapter._get_profile_state_dir('beta')
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'session_import_required.json').write_text('{}')
    else:
        profiles = json.loads(adapter.profiles_path.read_text())
        if change == 'deleted':
            del profiles['beta']
        elif change == 'recipient':
            profiles['beta'][0]['url'] = '@changed_target'
        elif change == 'no_targets':
            profiles['beta'] = []
        else:
            profiles['beta'] = {'enabled': False, 'targets': profiles['beta']}
        adapter.profiles_path.write_text(json.dumps(profiles))
    WorkerQueue(adapter).run()
    assert [p for p, _ in state['starts']] == ['gamma']
    assert len(state['reports']) == 1 and state['reports'][0][0] == 'beta'
    data = adapter.worker_queue().read()
    if change == 'login':
        assert [j['profile'] for j in data['awaiting_login']] == ['beta']
        assert not any(j['profile'] == 'beta' for j in data['history'])
    else:
        assert data['history'][0]['status'] == 'failed'


def hold_for_login(adapter, *, requester=200):
    enqueue(adapter, 'beta', requester=requester)
    root = adapter._bot_profile_root('beta')
    root.joinpath('state').mkdir(parents=True, exist_ok=True)
    marker = root / 'state' / 'session_import_required.json'
    marker.write_text('{}')
    WorkerQueue(adapter).run()
    return marker


@pytest.mark.parametrize('blocker', ['expired', 'manual', 'invalid'])
def test_login_wait_survives_reload_and_never_blocks_or_retries(runtime, monkeypatch, blocker):
    from yara_app.auth_flow import record_session_validation
    from yara_app.native_login import LOGIN_PENDING

    adapter, state = runtime
    enqueue(adapter, 'beta', requester=200)
    enqueue(adapter, 'gamma')
    root = adapter._bot_profile_root('beta')
    if blocker == 'invalid':
        record_session_validation(root / 'state', valid=False, source='test')
    else:
        marker = root / ('browser' if blocker == 'manual' else 'state') / (LOGIN_PENDING if blocker == 'manual' else 'session_import_required.json')
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text('{}')
    WorkerQueue(adapter).run()
    assert [p for p, _ in state['starts']] == ['gamma']
    assert WorkerQueue(adapter).snapshot()['awaiting_login'][0]['can_resume'] is False
    WorkerQueue(adapter).run()
    assert len(state['reports']) == 1  # Reading/restarting does not notify repeatedly.
    # Successful login alone must not send anything. The user resumes explicitly.
    if blocker != 'invalid':
        marker.unlink()
    record_session_validation(root / 'state', valid=True, source='test')
    monkeypatch.setattr(adapter, 'profile_login_confirmed', lambda key: key == 'beta')
    WorkerQueue(adapter).run()
    assert len(state['starts']) == 1
    held_id = WorkerQueue(adapter).read()['awaiting_login'][0]['id']
    result = adapter.start_profile_worker('beta', requester_id=200)
    assert result.queued and result.queue_position == 1
    assert WorkerQueue(adapter).read()['waiting'][0]['id'] == held_id
    assert not WorkerQueue(adapter).read()['awaiting_login']
    WorkerQueue(adapter).run()
    assert [p for p, _ in state['starts']] == ['gamma', 'beta']


def test_resume_requires_verified_login_and_preserves_job(runtime, monkeypatch):
    adapter, state = runtime
    marker = hold_for_login(adapter)
    before = WorkerQueue(adapter).read()['awaiting_login'][0]
    with pytest.raises(LoginRequired):
        adapter.start_profile_worker('beta', requester_id=200)
    marker.unlink()
    with pytest.raises(LoginRequired):
        adapter.start_profile_worker('beta', requester_id=200)
    assert WorkerQueue(adapter).read()['awaiting_login'][0] == before
    assert not state['starts']


@pytest.mark.parametrize('change', ['revoked', 'recipient', 'disabled', 'deleted', 'mode'])
def test_login_resume_revalidates_original_request(runtime, monkeypatch, change):
    adapter, state = runtime
    marker = hold_for_login(adapter)
    marker.unlink()
    monkeypatch.setattr(adapter, 'profile_login_confirmed', lambda _key: True)
    if change == 'revoked':
        config = json.loads(adapter.telegram_config_path.read_text())
        config['profile_owners'] = {}
        adapter.telegram_config_path.write_text(json.dumps(config))
    elif change == 'mode':
        adapter.update_control_state({'dry_run': True})
    else:
        profiles = json.loads(adapter.profiles_path.read_text())
        if change == 'deleted':
            del profiles['beta']
        elif change == 'disabled':
            profiles['beta'] = {'enabled': False, 'targets': profiles['beta']}
        else:
            profiles['beta'][0]['url'] = '@someone_else'
        adapter.profiles_path.write_text(json.dumps(profiles))
    # Even an admin's new click must not replace the original revoked owner.
    with pytest.raises((ValueError, PermissionError)):
        adapter.start_profile_worker('beta', requester_id=100)
    assert not state['starts']
    assert WorkerQueue(adapter).read()['awaiting_login'][0]['requester'] == 200
    assert WorkerQueue(adapter).cancel('beta') == 1


def test_duplicate_click_does_not_replace_original_requester(runtime):
    adapter, state = runtime
    enqueue(adapter, 'beta', requester=200)
    enqueue(adapter, 'beta', ignore_cooldown=True, requester=100)
    item = WorkerQueue(adapter).read()['waiting'][0]
    assert item['requester'] == 200 and item['ignore_cooldown'] is False
    config = json.loads(adapter.telegram_config_path.read_text())
    config['profile_owners'] = {}
    adapter.telegram_config_path.write_text(json.dumps(config))
    WorkerQueue(adapter).run()
    assert not state['starts']


def test_resume_drops_old_boot_and_manual_exceptions(runtime, monkeypatch):
    adapter, state = runtime
    monkeypatch.setenv('WORKER_FORCE_SEND_ON_BOOT', '1')
    enqueue(adapter, 'beta', requester=200, ignore_cooldown=True)
    marker = hold_for_login(adapter)
    marker.unlink()
    monkeypatch.setattr(adapter, 'profile_login_confirmed', lambda _key: True)
    adapter.start_profile_worker('beta', requester_id=200)
    WorkerQueue(adapter).run()
    assert state['starts'] == [('beta', {'ignore_cooldown': False, 'boot_force': False})]


def test_error_after_launch_attempt_is_never_treated_as_safe_login_wait(runtime, monkeypatch):
    adapter, _state = runtime
    enqueue(adapter, 'beta')
    def uncertain_start(*_args, **_kwargs):
        raise LoginRequired('A process might already have started')
    monkeypatch.setattr(adapter, '_start_worker_profile', uncertain_start)
    WorkerQueue(adapter).run()
    data = WorkerQueue(adapter).read()
    assert not data['awaiting_login'] and not data['waiting']
    assert data['history'][0]['status'] == 'failed'


def test_stop_all_also_cancels_jobs_waiting_for_login(runtime, monkeypatch):
    adapter, state = runtime
    hold_for_login(adapter)
    monkeypatch.setattr(adapter, 'stop_worker', lambda: WorkerStatus(False, None))
    monkeypatch.setattr(adapter, 'stop_telegram_bot', lambda: WorkerStatus(False, None))
    adapter.stop_all()
    data = WorkerQueue(adapter).read()
    assert not data['awaiting_login'] and not state['starts']
    assert data['history'][0]['status'] == 'cancelled'


def test_held_only_queue_does_not_start_idle_dispatcher(tmp_path, monkeypatch):
    adapter = ProjectAdapter(tmp_path)
    adapter.ensure_runtime_files()
    adapter.create_profile('beta')
    enqueue(adapter, 'beta')
    queue = WorkerQueue(adapter)
    data = queue.read()
    data['awaiting_login'].append(data['waiting'].pop())
    queue.save(data)
    monkeypatch.setattr('yara_app.worker_queue.subprocess.Popen', lambda *_a, **_kw: pytest.fail('idle dispatcher started'))
    assert queue.kick()


def test_legacy_queue_is_read_without_overwriting_or_losing_fields(runtime):
    adapter, _state = runtime
    queue = WorkerQueue(adapter)
    old = {'waiting': [], 'current': None, 'history': [], 'extension': {'keep': True}}
    original = json.dumps(old)
    queue.path.write_text(original)
    data = queue.read()
    assert data['awaiting_login'] == [] and queue.path.read_text() == original
    queue.save(data)
    assert queue.read()['extension'] == {'keep': True}


def test_cancellation_cannot_stop_other_profile(runtime, monkeypatch):
    adapter, _state = runtime
    adapter.start_profile_worker('alpha')
    adapter.start_profile_worker('beta', requester_id=200)
    adapter.start_profile_worker('gamma', requester_id=100)
    monkeypatch.setattr(adapter, 'stop_worker', lambda: pytest.fail('foreign worker stopped'))
    assert not adapter.stop_profile_worker('beta').running
    assert [j['profile'] for j in adapter.worker_queue().read()['waiting']] == ['gamma']
    assert adapter.worker_queue().cancel() == 1
    assert adapter.get_worker_status().running


def test_stop_all_cancels_waiting_work_before_stopping_current(runtime, monkeypatch):
    adapter, _state = runtime
    enqueue(adapter, 'beta')
    stopped = []
    def stop():
        assert not adapter.worker_queue().read()['waiting']
        stopped.append(True)
        return WorkerStatus(running=False, pid=None)
    monkeypatch.setattr(adapter, 'stop_worker', stop)
    monkeypatch.setattr(adapter, 'stop_telegram_bot', stop)
    adapter.stop_all()
    assert len(stopped) == 2


@pytest.mark.parametrize('flag', ['paused', 'stop_requested'])
def test_pause_and_stop_hold_waiting_jobs(runtime, flag):
    adapter, state = runtime
    enqueue(adapter, 'beta')
    adapter.update_control_state({flag: True})
    WorkerQueue(adapter).run()
    assert not state['starts']
    assert len(adapter.worker_queue().read()['waiting']) == 1
    adapter.update_control_state({flag: False})
    WorkerQueue(adapter).run()
    assert [p for p, _ in state['starts']] == ['beta']


def test_crashed_current_is_never_replayed(runtime):
    adapter, state = runtime
    enqueue(adapter, 'alpha')
    enqueue(adapter, 'beta')
    queue = adapter.worker_queue()
    data = queue.read()
    data['current'] = data['waiting'].pop(0)
    queue.save(data)
    WorkerQueue(adapter).run()
    assert [p for p, _ in state['starts']] == ['beta']
    assert state['reports'][0][0] == 'alpha'
    assert queue.read()['history'][0]['status'] == 'interrupted'


@pytest.mark.parametrize('bad', ['{broken', '{"waiting":[],"history":[],"current":{}}',
                                 '{"waiting":[{}],"history":[],"current":null}',
                                 '{"waiting":[],"history":[],"awaiting_login":{}}',
                                 '{"waiting":[],"history":[],"awaiting_login":[{}]}'])
def test_corrupt_queue_fails_closed_without_overwrite(runtime, bad):
    adapter, state = runtime
    queue = adapter.worker_queue()
    queue.path.write_text(bad)
    assert queue.snapshot()['error']
    with pytest.raises(ValueError):
        queue.run()
    assert queue.path.read_text() == bad
    assert not state['starts']


def test_second_dispatcher_cannot_start_while_first_waits(runtime, monkeypatch):
    adapter, state = runtime
    enqueue(adapter, 'alpha')
    enqueue(adapter, 'beta')
    entered = threading.Event()
    release = threading.Event()
    errors = []
    def wait(_pid):
        entered.set()
        assert release.wait(5)
        state.update(running=False, pid=None)
    monkeypatch.setattr(WorkerQueue, 'wait_for_process', staticmethod(wait))
    def run():
        try:
            WorkerQueue(adapter).run()
        except Exception as exc:
            errors.append(exc)
    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert entered.wait(5)
        WorkerQueue(adapter).run()
        assert [p for p, _ in state['starts']] == ['alpha']
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive() and not errors
    assert [p for p, _ in state['starts']] == ['alpha', 'beta']


def test_windows_runtime_child_is_waited_before_next_job(runtime, monkeypatch):
    adapter, state = runtime
    enqueue(adapter, 'alpha')
    enqueue(adapter, 'beta')
    waited = []
    def wait(pid):
        waited.append(pid)
        if len(waited) == 1:
            state['pid'] = 9999  # Launcher exits; runtime child remains.
        else:
            state.update(running=False, pid=None)
    monkeypatch.setattr(WorkerQueue, 'wait_for_process', staticmethod(wait))
    WorkerQueue(adapter).run()
    assert waited == [1000, 9999, 1001]
    assert [p for p, _ in state['starts']] == ['alpha', 'beta']


def test_idle_dispatcher_releases_lease_before_enqueue_lock(runtime, monkeypatch):
    adapter, _state = runtime
    queue = adapter.worker_queue()
    checked = []
    # Test the handoff at the exact empty-queue decision, without timing sleeps.
    original_read = queue.read
    def read():
        result = original_read()
        checked.append(True)
        return result
    monkeypatch.setattr(queue, 'read', read)
    queue.run()
    assert checked
    # OS lease must be available immediately for an incoming enqueue/kick.
    with file_lock(queue.dispatch_lock, timeout=0):
        pass


def test_wait_blocks_until_real_dummy_process_exits():
    child = subprocess.Popen([sys.executable, '-c', 'import sys; sys.stdin.read()'], stdin=subprocess.PIPE)
    completed = threading.Event()
    waiter = threading.Thread(target=lambda: (WorkerQueue.wait_for_process(child.pid), completed.set()))
    waiter.start()
    try:
        assert not completed.wait(0.1)
        child.stdin.close()
        assert completed.wait(10)
    finally:
        child.wait(timeout=10)
        waiter.join(10)


def test_real_worker_launch_receives_profile_and_boot_option(runtime, monkeypatch):
    adapter, _state = runtime
    adapter.main_script_path.parent.mkdir(parents=True, exist_ok=True)
    adapter.main_script_path.write_text('# dummy; never run')
    calls = []
    monkeypatch.setattr(adapter, '_launch_python_process', lambda **kw: calls.append(kw) or WorkerStatus(running=True, pid=123))
    ProjectAdapter._start_worker_profile(adapter, 'beta', ignore_cooldown=True, boot_force=True)
    assert calls[0]['extra_args'] == ['beta', '--telegram-manual', '--startup-cooldown']
    assert adapter.get_control_state()['active_profile'] == 'alpha'


def test_dispatcher_runs_real_dummy_processes_sequentially(tmp_path):
    adapter = ProjectAdapter(tmp_path)
    adapter.ensure_runtime_files()
    adapter.profiles_path.write_text(json.dumps({key: [{'name': key, 'url': '@' + key}]
                                                for key in ['alpha', 'beta']}))
    adapter.update_control_state({'active_profile': 'alpha', 'paused': False})
    adapter.main_script_path.parent.mkdir(parents=True, exist_ok=True)
    adapter.main_script_path.write_text(
        'import sys,time\nfrom pathlib import Path\n'
        'p=Path.cwd()/"dummy_events.txt"\n'
        'with p.open("a") as f: f.write(sys.argv[1]+":start\\n")\n'
        'time.sleep(0.3)\n'
        'with p.open("a") as f: f.write(sys.argv[1]+":end\\n")\n')
    enqueue(adapter, 'alpha')
    enqueue(adapter, 'beta')
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([sys.executable, str(root / 'scripts/worker_queue_runner.py'), '--base-dir', str(tmp_path)], cwd=root,
                            env={**os.environ, 'TG_DISABLE_NOTIFICATIONS': '1', 'YARA_BASE_DIR': str(tmp_path)},
                            capture_output=True, timeout=30,
                            **adapter._windows_subprocess_kwargs(hide_window=True))
    assert result.returncode == 0, result.stderr.decode(errors='replace')
    assert (tmp_path / 'dummy_events.txt').read_text().splitlines() == [
        'alpha:start', 'alpha:end', 'beta:start', 'beta:end']
    assert not adapter.worker_queue().read()['waiting']
    assert not adapter.get_worker_status().running


def test_kick_is_hidden_and_does_not_leak_boot_exception(tmp_path, monkeypatch):
    adapter = ProjectAdapter(tmp_path)
    adapter.ensure_runtime_files()
    adapter.create_profile('alpha')
    monkeypatch.setenv('WORKER_FORCE_SEND_ON_BOOT', '1')
    enqueue(adapter, 'alpha')
    helper = tmp_path / 'scripts/worker_queue_runner.py'
    helper.parent.mkdir(parents=True)
    helper.touch()
    launches = []
    monkeypatch.setattr('yara_app.worker_queue.subprocess.Popen', lambda args, **kw: launches.append((args, kw)))
    assert adapter.worker_queue().kick()
    assert len(launches) == 1
    args, options = launches[0]
    assert args[-2:] == ['--base-dir', str(tmp_path)]
    assert 'WORKER_FORCE_SEND_ON_BOOT' not in options['env']
    assert options['stdin'] == subprocess.DEVNULL
    if os.name == 'nt':
        assert args[0].lower().endswith('pythonw.exe')
        assert options['creationflags'] & subprocess.CREATE_NO_WINDOW
