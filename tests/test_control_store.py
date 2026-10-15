
from yara_app.ttbot.models import ControlStore


def test_store_seeds_and_loads_messages():
    store = ControlStore()
    messages = store.load_messages()
    assert isinstance(messages, list)
    assert len(messages) >= 20
