
from yara_app.telegram_control_bot import parse_command


def test_parse_command():
    cmd, rest = parse_command('/run test_profile')
    assert cmd == '/run'
    assert rest == 'test_profile'
