"""Minimal Chrome DevTools Protocol (CDP) client, stdlib-only.

Connects to a Chrome instance launched with --remote-debugging-port and lets
Python run JavaScript inside one of its real, already-authenticated tabs via
Runtime.evaluate -- the same mechanism the DevTools Console itself uses.

This is not a general WebSocket library: just enough of RFC 6455 (client-to-
server masked text frames, up to Chrome's typical response sizes) to talk to
a local CDP endpoint.
"""
import base64
import hashlib
import json
import os
import socket
import struct
import urllib.request
from urllib.parse import urlparse


def list_tabs(port=9222):
    with urllib.request.urlopen(f"http://localhost:{port}/json") as resp:
        return json.loads(resp.read().decode("utf-8"))


def find_tab(url_prefix, port=9222):
    for tab in list_tabs(port):
        if tab.get("type") == "page" and tab.get("url", "").startswith(url_prefix):
            return tab
    return None


def find_claude_tab(port=9222):
    return find_tab("https://claude.ai", port)


class CDPConnection:
    def __init__(self, ws_url):
        parsed = urlparse(ws_url)
        self.host = parsed.hostname
        self.port = parsed.port
        self.path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        self._id = 0
        self.sock = socket.create_connection((self.host, self.port), timeout=30)
        self._handshake()

    def _handshake(self):
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET {self.path} HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(request.encode("utf-8"))
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("WebSocket handshake failed (connection closed)")
            response += chunk
        if b"101" not in response.split(b"\r\n", 1)[0]:
            raise ConnectionError(f"WebSocket handshake rejected: {response[:200]!r}")

    def _send_text(self, text):
        payload = text.encode("utf-8")
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        length = len(payload)
        header = bytearray([0x81])  # FIN + text frame opcode
        if length <= 125:
            header.append(0x80 | length)
        elif length <= 0xFFFF:
            header.append(0x80 | 126)
            header += struct.pack(">H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", length)
        self.sock.sendall(bytes(header) + mask + masked)

    def _recv_frame(self):
        header = self._recv_exact(2)
        b0, b1 = header[0], header[1]
        opcode = b0 & 0x0F
        masked = bool(b1 & 0x80)
        length = b1 & 0x7F
        if length == 126:
            length = struct.unpack(">H", self._recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._recv_exact(8))[0]
        mask_key = self._recv_exact(4) if masked else b""
        data = self._recv_exact(length)
        if masked:
            data = bytes(b ^ mask_key[i % 4] for i, b in enumerate(data))
        return opcode, data

    def _recv_exact(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("WebSocket connection closed unexpectedly")
            buf += chunk
        return buf

    def send(self, method, params=None, timeout=60):
        self._id += 1
        msg_id = self._id
        self._send_text(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
        self.sock.settimeout(timeout)
        while True:
            opcode, data = self._recv_frame()
            if opcode != 0x1:  # text frame
                continue
            message = json.loads(data.decode("utf-8"))
            if message.get("id") == msg_id:
                return message

    def evaluate(self, expression, await_promise=True, timeout=120):
        result = self.send(
            "Runtime.evaluate",
            {
                "expression": expression,
                "awaitPromise": await_promise,
                "returnByValue": True,
            },
            timeout=timeout,
        )
        remote = result.get("result", {})
        # exceptionDetails is nested INSIDE the response's "result" object
        # (message -> result -> {result, exceptionDetails}), not at the top
        # level beside "id"/"result". Checking the top level -- as this did
        # until 2026-08-18 -- never matched, so every JS exception was
        # swallowed and evaluate() returned the thrown value instead: for a
        # rejected async IIFE that is `{}`, which callers then read as an
        # empty-but-valid API payload.
        #
        # Live consequence, measured before the fix: against a logged-out
        # claude.ai tab the conversation-list fetch threw on HTTP 403,
        # evaluate() returned {}, fetch_all_conversation_summaries() read
        # `data` as [], and poll_conversations.py logged "0 of 0
        # conversations to import" with failed=0 -- a clean success that
        # advanced poll_state.json's watermark past 12 days of conversations
        # it had never actually fetched. An expired session must fail loudly;
        # it is the one error that most closely imitates "no new data".
        if "exceptionDetails" in remote:
            raise RuntimeError(f"JS exception: {remote['exceptionDetails']}")
        return remote.get("result", {}).get("value")

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass
