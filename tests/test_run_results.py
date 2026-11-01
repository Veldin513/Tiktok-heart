import pytest

from yara_app.run_results import outcome


@pytest.mark.parametrize(('record', 'expected'), [
    ({'event': 'run_finished'}, 'finished'),
    ({'event': 'run_finished', 'status': 'failed'}, 'failed'),
    ({'event': 'run_finished', 'status': 'success', 'success_count': 1}, 'unknown'),
    ({'success': True}, 'unknown'),
    ({'success': True, 'confirmation': 'server_ack'}, 'accepted'),
    ({'status': 'partial', 'success_count': 1, 'confirmed_count': 1, 'failed_count': 1}, 'partial'),
    ({'reason': 'send_rejected', 'success': False}, 'rejected'),
    ({'status': 'stopped'}, 'stopped'),
    ({'status': 'running', 'success_count': 1}, 'running'),
    ({'reason': 'send_failed'}, 'unknown'),
    ({'skipped': True}, 'skipped'),
])
def test_outcomes_never_equate_process_completion_with_delivery(record, expected):
    assert outcome(record)['code'] == expected
