
from ttbot.models import ControlStore


def test_store_seeds_and_loads_messages():
    store = ControlStore()
    messages = store.load_messages()
    assert isinstance(messages, list)
    assert len(messages) >= 20


def test_add_and_remove_message_roundtrip():
    store = ControlStore()
    text = 'test control message unique'
    try:
        try:
            store.remove_message(text)
        except Exception:
            pass
        store.add_message(text)
        assert text in store.load_messages()
        store.remove_message(text)
        assert text not in store.load_messages()
    finally:
        try:
            store.remove_message(text)
        except Exception:
            pass
