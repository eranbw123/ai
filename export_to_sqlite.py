#!/usr/bin/env python3
"""Fetch conversations live via CDP (real, logged-in Chrome tab) and load them
directly into conversations.db's raw_conversations table -- no intermediate
markdown/json files, per the decision that source files aren't needed since
we're going straight to SQLite.

--after/--before filter on updated_at, not created_at, so a conversation
that already existed but got a new message since --after is picked up too,
not just brand-new ones -- upsert() below detects the actual content change
via content_hash either way; this only controls what's considered a
candidate in the first place.

Reuses the existing connect()/fetch_all_conversation_summaries()/js_fetch_conversation()
from claude_export_cdp.py and chatgpt_export_cdp.py untouched -- only the sink
changes (DB upsert instead of write_export() to a file).

Dedup / supersede logic:
    The conversations already backfilled from markdown (migrate_md_to_sqlite.py)
    got a synthetic conversation_id derived from the title, because the .md
    files never preserved the real API uuid/id. A live fetch of that same
    conversation gets its true id and would look like an unrelated new row
    under the (source, conversation_id) UNIQUE constraint. So before inserting
    a live row, we look for a 'markdown_reconstructed' row with the same
    (source, title, created_at) and delete it -- the live 'api_json' row,
    with full raw API fidelity, supersedes it.

Usage:
    python export_to_sqlite.py claude
    python export_to_sqlite.py chatgpt --limit 50 --batch-size 10
"""
import argparse
import contextlib
import hashlib
import json
import os
import sqlite3
import sys
import time
import urllib.request
from datetime import timezone
from pathlib import Path

import claude_export_cdp as cc
import chatgpt_export_cdp as gc
from claude_export import filter_conversations as claude_filter, parse_api_timestamp as claude_parse_ts
from chatgpt_export import filter_conversations as gpt_filter
from common import parse_date

REPO_ROOT = Path(__file__).resolve().parent
DB_PATH = REPO_ROOT / "conversations.db"
IMPORT_LOCK_PATH = REPO_ROOT / "conversations.db.import-lock"


class ImportLockError(RuntimeError):
    pass


@contextlib.contextmanager
def import_lock(lock_path=IMPORT_LOCK_PATH):
    """Exclusive lock held for the duration of a `python export_to_sqlite.py
    <source>` CLI run, so two full-import invocations can never run at once.

    Real incident, 2026-08-05: two separate Claude Code sessions each kicked
    off a full ChatGPT re-import against the same live conversations.db at
    the same time, unaware of each other -- doubling the request volume to
    chatgpt.com and making the rate-limited/empty responses that triggered
    the actual data-loss bug (see is_valid_conversation_payload) far more
    likely. This lock doesn't replace that fix, but two independent
    long-running full-import loops hammering the same source concurrently
    was never a scenario worth allowing.

    Deliberately scoped to the CLI entry point (main()) only -- NOT applied
    to poll_conversations.py's regular calls into run_claude()/run_chatgpt(),
    which are a separate, already-tested case: a low-volume background
    poller coexisting with an occasional manual full import is exactly what
    conversations.db's WAL journal mode + 60s busy_timeout were verified live
    to handle safely (see poll_conversations.py's connect() comments). This
    lock is about two *manual full-import runs* stacking, not about the
    poller ever needing to wait for one.

    Fails fast rather than waiting/queuing -- two full imports colliding is
    exactly the kind of thing that should surface immediately, not run
    silently in the background on top of each other. If a previous run
    crashed and left this file behind, delete it by hand once you've
    confirmed nothing else is actually running.
    """
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise ImportLockError(
            f"{lock_path} already exists -- another export_to_sqlite.py import appears to be "
            f"running. If you're sure that's not the case (e.g. it crashed and left this file "
            f"behind), delete it and try again."
        )
    try:
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        yield
    finally:
        with contextlib.suppress(FileNotFoundError):
            lock_path.unlink()


SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_conversations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source          TEXT    NOT NULL CHECK (source IN ('claude', 'chatgpt')),
    conversation_id TEXT    NOT NULL,
    title           TEXT,
    model           TEXT,
    created_at      TEXT,
    updated_at      TEXT,
    raw_json        TEXT    NOT NULL,
    raw_source      TEXT    NOT NULL DEFAULT 'api_json'
                    CHECK (raw_source IN ('api_json', 'markdown_reconstructed')),
    content_hash    TEXT    NOT NULL,
    imported_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (source, conversation_id)
);
CREATE INDEX IF NOT EXISTS idx_raw_conversations_created ON raw_conversations(created_at);
CREATE INDEX IF NOT EXISTS idx_raw_conversations_source  ON raw_conversations(source);
"""


# Same default topic council_bot.py's ntfy_notify() uses, so progress pings
# land in the same phone subscription as everything else. Override/disable
# the same way: NTFY_TOPIC env var ("" disables).
DEFAULT_NTFY_TOPIC = "claude-code-4a93bd81fbc78f6974701714"


def ntfy_notify(title, message, priority="default", tags=None):
    """Best-effort push via ntfy.sh. Never raises -- must not take down the import."""
    topic = os.environ.get("NTFY_TOPIC", DEFAULT_NTFY_TOPIC)
    if not topic:
        return
    base = os.environ.get("NTFY_BASE", "https://ntfy.sh")
    url = f"{base}/{topic}"
    headers = {"Title": title, "Priority": priority}
    if tags:
        headers["Tags"] = tags
    req = urllib.request.Request(url, data=message.encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
    except Exception as e:  # noqa: BLE001
        print(f"ntfy notify failed: {e}", file=sys.stderr)


def norm_ts(dt):
    return dt.astimezone(timezone.utc).isoformat() if dt else None


def find_chatgpt_model(data):
    for node in (data.get("mapping") or {}).values():
        msg = node.get("message") or {}
        slug = (msg.get("metadata") or {}).get("model_slug")
        if slug:
            return slug
    return None


_HASH_SCALAR_TYPES = (str, int, float, bool, type(None))


def _canonicalize_for_hash(value):
    """Recursively normalize a JSON-able structure before hashing: sorts any
    list whose elements are all plain scalars (str/int/float/bool/None).

    ChatGPT (and possibly Claude) return some "bookkeeping" lists -- e.g.
    safe_urls -- in a different element order on every single fetch, even
    when nothing about the conversation actually changed. Verified live: two
    back-to-back fetches of the same, untouched conversation differed only
    in safe_urls' order, which made content_hash flap on every poll cycle
    regardless of whether a real edit happened.

    Lists containing dicts or nested lists (chat_messages, mapping nodes,
    message content parts) are left exactly as returned -- their order can
    carry real meaning (message sequence) and isn't safe to reorder
    generically. Dict key order doesn't need handling here since
    json.dumps(..., sort_keys=True) at the call site already normalizes it.
    """
    if isinstance(value, dict):
        return {k: _canonicalize_for_hash(v) for k, v in value.items()}
    if isinstance(value, list):
        normalized = [_canonicalize_for_hash(v) for v in value]
        if all(isinstance(v, _HASH_SCALAR_TYPES) for v in normalized):
            return sorted(normalized, key=lambda v: (str(type(v)), str(v)))
        return normalized
    return value


def compute_content_hash(raw_obj):
    canonical = json.dumps(_canonicalize_for_hash(raw_obj), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


_MESSAGES_KEY_BY_SOURCE = {"claude": "chat_messages", "chatgpt": "mapping"}

# Must match council_bot.py's own conv_name = f"Council: {question}" exactly
# (see is_council_bot_scratch_conversation()'s docstring below).
COUNCIL_BOT_TITLE_PREFIX = "Council: "


def is_council_bot_scratch_conversation(title):
    """True if `title` looks like one of council_bot.py's own throwaway
    per-question claude.ai conversations (browser backend only), rather than
    a real conversation worth importing.

    council_bot.py creates one of these for every Telegram question and
    best-effort-deletes it when done (failures are silently swallowed, by
    design -- cleanup must never block sending the actual answer). When that
    delete fails or races with a poll cycle, the scratch conversation lingers
    and used to get imported like any other conversation -- which was both
    noise (an irrelevant "N new claude conversations" notification for pure
    bot housekeeping) and a real correctness bug: council_bot's own
    pick_random_conversation() samples a random past conversation as context
    for the *next* question, so a leftover one here could hand a future
    council deliberation its own past deliberation as "context".
    """
    return bool(title) and title.startswith(COUNCIL_BOT_TITLE_PREFIX)


def is_valid_conversation_payload(source, data):
    """True if `data` looks like a real conversation object worth storing, not
    an empty/garbage response from a flaky or rate-limited fetch.

    Real incident, 2026-08-05: chatgpt.com occasionally returned an empty
    response ({}) for a per-conversation detail fetch under load. run_chatgpt()
    used to pass that straight to upsert() unvalidated -- and since it reads
    conv["id"] (from the separate list-page summary) rather than data["id"] for
    the conversation id, an empty `data` sailed through and silently overwrote
    the existing row's raw_json with '{}'. By the time this was caught, 1441 of
    1626 ChatGPT conversations' local copies had been destroyed this way (the
    underlying chatgpt.com data was untouched -- only our local mirror lost
    it). run_claude() happened to survive by accident (it indexes data["uuid"],
    which raises on {} and gets caught as an ordinary per-item failure); this
    check makes both paths safe on purpose instead of by luck.

    Deliberately narrow: just checks the one field that would have caught the
    actual incident (a non-empty message list), not full schema validation.
    """
    if not isinstance(data, dict) or not data:
        return False
    key = _MESSAGES_KEY_BY_SOURCE.get(source)
    return bool(data.get(key)) if key else True


def fetch_conversation_with_retry(fetch_fn, *, source, label, max_attempts=4, backoff_base=3):
    """Call fetch_fn() (a zero-arg callable doing the actual per-conversation
    CDP fetch) and retry with linear backoff (3s, 6s, 9s, ...) whenever the
    response doesn't look like a real conversation (see
    is_valid_conversation_payload) -- an empty/malformed response under load is
    a real, observed failure mode (see that function's docstring), not a
    hypothetical one. Raises RuntimeError if every attempt comes back invalid,
    which callers should treat as an ordinary per-item failure (retry next
    cycle), never as something to write to the db. A fetch_fn exception
    propagates immediately without retrying here -- that already means
    something more specific went wrong and the caller's own except handles it.

    backoff_base was originally 10 (10s/20s/30s); dropped to 3 after a live
    full-history recovery run showed retries succeeding reliably well within
    that window -- the point of the wait is to ride out a transient
    rate-limit/glitch, not to punish it, and 10s/20s/30s across ~1 in 5 of
    1600+ conversations made a full re-import take hours for no added safety.
    """
    for attempt in range(1, max_attempts + 1):
        data = fetch_fn()
        if is_valid_conversation_payload(source, data):
            return data
        print(f"  empty/invalid response for {label} (attempt {attempt}/{max_attempts})", file=sys.stderr)
        if attempt < max_attempts:
            wait = backoff_base * attempt
            print(f"  retrying {label} after {wait}s", file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError(f"empty/invalid response for {label} after {max_attempts} attempts")


def upsert(conn, *, source, conversation_id, title, model, created_at, updated_at, raw_obj):
    # Defense-in-depth: fetch_conversation_with_retry() above is the primary
    # guard, but upsert() refuses on principle too -- this is exactly the bug
    # that silently destroyed 1441 ChatGPT conversations (see
    # is_valid_conversation_payload's docstring), and no caller should be able
    # to bypass it, today or in some future call site that forgets the retry
    # wrapper. Raises rather than silently skipping so it surfaces as a loud
    # per-item "failed" in the caller's stats, not a quiet no-op.
    if not is_valid_conversation_payload(source, raw_obj):
        raise ValueError(
            f"refusing to write empty/invalid conversation payload for {source}/{conversation_id}"
        )
    raw_json = json.dumps(raw_obj, ensure_ascii=False, indent=2)  # stored verbatim -- full raw API fidelity
    content_hash = compute_content_hash(raw_obj)  # hashed from a canonicalized copy -- see above

    superseded = conn.execute(
        """DELETE FROM raw_conversations
           WHERE source = ? AND raw_source = 'markdown_reconstructed'
             AND title = ? AND created_at = ? AND conversation_id != ?""",
        (source, title, created_at, conversation_id),
    ).rowcount

    existing = conn.execute(
        "SELECT content_hash FROM raw_conversations WHERE source = ? AND conversation_id = ?",
        (source, conversation_id),
    ).fetchone()

    if existing is None:
        conn.execute(
            """INSERT INTO raw_conversations
               (source, conversation_id, title, model, created_at, updated_at,
                raw_json, raw_source, content_hash)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'api_json', ?)""",
            (source, conversation_id, title, model, created_at, updated_at, raw_json, content_hash),
        )
        return "inserted", superseded
    elif existing[0] != content_hash:
        conn.execute(
            """UPDATE raw_conversations
               SET title = ?, model = ?, created_at = ?, updated_at = ?,
                   raw_json = ?, raw_source = 'api_json',
                   content_hash = ?, imported_at = datetime('now')
               WHERE source = ? AND conversation_id = ?""",
            (title, model, created_at, updated_at, raw_json, content_hash, source, conversation_id),
        )
        return "updated", superseded
    return "unchanged", superseded


def run_claude(conn, args):
    org_id = cc.require_org_id()
    cconn = cc.connect(args.port)
    try:
        conversations = cc.fetch_all_conversation_summaries(cconn, org_id, after=args.after)
        # date_field="updated", not "created": a conversation that already existed
        # before --after but got a new message since is still a real change we need
        # to catch -- upsert() below already detects it correctly via content_hash,
        # but only if it's not filtered out first. fetch_all_conversation_summaries()
        # already pages based on updated_at (see its docstring), so this is the only
        # place "created" was narrowing things back down. Filtering by "updated"
        # instead means the untouched-since-last-poll majority still comes back as a
        # cheap "unchanged" no-op (same content_hash), not wasted work.
        filtered = claude_filter(conversations, args.after, args.before, "updated", None)
        if args.limit:
            filtered = filtered[: args.limit]
        print(f"claude: {len(filtered)} of {len(conversations)} conversations to import")

        stats = {"inserted": 0, "updated": 0, "unchanged": 0, "superseded": 0, "failed": 0,
                  "inserted_titles": [], "updated_titles": []}
        for i, conv in enumerate(filtered, 1):
            if not cc.UUID_RE.match(conv["uuid"]):
                continue
            if is_council_bot_scratch_conversation(conv.get("name")):
                # council_bot.py's browser backend creates one of these per
                # question (see its own conv_name) and best-effort-deletes it
                # afterward -- when that delete fails/races, the scratch
                # conversation lingers on claude.ai and would otherwise get
                # imported like any other conversation. That's not just
                # notification noise: council_bot's own pick_random_conversation()
                # samples a random past conversation as context for the *next*
                # question, so a leftover one here could hand a future council
                # deliberation its own past deliberation as "context". Skip it
                # here rather than filtering it out downstream, so it never
                # enters conversations.db in the first place.
                continue
            try:
                data = fetch_conversation_with_retry(
                    lambda: cconn.evaluate(cc.js_fetch_conversation(org_id, conv["uuid"])),
                    source="claude", label=conv["uuid"],
                )
                outcome, superseded = upsert(
                    conn,
                    source="claude",
                    conversation_id=data["uuid"],
                    title=data.get("name"),
                    model=data.get("model"),
                    created_at=norm_ts(claude_parse_ts(data["created_at"])) if data.get("created_at") else None,
                    updated_at=norm_ts(claude_parse_ts(data["updated_at"])) if data.get("updated_at") else None,
                    raw_obj=data,
                )
                stats[outcome] += 1
                stats["superseded"] += superseded
                if outcome == "inserted":
                    stats["inserted_titles"].append(data.get("name") or "(untitled)")
                elif outcome == "updated":
                    stats["updated_titles"].append(data.get("name") or "(untitled)")
                print(f"[{i}/{len(filtered)}] {outcome} ({'superseded old row, ' if superseded else ''}{data.get('name')!r})")
                if i % args.batch_size == 0:
                    conn.commit()
                time.sleep(0.5)
            except Exception as e:  # noqa: BLE001
                stats["failed"] += 1
                print(f"[{i}/{len(filtered)}] FAILED {conv.get('uuid')}: {e}", file=sys.stderr)
        conn.commit()
        print(f"claude done: {stats}")
        return stats
    finally:
        cconn.close()


def run_chatgpt(conn, args):
    gconn = gc.connect(args.port)
    try:
        token = gconn.evaluate(gc.js_get_access_token())
        conversations = gc.fetch_all_conversation_summaries(gconn, token, after=args.after)
        # date_field="updated" -- see the matching comment in run_claude() above.
        filtered = gpt_filter(conversations, args.after, args.before, "updated")
        if args.limit:
            filtered = filtered[: args.limit]
        print(f"chatgpt: {len(filtered)} of {len(conversations)} conversations to import (limit={args.limit})")

        stats = {"inserted": 0, "updated": 0, "unchanged": 0, "superseded": 0, "failed": 0,
                  "inserted_titles": [], "updated_titles": []}
        for i, conv in enumerate(filtered, 1):
            if not gc.UUID_RE.match(conv["id"]):
                continue
            try:
                data = fetch_conversation_with_retry(
                    lambda: gconn.evaluate(gc.js_fetch_conversation(token, conv["id"])),
                    source="chatgpt", label=conv["id"],
                )
                created_at = norm_ts(gc.parse_api_timestamp(data.get("create_time")))
                updated_at = norm_ts(gc.parse_api_timestamp(data.get("update_time")))
                # The per-conversation detail fetch (data) returns title=null for
                # conversations too fresh for ChatGPT to have finished generating a
                # title server-side yet, even though the list page (conv) already has
                # it -- verified live (two real freshly-created test conversations
                # both hit this). Prefer the detail title when present (kept in sync
                # going forward), fall back to the list summary's rather than
                # storing/reporting a needlessly wrong "(untitled)".
                title = data.get("title") or conv.get("title")
                outcome, superseded = upsert(
                    conn,
                    source="chatgpt",
                    conversation_id=conv["id"],
                    title=title,
                    model=find_chatgpt_model(data),
                    created_at=created_at,
                    updated_at=updated_at,
                    raw_obj=data,
                )
                stats[outcome] += 1
                stats["superseded"] += superseded
                if outcome == "inserted":
                    stats["inserted_titles"].append(title or "(untitled)")
                elif outcome == "updated":
                    stats["updated_titles"].append(title or "(untitled)")
                print(f"[{i}/{len(filtered)}] {outcome} ({'superseded old row, ' if superseded else ''}{title!r})")
                if i % args.batch_size == 0:
                    conn.commit()
                    print(f"  -- committed batch through {i} --")
                    if args.notify:
                        ntfy_notify(
                            "ChatGPT import progress",
                            f"{i}/{len(filtered)} processed -- "
                            f"{stats['inserted']} new, {stats['updated']} updated, "
                            f"{stats['superseded']} superseded, {stats['failed']} failed",
                            priority="low",
                            tags="arrows_counterclockwise",
                        )
                time.sleep(0.5)
            except Exception as e:  # noqa: BLE001
                stats["failed"] += 1
                print(f"[{i}/{len(filtered)}] FAILED {conv.get('id')}: {e}", file=sys.stderr)
        conn.commit()
        print(f"chatgpt done: {stats}")
        if args.notify:
            ntfy_notify(
                "ChatGPT import finished",
                f"Done: {len(filtered)} processed -- {stats}",
                priority="default",
                tags="white_check_mark",
            )
        return stats
    finally:
        gconn.close()


def rehash_all_content_hashes(conn):
    """One-time, local-only migration: recompute content_hash for every row
    using compute_content_hash()'s canonicalized scheme, from the raw_json
    already stored -- no network calls, nothing else touched.

    Needed once, right after the canonicalization fix ships: without this,
    the next time a poll cycle (re-)touches an old row, it would look
    "updated" purely because the hash *algorithm* changed, not because
    anything about the conversation actually did -- and since updates now
    trigger a silent ntfy ping too, that would mean a burst of false
    "conversation updated" pings for every conversation the poller happens
    to touch first, exactly the noise this fix exists to prevent. raw_json,
    title, timestamps, and imported_at are all left exactly as they are.

    Returns (changed, total).
    """
    rows = conn.execute("SELECT id, raw_json, content_hash FROM raw_conversations").fetchall()
    changed = 0
    for row_id, raw_json, old_hash in rows:
        new_hash = compute_content_hash(json.loads(raw_json))
        if new_hash != old_hash:
            conn.execute("UPDATE raw_conversations SET content_hash = ? WHERE id = ?", (new_hash, row_id))
            changed += 1
    conn.commit()
    return changed, len(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=9222)
    parser.add_argument("--db", default=str(DB_PATH))
    parser.add_argument("--limit", type=int, default=None, help="Max conversations to import this run")
    parser.add_argument("--batch-size", type=int, default=10, help="Commit every N conversations")
    parser.add_argument("--notify", action="store_true", help="Push an ntfy.sh update after every batch")
    parser.add_argument("--after", type=parse_date)
    parser.add_argument("--before", type=parse_date)
    sub = parser.add_subparsers(dest="source", required=True)
    sub.add_parser("claude")
    sub.add_parser("chatgpt")
    sub.add_parser("rehash", help="One-time local migration: recompute content_hash for every "
                                   "existing row after a hashing-scheme change. No network calls.")
    args = parser.parse_args()

    # Windows console defaults to a non-UTF-8 codepage; conversation titles
    # routinely contain characters it can't encode (Hebrew, emoji, etc.).
    # Replace rather than crash -- this only affects what's printed, never
    # what's written to the DB (raw_json/title always go in as true UTF-8).
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")

    lock_path = Path(str(args.db) + ".import-lock")
    with import_lock(lock_path):
        conn = sqlite3.connect(args.db)
        conn.executescript(SCHEMA)
        try:
            if args.source == "claude":
                run_claude(conn, args)
            elif args.source == "chatgpt":
                run_chatgpt(conn, args)
            else:
                changed, total = rehash_all_content_hashes(conn)
                print(f"rehash done: {changed}/{total} rows had their content_hash recomputed")
        finally:
            conn.close()


if __name__ == "__main__":
    main()
