"""Observe TikTok's own send/ack traffic; never send or replay IM requests.

Field numbers follow the im_proto definitions in TikTok's web IM SDK
(async/89240.83338e1c.js). Unknown formats fail closed. Tokens and raw packets
are neither persisted nor logged.
"""
from __future__ import annotations

import base64
import json
from contextlib import suppress
from dataclasses import dataclass
from urllib.parse import urlparse

MAX_PACKET_BYTES = 2 * 1024 * 1024


class DeliveryUnconfirmed(RuntimeError):
    pass


class DeliveryRejected(RuntimeError):
    pass


class SendNotAttempted(RuntimeError):
    pass


@dataclass(frozen=True)
class SendReceipt:
    server_message_id: str
    client_message_id: str
    transport: str

    def __post_init__(self):
        if not self.server_message_id.isdigit() or int(self.server_message_id) <= 0 or not self.client_message_id:
            raise ValueError('Invalid send receipt')


def _varint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    for shift in range(0, 70, 7):
        if offset >= len(data):
            raise ValueError('truncated protobuf')
        byte = data[offset]
        offset += 1
        value |= (byte & 127) << shift
        if byte < 128:
            return value, offset
    raise ValueError('invalid varint')


def _fields(data: bytes) -> dict[int, int | bytes]:
    if not isinstance(data, bytes) or not data or len(data) > MAX_PACKET_BYTES:
        raise ValueError('unsupported packet')
    result = {}
    offset = 0
    while offset < len(data):
        tag, offset = _varint(data, offset)
        field, wire = tag >> 3, tag & 7
        if not field:
            raise ValueError('invalid field')
        if wire == 0:
            value, offset = _varint(data, offset)
        elif wire == 2:
            length, offset = _varint(data, offset)
            if length > len(data) - offset:
                raise ValueError('truncated field')
            value = data[offset:offset + length]
            offset += length
        elif wire in (1, 5):
            length = 8 if wire == 1 else 4
            if length > len(data) - offset:
                raise ValueError('truncated field')
            offset += length
            continue
        else:
            raise ValueError('unsupported wire type')
        # The protocol contains repeated header/map fields. Only scalar/body
        # fields are read below; protobuf uses the last scalar occurrence.
        result[field] = value
    return result


def _packet(data: bytes) -> dict[int, int | bytes]:
    if isinstance(data, str):
        if len(data) > MAX_PACKET_BYTES * 4 // 3 + 4:
            raise ValueError('unsupported packet')
        data = base64.b64decode(data, validate=True)
    fields = _fields(data)
    # WebSocket Frame: service=3, method=4, payload=8. HTTP uses Request/
    # Response directly. The frame's sequence number is not an IM command.
    if isinstance(fields.get(3), int) and isinstance(fields.get(4), int) and isinstance(fields.get(8), bytes):
        encoding = fields.get(6, b'')
        if encoding not in (b'', b'identity', b'pb', b'protobuf'):
            raise ValueError('unsupported frame encoding')
        return _fields(fields[8])
    return fields


def _text(value: int | bytes | None) -> str:
    return value.decode('utf-8') if isinstance(value, bytes) else ''


def _tiktok_transport(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in ('https', 'wss') and (parsed.hostname or '').endswith('.tiktok.com')


class DeliveryTracker:
    def __init__(self, page):
        self.expected_text: str | None = None
        self.pending: dict[tuple[str, int], str] = {}
        self.receipt: SendReceipt | None = None
        self.rejection: str | None = None
        self.observed_request = False
        page.on('websocket', self._socket)
        page.on('request', self._request)
        page.on('requestfinished', self._finished)

    def begin(self, text: str) -> None:
        self.expected_text = text
        self.pending.clear()
        self.receipt = None
        self.rejection = None
        self.observed_request = False

    def end(self) -> None:
        self.expected_text = None
        self.pending.clear()

    def observe_request(self, data: bytes, transport: str) -> None:
        if self.expected_text is None:
            return
        try:
            packet = _packet(data)
            if packet.get(1) != 100 or not isinstance(packet.get(2), int):
                return
            body = _fields(_fields(packet[8])[100])
            content = _text(body.get(4))
            try:
                parsed = json.loads(content)
                content = parsed.get('text') if isinstance(parsed, dict) else None
            except ValueError:
                pass
            client_id = _text(body.get(8))
            if content == self.expected_text and client_id:
                self.pending[(transport, packet[2])] = client_id
                self.observed_request = True
        except (KeyError, TypeError, ValueError, UnicodeError):
            return

    def observe_response(self, data: bytes, transport: str) -> None:
        if not self.pending:
            return
        try:
            packet = _packet(data)
            if packet.get(1) != 100:
                return
            client_id = self.pending.get((transport, packet.get(2)))
            if not client_id:
                return
            if packet.get(3, 0) != 0:
                self.rejection = 'TikTok отклонил запрос отправки.'
                return
            body = _fields(_fields(packet[6])[100])
            if _text(body.get(4)) != client_id:
                return
            if body.get(3, 0) != 0 or body.get(5, 0) != 0:
                self.rejection = 'TikTok не подтвердил приём сообщения (проверка или отказ).'
                return
            server_id = body.get(1)
            if isinstance(server_id, int) and server_id > 0:
                self.receipt = SendReceipt(str(server_id), client_id, transport.split(':', 1)[0])
        except (KeyError, TypeError, ValueError, UnicodeError):
            return

    def _socket(self, socket) -> None:
        if not _tiktok_transport(socket.url):
            return
        transport = f'websocket:{id(socket)}'
        socket.on('framesent', lambda data: self.observe_request(data, transport))
        socket.on('framereceived', lambda data: self.observe_response(data, transport))

    def _request(self, request) -> None:
        if self.expected_text is None or request.method != 'POST' or not _tiktok_transport(request.url):
            return
        with suppress(Exception):
            self.observe_request(request.post_data_buffer, f'http:{id(request)}')

    def _finished(self, request) -> None:
        transport = f'http:{id(request)}'
        if not any(key[0] == transport for key in self.pending):
            return
        try:
            response = request.response()
            if response.status >= 400:
                self.rejection = 'Сервер отклонил запрос отправки.'
                return
            self.observe_response(response.body(), transport)
        except Exception:
            pass


def attach_delivery_tracker(page) -> DeliveryTracker:
    tracker = getattr(page, '_heart_delivery_tracker', None)
    if tracker is None:
        tracker = DeliveryTracker(page)
        page._heart_delivery_tracker = tracker
    return tracker
