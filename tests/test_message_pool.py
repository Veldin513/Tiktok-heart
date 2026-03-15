from config import MESSAGE_VARIANTS
from config import MessageSelector


def test_message_pool_has_many_items():
    assert len(MESSAGE_VARIANTS) >= 150


def test_message_selector_cycles_without_loss():
    selector = MessageSelector(['a', 'b', 'c'])
    seen = {selector.next(), selector.next(), selector.next()}
    assert seen == {'a', 'b', 'c'}


def test_message_selector_avoids_immediate_repeat_after_refill(monkeypatch):
    monkeypatch.setattr('random.shuffle', lambda items: None)
    selector = MessageSelector(['a', 'b'])
    first_cycle = [selector.next(), selector.next()]
    second_cycle_first = selector.next()
    assert first_cycle == ['a', 'b']
    assert second_cycle_first == 'a'
    assert second_cycle_first != first_cycle[-1]
