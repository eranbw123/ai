#!/usr/bin/env python3
"""Fully automated end-to-end check of council_bot.py -- no human needed.

Unlike verify_heartbeat_live.py (which nudges your real Telegram chat and
waits for a human to reply by hand), this script needs no human interaction
at all: it launches council_bot.py as a subprocess pointed at a small local
fake Telegram server (this file's own FakeTelegramServer) instead of the
real api.telegram.org, injects one synthetic incoming message, and watches
the subprocess's own stdout/stderr for the full round trip (question
received -> heartbeat pings -> reply sent).

Only the Telegram transport is faked -- COUNCIL_BACKEND, CLAUDE_ORG_ID etc.
still come from .env.local as normal, so the council generation itself
still goes through the REAL claude.ai/Chrome CDP backend. This exercises
real message-handling code, real threading/heartbeat timing, and the real
OS console-encoding/buffering behavior (the two live-only bugs this repo
already hit), without ever touching your actual Telegram chat or bot token
-- it uses a throwaway fake token/chat id for the subprocess.

Deliberately still not wired into a pre-commit hook or CI: it drives a
real Chrome tab and can take 1-3 minutes. Run it (or have Claude run it)
whenever council_bot.py's message-handling path changes.

Usage:
    python e2e_verify_bot.py [--timeout SECONDS] [--question TEXT]

Exit code 0 = pass, 1 = fail.
"""
import argparse
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from common import REPO_ROOT

FAKE_CHAT_ID = 999999999
FAKE_TOKEN = "e2e-fake-token"
DEFAULT_QUESTION = "e2e_verify_bot.py automated test question -- give a one-sentence answer."


class FakeTelegramState:
    """Thread-safe queue of fake inbound updates + log of outbound sends,
    shared between the harness (which enqueues/inspects) and the HTTP
    handler threads (which serve council_bot's polling)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.pending_updates = []
        self.next_update_id = 1
        self.sent_messages = []  # [(chat_id, text), ...]

    def enqueue_message(self, text, chat_id=FAKE_CHAT_ID):
        with self.lock:
            update = {
                "update_id": self.next_update_id,
                "message": {
                    "message_id": self.next_update_id,
                    "chat": {"id": chat_id},
                    "text": text,
                },
            }
            self.next_update_id += 1
            self.pending_updates.append(update)

    def updates_from(self, offset):
        with self.lock:
            # Mirrors real Telegram semantics closely enough for this test:
            # a poll with a given offset only ever sees updates >= it, so
            # already-processed ones (offset advances past them) naturally
            # stop being returned without needing to mutate the list.
            return [u for u in self.pending_updates if u["update_id"] >= offset]

    def record_sent(self, chat_id, text):
        with self.lock:
            self.sent_messages.append((chat_id, text))
        return len(self.sent_messages)


class QuietThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # A client (the council_bot subprocess) disconnecting mid-response --
        # e.g. during this harness's own shutdown -- is expected, not a bug;
        # don't spam a traceback for it. Anything else still prints normally.
        exc_type = sys.exc_info()[0]
        if exc_type and issubclass(exc_type, (ConnectionError, TimeoutError)):
            return
        super().handle_error(request, client_address)


def make_handler(state):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass  # keep this harness's own prints as the signal, not raw HTTP noise

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                body = {}
            method = self.path.rsplit("/", 1)[-1]  # path is /bot<token>/<method>

            if method == "getUpdates":
                offset = body.get("offset", 0)
                if offset == -1:
                    result = []  # startup "skip backlog" probe -- nothing queued yet
                else:
                    # Poll briefly instead of a real long-poll wait, to keep
                    # this harness fast; council_bot just loops again if empty.
                    deadline = time.monotonic() + 0.3
                    result = state.updates_from(offset)
                    while not result and time.monotonic() < deadline:
                        time.sleep(0.05)
                        result = state.updates_from(offset)
                self._reply({"ok": True, "result": result})
            elif method == "sendMessage":
                state.record_sent(body.get("chat_id"), body.get("text", ""))
                self._reply({"ok": True, "result": {"message_id": 1}})
            else:
                self._reply({"ok": False, "description": f"unhandled method {method}"}, status=404)

        def _reply(self, payload, status=200):
            data = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return Handler


def stream_reader(pipe, sink, echo_prefix):
    """Read a subprocess pipe line by line into `sink` (a list), printing
    each line as it arrives so a human watching this script's own output
    sees the bot's activity live. Runs in its own thread; exits when the
    pipe closes (subprocess exited)."""
    for line in iter(pipe.readline, ""):
        sink.append(line.rstrip("\n"))
        print(f"[{echo_prefix}] {line.rstrip(chr(10))}")
    pipe.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--timeout", type=int, default=240, help="max seconds to wait for the full round trip")
    parser.add_argument("--question", default=DEFAULT_QUESTION)
    args = parser.parse_args()

    state = FakeTelegramState()
    server = QuietThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    port = server.server_address[1]
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    print(f"Fake Telegram server up on http://127.0.0.1:{port}")

    env = os.environ.copy()
    env["TELEGRAM_API_BASE"] = f"http://127.0.0.1:{port}"
    env["TELEGRAM_BOT_TOKEN"] = FAKE_TOKEN
    env["TELEGRAM_CHAT_ID"] = str(FAKE_CHAT_ID)
    # PYTHONUNBUFFERED isn't required (council_bot.py forces line buffering
    # itself now) but costs nothing and keeps this belt-and-suspenders.
    env["PYTHONUNBUFFERED"] = "1"

    proc = subprocess.Popen(
        [sys.executable, str(REPO_ROOT / "council_bot.py")],
        cwd=str(REPO_ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
    )

    stdout_lines, stderr_lines = [], []
    threading.Thread(target=stream_reader, args=(proc.stdout, stdout_lines, "bot"), daemon=True).start()
    threading.Thread(target=stream_reader, args=(proc.stderr, stderr_lines, "bot:err"), daemon=True).start()

    try:
        # Wait for the subprocess to actually be polling before injecting
        # the message -- otherwise it could land before the startup
        # skip-backlog probe and get silently skipped.
        deadline = time.monotonic() + 15
        while proc.poll() is None and time.monotonic() < deadline:
            if any("listening for Telegram" in l for l in stdout_lines):
                break
            time.sleep(0.1)
        else:
            if proc.poll() is not None:
                return _fail(proc, "council_bot.py exited before it even started listening.")

        print(f"Injecting fake message: {args.question!r}")
        state.enqueue_message(args.question)

        deadline = time.monotonic() + args.timeout
        outcome = None
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                return _fail(proc, f"council_bot.py exited unexpectedly (code {proc.returncode}).")
            if any(l.startswith("Sent reply to Telegram.") for l in stdout_lines):
                outcome = "sent"
                break
            if any(l.startswith("Council failed:") for l in stdout_lines):
                outcome = "failed"
                break
            time.sleep(0.2)

        heartbeat_pings = sum(1 for l in stdout_lines if l.startswith("[heartbeat] ping sent"))
        saw_question = any(l.startswith("Question:") for l in stdout_lines)
        outbound = [text for chat_id, text in state.sent_messages]

        print()
        print("=" * 60)
        if not saw_question:
            return _fail(proc, "council_bot never logged receiving the injected question.")
        if outcome is None:
            return _fail(proc, f"No reply within {args.timeout}s (question was received).")

        print(f"Question received: yes")
        print(f"Heartbeat pings (subprocess log): {heartbeat_pings}")
        print(f"Outbound messages via fake Telegram server: {len(outbound)}")
        for i, text in enumerate(outbound, 1):
            print(f"  {i}. {text[:100]}{'...' if len(text) > 100 else ''}")
        print(f"Outcome: {outcome}")

        if outcome == "failed":
            print("RESULT: FAIL -- council run completed but reported a failure (see [bot] lines above).")
            return 1

        if not outbound:
            return _fail(proc, "Reply logged locally but nothing arrived at the fake Telegram server.")

        print("RESULT: PASS -- question in, reply out, no crash, all confirmed via the fake server too.")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        server.shutdown()


def _fail(proc, message):
    print()
    print("=" * 60)
    print(f"RESULT: FAIL -- {message}")
    if proc.poll() is not None and proc.returncode not in (0, None):
        print(f"(subprocess exit code: {proc.returncode} -- check the [bot:err] lines above for a traceback)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
