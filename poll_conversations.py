#!/usr/bin/env python3
"""Continuously poll Claude and ChatGPT for new conversations and load them
into conversations.db -- the "auto conversation update" mechanism.

Runs forever, one poll cycle every --interval seconds (default 60). Each
cycle calls straight into export_to_sqlite.py's run_claude()/run_chatgpt(),
so every conversation lands through the exact same upsert() dedup path a
manual `python export_to_sqlite.py <source>` run uses: UNIQUE(source,
conversation_id), a content_hash to skip no-op rewrites, and the
markdown_reconstructed-supersede logic. Nothing new to dedupe here -- this
script's only job is deciding *what date range to ask for* each cycle.

Catch-up window ("--after"), per source, independently:
  - Steady state: min(last successful poll's timestamp, now - --overlap).
    --overlap (default 2 minutes) is intentionally wider than --interval so
    a slow cycle or a slightly-late wakeup never leaves a gap -- re-fetching
    a conversation already in the DB is a cheap no-op (content_hash match),
    so overlap costs nothing but guarantees nothing is missed.
  - First run ever for a source (no state recorded yet): the most recent
    created_at already in raw_conversations for that source, so we pick up
    exactly where the last manual/full import left off. If that source has
    no rows at all yet, fall back to --bootstrap-days (default 7) back.
  - Down for a while (process was killed, laptop slept, Chrome tab closed):
    handled by the same "last successful poll" timestamp above -- it's
    loaded from poll_state.json on startup, so a gap of hours or days just
    means a wider --after on the first cycle back, not a blind spot.

A source's timestamp only advances after a cycle that *succeeds* for that
source. If the required Chrome tab isn't open (connect() raises SystemExit)
or the fetch fails outright, that source's window is left exactly where it
was and gets retried next cycle -- so a closed tab or a network blip only
delays catch-up, it never skips conversations.

State (poll_state.json) and the log this writes to are both gitignored,
same as conversations.db itself.

Usage:
    python poll_conversations.py                  # run forever, 60s cadence
    python poll_conversations.py --once            # single cycle, then exit
    python poll_conversations.py --interval 30 --port 9222
"""
import argparse
import json
import sqlite3
import sys
import time
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import export_to_sqlite as ets

REPO_ROOT = Path(__file__).resolve().parent
DB_PATH = REPO_ROOT / "conversations.db"
STATE_PATH = REPO_ROOT / "poll_state.json"

SOURCES = ("claude", "chatgpt")
DEFAULT_INTERVAL = 60  # seconds between poll cycles
DEFAULT_OVERLAP_MINUTES = 2  # always re-check at least this far back, every cycle
DEFAULT_BOOTSTRAP_DAYS = 7  # first-ever run for a source with zero rows in the db


def log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{ts}] {msg}", flush=True)


def load_state():
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            log(f"poll_state.json unreadable ({e}), starting fresh")
    return {}


def save_state(state):
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(STATE_PATH)  # atomic on both POSIX and Windows


def db_max_created_at(conn, source):
    row = conn.execute(
        "SELECT MAX(created_at) FROM raw_conversations WHERE source = ?", (source,)
    ).fetchone()
    if row and row[0]:
        return datetime.fromisoformat(row[0])
    return None


def compute_after(conn, source, state, now, *, overlap, bootstrap_days):
    """Returns the tz-aware datetime to pass as --after for this source this cycle."""
    last_success = (state.get(source) or {}).get("last_success_at")
    if last_success:
        return min(datetime.fromisoformat(last_success), now - overlap)
    db_max = db_max_created_at(conn, source)
    return db_max if db_max is not None else (now - timedelta(days=bootstrap_days))


def poll_once(conn, state, *, port, overlap, bootstrap_days):
    now = datetime.now(timezone.utc)
    for source in SOURCES:
        after = compute_after(conn, source, state, now, overlap=overlap, bootstrap_days=bootstrap_days)
        state.setdefault(source, {})
        args = types.SimpleNamespace(
            port=port, db=str(DB_PATH), limit=None, batch_size=10,
            notify=False, after=after, before=None,
        )
        log(f"{source}: polling since {after.isoformat()}")
        try:
            if source == "claude":
                ets.run_claude(conn, args)
            else:
                ets.run_chatgpt(conn, args)
            state[source]["last_success_at"] = now.isoformat()
            state[source]["last_error"] = None
        except SystemExit as e:
            # connect() sys.exit()s when the required tab (claude.ai /
            # chatgpt.com) isn't open in the CDP-attached Chrome -- not
            # fatal to the loop, just sit out this cycle and retry next time.
            log(f"{source}: no tab available ({e}) -- will retry next cycle")
            state[source]["last_error"] = str(e)
        except Exception as e:  # noqa: BLE001 -- loop must survive any single-cycle failure
            log(f"{source}: poll cycle failed: {e!r}")
            state[source]["last_error"] = str(e)
    save_state(state)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=9222, help="Chrome --remote-debugging-port")
    parser.add_argument("--db", default=str(DB_PATH))
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL, help="seconds between poll cycles")
    parser.add_argument("--overlap-minutes", type=float, default=DEFAULT_OVERLAP_MINUTES)
    parser.add_argument("--bootstrap-days", type=int, default=DEFAULT_BOOTSTRAP_DAYS)
    parser.add_argument("--once", action="store_true", help="run a single cycle then exit (for testing)")
    args = parser.parse_args()

    # Same rationale as export_to_sqlite.py/council_bot.py: this runs long-lived
    # and redirected to a log file, so force UTF-8 + line buffering up front
    # rather than crash on the first Hebrew/emoji title or sit unflushed.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

    overlap = timedelta(minutes=args.overlap_minutes)
    conn = sqlite3.connect(args.db, timeout=30)  # timeout = retry window for "database is locked"
    conn.executescript(ets.SCHEMA)
    state = load_state()

    log(f"poll_conversations starting (interval={args.interval}s, overlap={args.overlap_minutes}m, "
        f"bootstrap={args.bootstrap_days}d, db={args.db})")
    try:
        if args.once:
            poll_once(conn, state, port=args.port, overlap=overlap, bootstrap_days=args.bootstrap_days)
            return
        while True:
            cycle_start = time.monotonic()
            poll_once(conn, state, port=args.port, overlap=overlap, bootstrap_days=args.bootstrap_days)
            elapsed = time.monotonic() - cycle_start
            time.sleep(max(0.0, args.interval - elapsed))
    except KeyboardInterrupt:
        log("stopping (KeyboardInterrupt)")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
