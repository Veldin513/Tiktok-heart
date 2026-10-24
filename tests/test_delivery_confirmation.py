from __future__ import annotations

import base64
import hashlib
import json
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from yara_app.delivery import DeliveryTracker, SendReceipt


def varint(value):
    result = bytearray()
    while value > 127:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def field(number, value):
    if isinstance(value, str):
        value = value.encode()
    if isinstance(value, bytes):
        return varint((number << 3) | 2) + varint(len(value)) + value
    return varint(number << 3) + varint(value)


def request(*, sequence=42, client='client-test', text='hello'):
    body = field(1, 'conversation-test') + field(4, json.dumps({'text': text})) + field(8, client)
    return field(1, 100) + field(2, sequence) + field(8, field(100, body))


def response(*, sequence=42, client='client-test', server_id=987654, status=0, check_code=0, response_code=0):
    body = field(1, server_id) + field(3, status) + field(4, client) + field(5, check_code)
    return field(1, 100) + field(2, sequence) + field(3, response_code) + field(6, field(100, body))


def frame(payload):
    return field(1, 1) + field(2, 1) + field(3, 33554513) + field(4, 2) + field(8, payload)


def tracker():
    return DeliveryTracker(SimpleNamespace(on=lambda *_a: None))


@pytest.mark.parametrize('wrapped', [False, True])
def test_only_matching_server_ack_confirms_send(wrapped):
    t = tracker()
    t.begin('hello')
    wrap = frame if wrapped else lambda value: value
    t.observe_request(wrap(request()), 'websocket:one')
    assert t.receipt is None
    t.observe_response(wrap(response()), 'websocket:one')
    assert t.receipt == SendReceipt('987654', 'client-test', 'websocket')


@pytest.mark.parametrize('change', [
    {'sequence': 43}, {'client': 'other-message'}, {'server_id': 0},
    {'status': 1}, {'status': 3}, {'status': 4}, {'check_code': 9}, {'response_code': 1},
])
def test_unrelated_empty_or_rejected_ack_cannot_be_success(change):
    t = tracker()
    t.begin('hello')
    t.observe_request(request(), 'http:one')
    t.observe_response(response(**change), 'http:one')
    assert t.receipt is None


def test_old_message_other_text_and_other_socket_cannot_confirm_send():
    t = tracker()
    t.begin('hello')
    t.observe_response(response(), 'websocket:one')
    assert t.receipt is None
    t.observe_request(request(text='different'), 'websocket:one')
    t.observe_response(response(), 'websocket:one')
    assert t.receipt is None
    t.observe_request(request(), 'websocket:one')
    t.observe_response(response(), 'websocket:other')
    assert t.receipt is None


@pytest.mark.parametrize('packet', [b'', b'\x80', b'\x42\xff\xff', b'sessionid=fake-secret', 'not-binary'])
def test_unknown_protocol_does_not_confirm_or_expose_packet(packet):
    t = tracker()
    t.begin('hello')
    t.observe_request(packet, 'http:one')
    t.observe_response(packet, 'http:one')
    assert t.receipt is None
    assert t.rejection is None


def test_repeated_header_fields_do_not_hide_real_ack():
    t = tracker()
    t.begin('hello')
    t.observe_request(request(), 'http:one')
    headers = field(8, field(1, 'header-a')) + field(8, field(1, 'header-b'))
    t.observe_response(response() + headers, 'http:one')
    assert t.receipt is not None


def test_base64_websocket_subprotocol_receipt():
    t = tracker()
    t.begin('hello')
    t.observe_request(base64.b64encode(frame(request())).decode(), 'websocket:one')
    t.observe_response(base64.b64encode(frame(response())).decode(), 'websocket:one')
    assert t.receipt is not None


@pytest.mark.skipif(not (Path(__file__).resolve().parents[1] / '.playwright-browsers').exists(), reason='local Chromium not installed')
@pytest.mark.parametrize('outcome', ['accepted', 'no_ack', 'rejected'])
def test_native_websocket_ack_with_optimistic_composer_clear(tmp_path, monkeypatch, outcome):
    from playwright.sync_api import sync_playwright

    from yara_app.delivery import DeliveryRejected, DeliveryUnconfirmed, attach_delivery_tracker
    from yara_app.tiktok_messenger import send_message

    monkeypatch.setenv('PLAYWRIGHT_BROWSERS_PATH', str(Path(__file__).resolve().parents[1] / '.playwright-browsers'))
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            accept = base64.b64encode(hashlib.sha1((self.headers['Sec-WebSocket-Key'] + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
            self.send_response(101)
            self.send_header('Upgrade', 'websocket')
            self.send_header('Connection', 'Upgrade')
            self.send_header('Sec-WebSocket-Accept', accept)
            self.end_headers()
            self.connection.settimeout(5)
            # Consume one native masked browser frame (RFC 6455).
            header = self.rfile.read(2)
            size = header[1] & 127
            if size == 126:
                size = struct.unpack('!H', self.rfile.read(2))[0]
            elif size == 127:
                size = struct.unpack('!Q', self.rfile.read(8))[0]
            self.rfile.read(4)  # mask
            self.rfile.read(size)
            if outcome != 'no_ack':
                payload = frame(response(status=0 if outcome == 'accepted' else 3))
                self.connection.sendall(bytes([0x82, len(payload)]) + payload)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f'ws://127.0.0.1:{server.server_port}/test'
    # Test transport is isolated loopback. Production still accepts only TikTok.
    monkeypatch.setattr('yara_app.delivery._tiktok_transport', lambda value: value == url)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            attach_delivery_tracker(page)
            data = list(frame(request()))
            page.set_content('''<div class="public-DraftEditor-content" contenteditable="true"></div>
                <button data-e2e="message-send">Send</button><script>
                window.testSocket = new WebSocket(''' + json.dumps(url) + ''');
                document.querySelector('button').onclick = () => {
                    document.querySelector('[contenteditable]').innerText = '';
                    testSocket.send(new Uint8Array(''' + json.dumps(data) + '''));
                };</script>''')
            page.wait_for_function('() => testSocket.readyState === WebSocket.OPEN')
            try:
                if outcome == 'accepted':
                    assert send_message(page, 'hello', confirmation_timeout_ms=1000).server_message_id == '987654'
                else:
                    with pytest.raises(DeliveryUnconfirmed if outcome == 'no_ack' else DeliveryRejected):
                        send_message(page, 'hello', confirmation_timeout_ms=500)
                assert page.locator('[contenteditable]').inner_text() == ''
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.skipif(not (Path(__file__).resolve().parents[1] / '.playwright-browsers').exists(), reason='local Chromium not installed')
def test_native_http_ack_is_matched_to_the_actual_request(monkeypatch):
    from playwright.sync_api import sync_playwright

    from yara_app.delivery import attach_delivery_tracker
    from yara_app.tiktok_messenger import send_message

    data = list(request())
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            body = ('''<div class="public-DraftEditor-content" contenteditable="true"></div>
                <button data-e2e="message-send">Send</button><script>
                document.querySelector('button').onclick = () => {
                    document.querySelector('[contenteditable]').innerText = '';
                    fetch('/send', {method:'POST', body:new Uint8Array(''' + json.dumps(data) + ''')});
                };</script>''').encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            assert self.rfile.read(int(self.headers['Content-Length'])) == request()
            body = response()
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    monkeypatch.setenv('PLAYWRIGHT_BROWSERS_PATH', str(Path(__file__).resolve().parents[1] / '.playwright-browsers'))
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f'http://127.0.0.1:{server.server_port}'
    monkeypatch.setattr('yara_app.delivery._tiktok_transport', lambda value: value == url + '/send')
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            attach_delivery_tracker(page)
            page.goto(url)
            try:
                receipt = send_message(page, 'hello', confirmation_timeout_ms=1500)
                assert receipt == SendReceipt('987654', 'client-test', 'http')
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
