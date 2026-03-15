
from telegram_control_bot import parse_command


def test_parse_command():
    cmd, rest = parse_command('/run demo_profile')
    assert cmd == '/run'
    assert rest == 'demo_profile'
