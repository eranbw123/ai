#!/usr/bin/env python3
"""End-to-end check of council_bot.py's message-handling path (heartbeat +
council reply), in two modes sharing one round-trip-detection core.

  mock (default) -- no human needed, safe to run any time. Launches
    council_bot.py as a subprocess pointed at a small local fake Telegram
    server (this file's own FakeTelegramServer) and a throwaway fake
    token/chat id, injects one synthetic message itself, and uses a lean
    synthetic context file (a couple hundred bytes, not a real conversation
    pulled from conversations.db) to keep the round trip fast and cheap,
    predictable, and independent of whatever happens to be in the db right
    now. Only the
    Telegram transport and context size are faked -- COUNCIL_BACKEND,
    CLAUDE_ORG_ID etc. still come from .env.local, so it's still a real
    claude.ai/Chrome CDP generation. Never touches the real bot/chat.

  real -- needs council_bot.py already running against the real Telegram
    API, and a human to send one message by hand. Sends a nudge to the
    real chat, then watches council_bot.log (not a subprocess -- a second
    getUpdates consumer on the same real token would race the running bot
    for updates) for the same round trip. Slower, touches the real chat,
    and is the only way to confirm the real end-to-end experience (message
    formatting etc.) rather than the mocked transport. Run this mode only
    when deliberately deciding to -- not the default, not automatic.

Both modes check the same two things: the heartbeat fires during a
still-running question, and the council reply actually arrives. Neither
mode is a pre-commit hook or CI job -- both drive a real Chrome tab and
take well under a minute (mock) to a few minutes (real, includes waiting
on a human). Run by hand (or have Claude run mock mode) whenever
council_bot.py's message-handling path changes.

Usage:
    python e2e_verify_bot.py                       # mock, default
    python e2e_verify_bot.py --mode real            # needs a human reply
    python e2e_verify_bot.py --timeout SECONDS --question TEXT

Exit code 0 = pass, 1 = fail.
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from common import REPO_ROOT, load_env_local
from council_bot import tg_send_message

FAKE_CHAT_ID = 999999999
FAKE_TOKEN = "e2e-fake-token"
DEFAULT_QUESTION = "e2e_verify_bot.py test question -- reply in one short sentence."
LEAN_CONTEXT = "e2e_verify_bot.py test context. Not real conversation history -- ignore for content, just confirm you can read it."
LOG_PATH = REPO_ROOT / "council_bot.log"
ERR_LOG_PATH = REPO_ROOT / "council_bot.err.log"


class RoundTripDetector:
    """Shared verdict logic for both modes: feed it council_bot's own log
    lines (from a subprocess pipe or a tailed log file, doesn't matter) and
    ask it whether the round trip completed. This -- not the transport --
    is "the single logic" both mock and real mode share."""

    def __init__(self):
        self.got_question = False
        self.heartbeat_pings = 0
        self.outcome = None  # "sent" | "failed" | None (still waiting)
        self.outcome_line = None

    def feed(self, line):
        if line.startswith("Question:") and not self.got_question:
            self.got_question = True
        elif line.startswith("[heartbeat] ping sent"):
            self.heartbeat_pings += 1
        elif line.startswith("Sent reply to Telegram."):
            self.outcome, self.outcome_line = "sent", line
        elif line.startswith("Council failed:"):
            self.outcome, self.outcome_line = "failed", line

    def report(self, extra_lines=()):
        print()
        print("=" * 60)
        if not self.got_question:
            print("RESULT: FAIL -- council_bot never logged receiving the question.")
            return 1
        if self.outcome is None:
            print("RESULT: FAIL -- question received but no reply within the timeout.")
            return 1
        print(f"Question received: yes")
        print(f"Heartbeat pings: {self.heartbeat_pings}")
        print(f"Outcome: {self.outcome_line}")
        for line in extra_lines:
            print(line)
        if self.outcome == "failed":
            print("RESULT: FAIL -- council run completed but reported a failure (see above).")
            return 1
        print("RESULT: PASS -- question in, reply out, no crash.")
        return 0


# ---------------------------------------------------------------- mock mode

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
                "message": {"message_id": self.next_update_id, "chat": {"id": chat_id}, "text": text},
            }
            self.next_update_id += 1
            self.pending_updates.append(update)

    def updates_from(self, offset):
        with self.lock:
            # Mirrors real Telegram semantics closely enough for this test: a
            # poll with a given offset only sees updates >= it, so already-
            # processed ones stop being returned without needing mutation.
            return [u for u in self.pending_updates if u["update_id"] >= offset]

    def record_sent(self, chat_id, text):
        with self.lock:
            self.sent_messages.append((chat_id, text))


class QuietThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # A client (the council_bot subprocess) disconnecting mid-response --
        # e.g. during this harness's own shutdown -- is expected, not a bug.
        exc_type = sys.exc_info()[0]
        if exc_type and issubclass(exc_type, (ConnectionError, TimeoutError)):
            return
        super().handle_error(request, client_address)


def make_handler(state):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass  # this harness's own prints are the signal, not raw HTTP noise

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
                    deadline = time.monotonic() + 0.3  # short poll, not a real 30s long-poll
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


def stream_into(pipe, detector, echo_prefix):
    """Feed a subprocess pipe's lines into `detector` as they arrive,
    printing each one so a human watching this script's output sees the
    bot's activity live. Runs in its own thread; exits when the pipe closes."""
    for line in iter(pipe.readline, ""):
        line = line.rstrip("\n")
        print(f"[{echo_prefix}] {line}")
        if echo_prefix == "bot":
            detector.feed(line)
    pipe.close()


def run_mock(args):
    state = FakeTelegramState()
    server = QuietThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"Fake Telegram server up on http://127.0.0.1:{port}")

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8", dir=REPO_ROOT
    ) as f:
        f.write(LEAN_CONTEXT)
        lean_context_path = Path(f.name)

    env = os.environ.copy()
    env["TELEGRAM_API_BASE"] = f"http://127.0.0.1:{port}"
    env["TELEGRAM_BOT_TOKEN"] = FAKE_TOKEN
    env["TELEGRAM_CHAT_ID"] = str(FAKE_CHAT_ID)
    env["COUNCIL_CONTEXT_FILE"] = str(lean_context_path)  # lean: skip the real db lookup
    env["PYTHONUNBUFFERED"] = "1"  # belt-and-suspenders; council_bot also forces this itself
    env["NTFY_TOPIC"] = ""  # disable pushes -- this runs often and shouldn't page the phone

    proc = subprocess.Popen(
        [sys.executable, str(REPO_ROOT / "council_bot.py")],
        cwd=str(REPO_ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
    )
    detector = RoundTripDetector()
    threading.Thread(target=stream_into, args=(proc.stdout, detector, "bot"), daemon=True).start()
    threading.Thread(target=stream_into, args=(proc.stderr, detector, "bot:err"), daemon=True).start()

    try:
        deadline = time.monotonic() + 15
        while proc.poll() is None and time.monotonic() < deadline and not detector.got_question:
            time.sleep(0.1)
        if proc.poll() is not None:
            return detector.report([f"(subprocess exited before listening, code {proc.returncode})"])

        print(f"Injecting fake message: {args.question!r}")
        state.enqueue_message(args.question)

        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline and detector.outcome is None:
            if proc.poll() is not None:
                return detector.report([f"(subprocess exited unexpectedly, code {proc.returncode})"])
            time.sleep(0.2)

        outbound = [text for _, text in state.sent_messages]
        extra = [f"Outbound messages via fake Telegram server: {len(outbound)}"]
        extra += [f"  {i}. {t[:100]}{'...' if len(t) > 100 else ''}" for i, t in enumerate(outbound, 1)]
        code = detector.report(extra)
        if code == 0 and not outbound:
            print("RESULT: FAIL -- reply logged locally but nothing arrived at the fake server.")
            code = 1
        return code
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        server.shutdown()
        lean_context_path.unlink(missing_ok=True)


# ---------------------------------------------------------------- real mode

def tail_new_lines(path, offset):
    """Return (new_text, new_offset) appended to `path` since byte `offset`.
    Missing file reads as empty, offset unchanged (nothing to report yet)."""
    if not path.exists():
        return "", offset
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        f.seek(offset)
        return f.read(), f.tell()


def run_real(args):
    load_env_local()
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        sys.exit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env.local")
    if not LOG_PATH.exists():
        sys.exit(f"{LOG_PATH.name} doesn't exist -- is council_bot.py running and logging to it?")

    log_offset = LOG_PATH.stat().st_size
    err_offset = ERR_LOG_PATH.stat().st_size if ERR_LOG_PATH.exists() else 0

    nudge = f"\U0001f9ea e2e_verify_bot.py --mode real: send any message now to run the live check ({args.timeout}s window)."
    print(nudge)
    tg_send_message(token, int(chat_id), nudge)

    detector = RoundTripDetector()
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline and detector.outcome is None:
        new_err, err_offset = tail_new_lines(ERR_LOG_PATH, err_offset)
        if new_err.strip():
            print("--- new content in council_bot.err.log (a Traceback here means it crashed) ---")
            print(new_err.rstrip())

        new_log, log_offset = tail_new_lines(LOG_PATH, log_offset)
        for line in new_log.splitlines():
            print(f"  {line}")
            detector.feed(line)

        if detector.outcome is None:
            time.sleep(1)

    return detector.report()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=["mock", "real"], default="mock")
    parser.add_argument("--timeout", type=int, default=240, help="max seconds to wait for the round trip")
    parser.add_argument("--question", default=DEFAULT_QUESTION)
    args = parser.parse_args()
    sys.exit((run_mock if args.mode == "mock" else run_real)(args))


if __name__ == "__main__":
    main()
