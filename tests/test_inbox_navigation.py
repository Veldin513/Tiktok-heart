from __future__ import annotations

from pathlib import Path

import pytest

from yara_app.delivery import SendNotAttempted
from yara_app.tiktok_messenger import dismiss_chat_notice, open_chat_by_list, open_messages, send_message

pytestmark = pytest.mark.skipif(
    not (Path(__file__).resolve().parents[1] / '.playwright-browsers').exists(), reason='local Chromium not installed',
)


@pytest.fixture(scope='module')
def browser():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        instance = playwright.chromium.launch(headless=True)
        yield instance
        instance.close()


@pytest.fixture
def page(browser):
    tab = browser.new_page()
    yield tab
    tab.close()


def seed_inbox(page, *, title='ᵉʳ.ᵐⁱⁿᵉ', preview='Alice liked a video', handle='recipient', duplicate=False):
    page.set_content('''
      <div hidden data-e2e="inbox-list-item"><a data-e2e="inbox-title">ᵉʳ.ᵐⁱⁿᵉ</a> followed you</div>
      <div data-e2e="dm-new-chatbox"><div id="rows"></div><div id="thread"></div></div>
      <script>window.opened = 0;</script>
    ''')
    page.evaluate('''({title, preview, handle, duplicate}) => {
      for (let i=0;i<(duplicate ? 2 : 1);i++) {
        const row=document.createElement('div');
        row.setAttribute('data-e2e','dm-new-conversation-item');
        const name=document.createElement('p');
        name.setAttribute('data-e2e','dm-new-conversation-nickname');
        name.textContent=title;
        const message=document.createElement('p'); message.textContent=preview;
        row.append(name,message); document.querySelector('#rows').append(row);
        row.onclick=()=>{
          window.opened++;
          document.querySelector('#thread').innerHTML='<p data-e2e="chat-uniqueid"></p>' +
            '<div class="public-DraftEditor-content" contenteditable="true"></div>';
          document.querySelector('[data-e2e="chat-uniqueid"]').textContent='@'+handle;
        };
      }
    }''', {'title': title, 'preview': preview, 'handle': handle, 'duplicate': duplicate})


def test_modern_dm_title_not_combined_with_preview_or_hidden_activity(page):
    seed_inbox(page)
    assert open_chat_by_list(page, 'er.mine', 'https://www.tiktok.com/@recipient')
    assert page.evaluate('window.opened') == 1
    assert page.locator('.public-DraftEditor-content').inner_text() == ''


def test_message_preview_cannot_match_another_recipient(page):
    seed_inbox(page, title='Bob', preview='er.mine')
    assert not open_chat_by_list(page, 'er.mine', 'https://www.tiktok.com/@recipient')
    assert page.evaluate('window.opened') == 0


def test_duplicate_names_do_not_choose_first_conversation(page):
    seed_inbox(page, duplicate=True)
    assert not open_chat_by_list(page, 'er.mine', 'https://www.tiktok.com/@recipient')
    assert page.evaluate('window.opened') == 0


def test_unique_handle_mismatch_never_confirms_chat(page):
    seed_inbox(page, handle='someone-else')
    assert not open_chat_by_list(page, 'er.mine', 'https://www.tiktok.com/@recipient')


def test_modern_messages_do_not_switch_to_hidden_notifications(page, monkeypatch):
    page.route('https://www.tiktok.com/messages', lambda route: route.fulfill(
        body='<div hidden data-e2e="inbox-list-item">Alice followed you</div>'
             '<div data-e2e="dm-new-conversation-item">DM</div>', content_type='text/html',
    ))
    monkeypatch.setattr('yara_app.tiktok_messenger._ensure_messages_tab', lambda *_a: pytest.fail('DM list already visible'))
    open_messages(page)
    assert page.locator('[data-e2e="dm-new-conversation-item"]').is_visible()


def modal(page, *, label):
    page.set_content('''<div class="public-DraftEditor-content" contenteditable="true"></div>
      <div id="portal"><div class="TUXModal-overlay" style="position:fixed;inset:0;background:#777"></div>
        <div role="dialog" style="position:fixed;top:20px;left:20px"><button></button></div></div>
      <script>window.clicked=0;document.querySelector('button').onclick=()=>{
        window.clicked++;document.querySelector('#portal').remove();
      };</script>''')
    page.locator('button').evaluate('(node, label) => node.setAttribute("aria-label",label)', label)


@pytest.mark.parametrize('label', ['Close', 'Закрыть', 'Не сейчас'])
def test_notice_uses_close_control_without_forcing_click_through_overlay(page, label):
    modal(page, label=label)
    dismiss_chat_notice(page)
    assert page.evaluate('window.clicked') == 1
    page.locator('.public-DraftEditor-content').click(trial=True)


def test_consent_dialog_is_not_accepted_and_send_never_starts(page):
    modal(page, label='Accept')
    with pytest.raises(SendNotAttempted, match='Всплывающее окно'):
        send_message(page, 'not sent', confirmation_timeout_ms=100)
    assert page.evaluate('window.clicked') == 0
    assert page.locator('.public-DraftEditor-content').inner_text() == ''
