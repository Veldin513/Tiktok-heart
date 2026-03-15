from __future__ import annotations

import unittest
from pathlib import Path

from ttbot.models import ChatOpenMethod
from tiktok_messenger import ChatOpener
from ttbot.dispatch import TikTokSession
from ttbot.models import ChatOpenResult, Target


class FakeLocator:
    def __init__(self, *, visible=True, count_value=1, bounding_box=None, children=None, text='alice'):
        self._visible = visible
        self._count_value = count_value
        self._bounding_box = bounding_box or {'x': 0, 'y': 0, 'width': 10, 'height': 10}
        self.clicked = False
        self.wait_calls = []
        self.children = list(children or [])
        self._text = text

    @property
    def first(self):
        return self.children[0] if self.children else self

    def filter(self, **kwargs):
        visible = kwargs.get('visible')
        if visible is None:
            return self
        if self.children:
            filtered_children = [child for child in self.children if child.is_visible() == visible]
            return FakeLocator(children=filtered_children)
        if self.is_visible() == visible:
            return self
        return FakeLocator(count_value=0, children=[])

    def nth(self, index):
        return self.children[index]

    def count(self):
        return self._count_value if not self.children else len(self.children)

    def wait_for(self, **kwargs):
        self.wait_calls.append(kwargs)
        if not self.children and self._count_value == 0:
            raise RuntimeError('locator missing')
        return None

    def evaluate(self, _):
        self.clicked = True

    def is_visible(self):
        return self._visible

    def bounding_box(self):
        return self._bounding_box

    def get_by_text(self, *args, **kwargs):
        return self

    def inner_text(self):
        return self._text


class FakeMouse:
    def __init__(self):
        self.clicks = []

    def click(self, x, y):
        self.clicks.append((x, y))


class FakePage:
    def __init__(
        self,
        *,
        legacy_opens_editor=True,
        strict_opens_editor=True,
        strict_text='alice',
        visible_legacy_opens_editor=None,
        hidden_legacy_opens_editor=None,
        hidden_list_opens_editor=False,
        legacy_hidden_first=False,
    ):
        self.legacy_opens_editor = legacy_opens_editor
        self.strict_opens_editor = strict_opens_editor
        self.visible_legacy_opens_editor = legacy_opens_editor if visible_legacy_opens_editor is None else visible_legacy_opens_editor
        self.hidden_legacy_opens_editor = legacy_opens_editor if hidden_legacy_opens_editor is None else hidden_legacy_opens_editor
        self.hidden_list_opens_editor = hidden_list_opens_editor
        self.mouse = FakeMouse()
        self.visible_legacy = FakeLocator(visible=True)
        self.hidden_legacy = FakeLocator(visible=False)
        legacy_children = [self.hidden_legacy, self.visible_legacy] if legacy_hidden_first else [self.visible_legacy, self.hidden_legacy]
        self.legacy = FakeLocator(children=legacy_children)
        self.strict_visible_item = FakeLocator(visible=True, text=strict_text)
        self.hidden_item = FakeLocator(visible=False, text=strict_text)
        self.list_locator = FakeLocator(children=[self.strict_visible_item] if strict_opens_editor else [self.hidden_item])
        self.profile_button = FakeLocator(visible=True)
        self.url = 'https://www.tiktok.com/messages'
        self.goto_calls = []
        self.waited_for_url = False
        self.context = type('FakeContext', (), {'pages': [self]})()

    def get_by_text(self, *args, **kwargs):
        return self.legacy

    def locator(self, selector):
        if selector == '[data-e2e="inbox-list-item"]':
            return self.list_locator
        if selector == '.public-DraftEditor-content':
            loc = FakeLocator(visible=True)

            def wait_for(**kwargs):
                if self.visible_legacy.clicked and self.visible_legacy_opens_editor:
                    return None
                if self.hidden_legacy.clicked and self.hidden_legacy_opens_editor:
                    return None
                if self.strict_visible_item.clicked and self.strict_opens_editor:
                    return None
                if self.hidden_item.clicked and self.hidden_list_opens_editor:
                    return None
                raise RuntimeError('editor missing')

            loc.wait_for = wait_for
            return loc
        if selector == 'button':
            return self.profile_button
        return FakeLocator()

    def goto(self, url, **kwargs):
        self.goto_calls.append(url)
        self.url = url

    def wait_for_url(self, *args, **kwargs):
        self.waited_for_url = True


class MessengerTests(unittest.TestCase):
    def test_legacy_plan_a_is_preferred_when_it_works(self):
        page = FakePage(legacy_opens_editor=True, strict_opens_editor=True)
        result = ChatOpener().open_chat(page, 'alice', '@alice')
        self.assertTrue(result.ok)
        self.assertEqual(result.method, ChatOpenMethod.LEGACY_LIST)

    def test_recovery_scan_is_used_when_exact_legacy_lookups_fail(self):
        page = FakePage(legacy_opens_editor=False, strict_opens_editor=True)
        result = ChatOpener().open_chat(page, 'alice', '@alice')
        self.assertTrue(result.ok)
        self.assertEqual(result.method, ChatOpenMethod.LEGACY_LIST)
        self.assertTrue(page.strict_visible_item.clicked)
        self.assertFalse(page.waited_for_url)

    def test_profile_fallback_used_when_list_strategies_fail(self):
        page = FakePage(legacy_opens_editor=False, strict_opens_editor=False)
        result = ChatOpener().open_chat(page, 'alice', '@alice')
        self.assertTrue(result.ok)
        self.assertEqual(result.method, ChatOpenMethod.PROFILE)
        self.assertTrue(page.waited_for_url)

    def test_unicode_name_uses_recovery_scan_before_profile(self):
        page = FakePage(legacy_opens_editor=False, strict_opens_editor=True, strict_text='er.mine')
        result = ChatOpener().open_chat(page, 'er.mine', '@alice')
        self.assertTrue(result.ok)
        self.assertEqual(result.method, ChatOpenMethod.LEGACY_LIST)
        self.assertTrue(page.strict_visible_item.clicked)
        self.assertFalse(page.waited_for_url)

    def test_original_plan_a_broad_fallback_is_attempted_after_visible_locator_fails(self):
        page = FakePage(
            legacy_opens_editor=False,
            visible_legacy_opens_editor=False,
            hidden_legacy_opens_editor=True,
            strict_opens_editor=False,
            legacy_hidden_first=True,
        )
        result = ChatOpener().open_chat(page, 'alice', '@alice')
        self.assertTrue(result.ok)
        self.assertEqual(result.method, ChatOpenMethod.LEGACY_LIST)
        self.assertTrue(page.visible_legacy.clicked)
        self.assertTrue(page.hidden_legacy.clicked)

    def test_profile_fallback_is_used_only_after_all_list_recovery_strategies_fail(self):
        page = FakePage(
            legacy_opens_editor=False,
            visible_legacy_opens_editor=False,
            hidden_legacy_opens_editor=False,
            strict_opens_editor=False,
            hidden_list_opens_editor=False,
            strict_text='other-user',
        )
        result = ChatOpener().open_chat(page, 'alice', '@alice')
        self.assertTrue(result.ok)
        self.assertEqual(result.method, ChatOpenMethod.PROFILE)
        self.assertTrue(page.visible_legacy.clicked)
        self.assertTrue(page.waited_for_url)

    def test_hidden_row_recovery_is_attempted_after_visible_row_recovery(self):
        page = FakePage(
            legacy_opens_editor=False,
            visible_legacy_opens_editor=False,
            hidden_legacy_opens_editor=False,
            strict_opens_editor=False,
            hidden_list_opens_editor=True,
            strict_text='alice',
        )
        page.strict_visible_item._text = 'other-user'
        page.list_locator = FakeLocator(children=[page.strict_visible_item, page.hidden_item])
        result = ChatOpener().open_chat(page, 'alice', '@alice')
        self.assertTrue(result.ok)
        self.assertEqual(result.method, ChatOpenMethod.LEGACY_LIST)
        self.assertTrue(page.hidden_item.clicked)
        self.assertFalse(page.waited_for_url)


class FakeChatOpener:
    def __init__(self):
        self.calls = []

    def open_chat(self, page, target_name, profile_url=None):
        self.calls.append((page, target_name, profile_url))
        return ChatOpenResult(ok=True, method=ChatOpenMethod.LEGACY_LIST)


class SessionTests(unittest.TestCase):
    def test_session_reopens_messages_before_delegating_to_chat_opener(self):
        page = FakePage()
        opener = FakeChatOpener()
        session = TikTokSession(page=page, chat_opener=opener, artifacts_dir=Path('.'), telegram=object())

        result = session.open_chat(Target(name='alice', profile_url='@alice'))

        self.assertTrue(result.ok)
        self.assertEqual(page.goto_calls[-1], 'https://www.tiktok.com/messages')
        self.assertEqual(opener.calls[-1][1:], ('alice', '@alice'))


if __name__ == '__main__':
    unittest.main()
