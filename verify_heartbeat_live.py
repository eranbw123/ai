#!/usr/bin/env python3
"""Live, on-demand end-to-end check of council_bot's heartbeat + reply path.

This is NOT a unit test and is NOT meant to run in a pre-commit hook or CI --
it drives the real, already-running council_bot.py process against real
Telegram traffic and a real claude.ai tab, which is slow (minutes, not
seconds) and depends on external services being up. test_council_bot.py's
offline tests cover the wiring logic on every commit cheaply; this script
covers what those tests structurally cannot: that the real OS environment
(console codepage, log buffering) and the real claude.ai response shape
actually work together end to end. Run it by hand after any change to
council_bot.py's message-handling path, before considering the change done.

Why this doesn't poll Telegram itself: council_bot.py is (or should be)
already running and long-polling getUpdates on the same bot token. A second
getUpdates consumer on that token would race it for updates and could steal
the very message this script is waiting for. So instead this script only
ever *sends* a message (sendMessage doesn't conflict with anyone's
getUpdates offset) and then watches council_bot.py's own log file for the
resulting activity -- the same thing a human tailing the log would do.

Usage:
    python verify_heartbeat_live.py [--timeout SECONDS]

Exit code 0 = pass, 1 = fail (see the printed report either way).
"""
import argparse
import sys
import time
from pathlib import Path

from common import REPO_ROOT, load_env_local
from council_bot import tg_send_message

LOG_PATH = REPO_ROOT / "council_bot.log"
ERR_LOG_PATH = REPO_ROOT / "council_bot.err.log"
POLL_INTERVAL = 1  # seconds, how often this script re-reads the log files


def tail_new_lines(path, offset):
    """Return (new_text, new_offset) appended to `path` since byte `offset`.
    Missing file reads as empty, offset unchanged (nothing to report yet)."""
    if not path.exists():
        return "", offset
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        f.seek(offset)
        new_text = f.read()
        return new_text, f.tell()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--timeout", type=int, default=360,
        help="Max seconds to wait for the full round trip (default 360 -- "
             "browser-backend generation alone commonly takes 100-150s)",
    )
    args = parser.parse_args()

    load_env_local()
    import os
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        sys.exit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env.local")
    chat_id = int(chat_id)

    if not LOG_PATH.exists():
        sys.exit(
            f"{LOG_PATH.name} doesn't exist yet -- is council_bot.py running "
            "and logging to it? Start it first (see council_bot.py's docstring)."
        )

    log_offset = LOG_PATH.stat().st_size
    err_offset = ERR_LOG_PATH.stat().st_size if ERR_LOG_PATH.exists() else 0

    nudge = (
        "\U0001f9ea verify_heartbeat_live.py: send any message now to run the "
        "live heartbeat check (I'm watching council_bot.log)."
    )
    print(nudge)
    tg_send_message(token, chat_id, nudge)

    print(f"Waiting up to {args.timeout}s for the round trip...")
    seen_question = False
    heartbeat_pings = 0
    outcome = None  # "sent" | "failed" | None (timed out)
    outcome_line = None
    log_buf = ""
    deadline = time.monotonic() + args.timeout

    while time.monotonic() < deadline and outcome is None:
        new_log, log_offset = tail_new_lines(LOG_PATH, log_offset)
        new_err, err_offset = tail_new_lines(ERR_LOG_PATH, err_offset)

        if new_err.strip():
            print("--- new content in council_bot.err.log ---")
            print(new_err.rstrip())
            print("--- (a Traceback here means the process crashed) ---")

        if new_log:
            log_buf += new_log
            for line in new_log.splitlines():
                print(f"  {line}")
                if line.startswith("Question:") and not seen_question:
                    seen_question = True
                elif line.startswith("[heartbeat] ping sent"):
                    heartbeat_pings += 1
                elif line.startswith("Sent reply to Telegram."):
                    outcome, outcome_line = "sent", line
                elif line.startswith("Council failed:"):
                    outcome, outcome_line = "failed", line

        if outcome is None:
            time.sleep(POLL_INTERVAL)

    print()
    print("=" * 60)
    if not seen_question:
        print("FAIL: no incoming message was ever logged by council_bot.")
        print("      Did you send a message? Is council_bot.py actually running")
        print("      and pointed at the same TELEGRAM_CHAT_ID?")
        sys.exit(1)

    if outcome is None:
        print(f"FAIL: question was received but no reply within {args.timeout}s.")
        print("      Check council_bot.err.log for a crash (a silent process")
        print("      death -- e.g. an unhandled exception outside the per-")
        print("      message try/except -- looks exactly like this: no")
        print("      'Council failed:' line, just... nothing).")
        sys.exit(1)

    print(f"Question received: yes")
    print(f"Heartbeat pings seen: {heartbeat_pings}")
    print(f"Outcome: {outcome_line}")

    if outcome == "failed":
        print("RESULT: council run completed but reported a failure (see line above).")
        print("        Not necessarily a wiring bug -- could be a real claude.ai/CDP error.")
        sys.exit(1)

    print("RESULT: PASS -- question in, reply out, no crash.")
    if heartbeat_pings == 0:
        print("NOTE: zero heartbeat pings fired. Fine if the reply came back in under")
        print(f"      HEARTBEAT_INTERVAL seconds; suspicious otherwise -- check the timing.")
    sys.exit(0)


if __name__ == "__main__":
    main()
