from pathlib import Path

from yara_app.ttbot.models import StateStore


def test_state_store_roundtrip(tmp_path: Path):
    store = StateStore(tmp_path, cooldown_hours=12)
    target = 'бµ‰Кі.бµђвЃ±вЃїбµ‰'
    assert store.get_cooldown_status(target).allowed is True
    store.mark_sent_now(target)
    assert store.get_cooldown_status(target).allowed is False
    streak = store.update_streak_stats(target)
    assert streak.current_count >= 1
