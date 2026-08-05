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

A source's timestamp only advances after a cycle with zero failures for that
source -- run_claude()/run_chatgpt() catch failures per-item internally and
keep going, so a whole-cycle try/except isn't enough to notice one; we check
the returned stats dict's "failed" count instead. If the required Chrome tab
isn't open (connect() raises SystemExit), the fetch fails outright, or even
a single item in an otherwise-fine cycle fails (e.g. "database is locked"
colliding with a concurrent manual import), that source's window is left
exactly where it was and gets retried next cycle -- so a closed tab, a
network blip, or a transient per-item error only delays catch-up, it never
silently skips a conversation.

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


MAX_TITLES_IN_NOTIFICATION = 10  # ntfy messages have no hard limit, but keep pings skimmable


def notify_new(source, stats):
    """Silent (low-priority, no alert sound) ntfy.sh ping whenever a cycle actually
    added something new -- same topic/plumbing as export_to_sqlite.py's own
    progress notifications, and same "low = silent" convention council_bot.py
    uses for its own routine pings. Never called for a source that only saw
    unchanged/updated rows -- just genuinely new conversations. Lists each new
    conversation's title (run_claude()/run_chatgpt() collect these in
    stats["inserted_titles"]) so the ping is actually useful at a glance
    rather than just a bare count."""
    inserted = stats.get("inserted", 0)
    if inserted <= 0:
        return
    titles = stats.get("inserted_titles", [])
    shown = titles[:MAX_TITLES_IN_NOTIFICATION]
    lines = "\n".join(f"- {t}" for t in shown)
    if len(titles) > len(shown):
        lines += f"\n- (+{len(titles) - len(shown)} more)"
    message = f"{inserted} new {source} conversation{'s' if inserted != 1 else ''}:\n{lines}" if lines \
        else f"{inserted} new {source} conversation{'s' if inserted != 1 else ''} added to conversations.db"
    ets.ntfy_notify(
        f"New {source} conversation{'s' if inserted != 1 else ''}",
        message,
        priority="low",
        tags="speech_balloon",
    )


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
                stats = ets.run_claude(conn, args)
            else:
                stats = ets.run_chatgpt(conn, args)
            if stats.get("failed", 0) == 0:
                # Only advance the window on a clean cycle. A per-item failure
                # (e.g. "database is locked" colliding with a concurrent manual
                # import) must NOT move --after forward -- otherwise that
                # conversation's created_at falls before the next cycle's
                # window and it's silently skipped forever. Leaving the
                # timestamp put means the same (now wider) window gets
                # retried next cycle until it succeeds. Verified live: this
                # is exactly what happened to a real conversation before this
                # check existed.
                state[source]["last_success_at"] = now.isoformat()
            else:
                log(f"{source}: {stats['failed']} item(s) failed this cycle -- "
                    f"window left at {after.isoformat()}, will retry next cycle")
            state[source]["last_error"] = None
            notify_new(source, stats)
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
    # timeout = retry window for "database is locked" (busy_timeout). 60s because this
    # DB also gets written by manual `export_to_sqlite.py` full-import runs, which can
    # hold a write transaction open for tens of seconds across many inserts between
    # commits (--batch-size); WAL shortens that window further -- unlike the default
    # rollback-journal mode, a WAL writer doesn't need to rewrite the whole journal per
    # commit, and readers never block it either. Two concurrent *writers* still
    # serialize (WAL doesn't remove that), but each one's lock is held for much less
    # time, so 60s of retrying comfortably rides out a collision either way. Verified
    # live: without this, a concurrent full backfill import caused a "database is
    # locked" failure on a poll cycle within the first minute of running.
    conn = sqlite3.connect(args.db, timeout=60)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError as e:
        # Switching journal modes needs exclusive access to the file, which
        # doesn't go through the normal busy_timeout retry loop -- it just
        # fails immediately if another process (e.g. a concurrent manual
        # export_to_sqlite.py run) already has the db open. Non-fatal: stay
        # on the default rollback-journal mode for this run; the 60s
        # busy_timeout above still covers ordinary "database is locked"
        # contention on reads/writes, just with a longer average wait than
        # WAL would give. Verified live: this exact collision crashed the
        # poller at startup before this fallback existed.
        log(f"could not switch to WAL journal mode ({e}), continuing without it")
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
