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
    python export_to_sqlite.py chatgpt --max-runtime-minutes 25  # bail cleanly after 25m, rerun to resume
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
from collections import deque
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


def progress_summary(i, total, start_time, stats):
    """Human-readable 'Nm elapsed, X.X/min, ~Ym left, R retries so far' fragment
    for notifications -- built once here so the starting/progress/stopped/done
    messages all report the same numbers the same way.

    rate is computed from wall-clock elapsed time, not a fixed per-item
    estimate, specifically so it reflects real throttling (chatgpt.com making
    every fetch_conversation_with_retry() call burn through several 10/20/30s
    backoff waits) rather than looking artificially healthy.
    """
    elapsed_min = (time.monotonic() - start_time) / 60
    rate = i / elapsed_min if elapsed_min > 0 else 0
    remaining = total - i
    eta = f", ~{remaining / rate:.0f}m left" if rate > 0 else ""
    return (f"{elapsed_min:.0f}m elapsed, {rate:.1f}/min{eta}, "
            f"{stats.get('retries', 0)} retries so far")


def adaptive_pace(recent_retry_counts, *, base=0.5, max_retries=3):
    """Seconds to sleep before the next detail fetch, scaled to how much
    retrying the last few items needed -- eases off chatgpt.com's rate
    limiter instead of hammering it at the same flat cadence regardless of
    how throttled it obviously already is.

    Real incident, 2026-08-05/06: repeated same-day full-history passes (see
    fetch_conversation_with_retry's docstring) degraded chatgpt.com's
    response rate from 0 retries needed across 1625 items (the first pass of
    the day) to nearly every single item needing 3/3 retries by the next
    morning, at a flat 0.5s pace throughout -- the pace never adapted to the
    obviously-worsening throttling, so the run kept adding to the same
    pressure that caused it. `recent_retry_counts` holds each of the last
    handful of items' retry count (0..max_retries); once the recent average
    gets close to max_retries (nearly everything failing until the last
    attempt), that's not noise to retry through faster, it's a clear signal
    to slow down harder.
    """
    if not recent_retry_counts:
        return base
    avg = sum(recent_retry_counts) / len(recent_retry_counts)
    if avg >= max_retries * 0.75:
        return 20.0
    if avg >= max_retries * 0.4:
        return 5.0
    return base


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


def fetch_conversation_with_retry(fetch_fn, *, source, label, stats=None, max_attempts=4, backoff_base=10):
    """Call fetch_fn() (a zero-arg callable doing the actual per-conversation
    CDP fetch) and retry with linear backoff (10s, 20s, 30s, ...) whenever the
    response doesn't look like a real conversation (see
    is_valid_conversation_payload) -- an empty/malformed response under load is
    a real, observed failure mode (see that function's docstring), not a
    hypothetical one. Raises RuntimeError if every attempt comes back invalid,
    which callers should treat as an ordinary per-item failure (retry next
    cycle), never as something to write to the db. A fetch_fn exception
    propagates immediately without retrying here -- that already means
    something more specific went wrong and the caller's own except handles it.

    backoff_base was briefly dropped to 3 (3s/6s/9s) after one recovery run
    showed retries succeeding reliably within that window -- but a later run,
    stacked on top of several other full-history passes against chatgpt.com
    the same day, saw a much higher rate-limit rate: 13/24 items exhausted
    all 4 attempts and permanently failed with the short backoff, vs. ~19%
    needing any retry at all under the original 10s/20s/30s. The wait exists
    to ride out real rate-limiting, not just a glitch, and a too-short window
    trades "fast per item" for "recovers less data per pass" under load --
    reverted back to 10 for that reason.

    `stats`, if given, gets stats["retries"] incremented once per retry (not
    per attempt) -- purely so callers can report how much rate-limiting a run
    hit (see run_chatgpt()'s progress notifications), without changing what
    gets raised/returned here.
    """
    for attempt in range(1, max_attempts + 1):
        data = fetch_fn()
        if is_valid_conversation_payload(source, data):
            return data
        print(f"  empty/invalid response for {label} (attempt {attempt}/{max_attempts})", file=sys.stderr)
        if attempt < max_attempts:
            wait = backoff_base * attempt
            print(f"  retrying {label} after {wait}s", file=sys.stderr)
            if stats is not None:
                stats["retries"] = stats.get("retries", 0) + 1
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
        max_runtime_minutes = getattr(args, "max_runtime_minutes", None)
        deadline = time.monotonic() + max_runtime_minutes * 60 if max_runtime_minutes else None

        stats = {"inserted": 0, "updated": 0, "unchanged": 0, "superseded": 0, "failed": 0, "retries": 0,
                  "inserted_titles": [], "updated_titles": [], "stopped_early": False}
        for i, conv in enumerate(filtered, 1):
            if deadline is not None and time.monotonic() >= deadline:
                stats["stopped_early"] = True
                print(f"claude: hit --max-runtime-minutes budget ({max_runtime_minutes}m) "
                      f"with {len(filtered) - i + 1} left -- stopping here, rerun to resume")
                break
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
                    source="claude", label=conv["uuid"], stats=stats,
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
        max_runtime_minutes = getattr(args, "max_runtime_minutes", None)
        start_time = time.monotonic()
        deadline = start_time + max_runtime_minutes * 60 if max_runtime_minutes else None

        # conversation_id -> stored updated_at, so a conversation whose list-page
        # update_time already matches what's in the db can skip the detail fetch
        # entirely, not just the write. upsert()'s content_hash check already
        # made re-fetching harmless, but "harmless to write" isn't "free" -- every
        # full resume was re-issuing a detail request for all ~1629 conversations
        # every single time regardless of whether anything changed. Real incident,
        # 2026-08-05/06: 7+ full passes in ~16h against the same chatgpt.com
        # session/account (each one re-requesting the whole history) escalated
        # chatgpt.com's throttling from 0 retries needed across 1625 items on the
        # first pass to nearly every item needing all 3 retries by the next
        # morning (see fetch_conversation_with_retry's and adaptive_pace's
        # docstrings). update_time is already trusted as the staleness signal
        # elsewhere (poll_conversations.py's compute_after, the --after/--before
        # filtering above) -- reusing it here to skip network calls outright, not
        # just db writes, is the same trust applied one step earlier.
        existing_updated_at = dict(conn.execute(
            "SELECT conversation_id, updated_at FROM raw_conversations WHERE source = 'chatgpt'"
        ).fetchall())

        stats = {"inserted": 0, "updated": 0, "unchanged": 0, "superseded": 0, "failed": 0, "retries": 0,
                  "inserted_titles": [], "updated_titles": [], "stopped_early": False}
        if args.notify:
            ntfy_notify(
                "ChatGPT import starting",
                f"{len(filtered)} candidates to check"
                + (f" -- capped at {max_runtime_minutes:.0f}m" if max_runtime_minutes else ""),
                priority="low",
                tags="rocket",
            )
        stopped_early = False
        last_title = None
        # Rolling window of the last few items' retry counts, feeding
        # adaptive_pace() -- see its docstring for why a flat pace regardless of
        # how throttled chatgpt.com clearly is was part of the problem.
        recent_retry_counts = deque(maxlen=10)
        for i, conv in enumerate(filtered, 1):
            if deadline is not None and time.monotonic() >= deadline:
                stopped_early = True
                stats["stopped_early"] = True
                print(f"chatgpt: hit --max-runtime-minutes budget ({max_runtime_minutes}m) "
                      f"with {len(filtered) - i + 1} left -- stopping here, rerun to resume "
                      f"({progress_summary(i - 1, len(filtered), start_time, stats)})")
                break
            if not gc.UUID_RE.match(conv["id"]):
                continue
            conv_updated = norm_ts(gc.parse_api_timestamp(conv["update_time"])) if conv.get("update_time") else None
            if conv_updated is not None and existing_updated_at.get(conv["id"]) == conv_updated:
                stats["unchanged"] += 1
                last_title = conv.get("title") or last_title
                print(f"[{i}/{len(filtered)}] unchanged, cached -- no fetch ({conv.get('title')!r})")
                continue
            try:
                retries_before = stats["retries"]
                data = fetch_conversation_with_retry(
                    lambda: gconn.evaluate(gc.js_fetch_conversation(token, conv["id"])),
                    source="chatgpt", label=conv["id"], stats=stats,
                )
                recent_retry_counts.append(stats["retries"] - retries_before)
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
                last_title = title
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
                pace = adaptive_pace(recent_retry_counts)
                if i % args.batch_size == 0:
                    conn.commit()
                    print(f"  -- committed batch through {i} --")
                    if args.notify:
                        ntfy_notify(
                            "ChatGPT import progress",
                            f"{i}/{len(filtered)} checked ({progress_summary(i, len(filtered), start_time, stats)}, "
                            f"pace {pace:.1f}s/item) -- "
                            f"{stats['inserted']} new, {stats['updated']} updated, {stats['unchanged']} unchanged, "
                            f"{stats['superseded']} superseded, {stats['failed']} failed -- "
                            f"last: {title!r}",
                            priority="low",
                            tags="arrows_counterclockwise",
                        )
                time.sleep(pace)
            except Exception as e:  # noqa: BLE001
                stats["failed"] += 1
                # Count as a max-retries item too -- an outright failure after
                # exhausting every attempt is at least as strong a throttling
                # signal as a retry that eventually succeeded, and should slow
                # the next fetch down the same way (see adaptive_pace).
                recent_retry_counts.append(3)
                print(f"[{i}/{len(filtered)}] FAILED {conv.get('id')}: {e}", file=sys.stderr)
                time.sleep(adaptive_pace(recent_retry_counts))
        conn.commit()
        processed = (i - 1) if stopped_early else len(filtered)
        status = "stopped early (time budget)" if stopped_early else "done"
        print(f"chatgpt {status}: {stats}")
        if args.notify:
            summary = progress_summary(processed, len(filtered), start_time, stats) if filtered else "nothing to do"
            ntfy_notify(
                f"ChatGPT import {'paused' if stopped_early else 'finished'}",
                f"{'Stopped early, rerun to resume' if stopped_early else 'Done'}: "
                f"{processed}/{len(filtered)} checked ({summary}) -- "
                f"{stats['inserted']} new, {stats['updated']} updated, {stats['unchanged']} unchanged, "
                f"{stats['superseded']} superseded, {stats['failed']} failed, {stats['retries']} retries -- "
                f"last: {last_title!r}",
                priority="default",
                tags="hourglass_flowing_sand" if stopped_early else "white_check_mark",
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
    parser.add_argument("--max-runtime-minutes", type=float, default=None,
                         help="Stop starting new conversation fetches after this many minutes "
                              "(commits what's already done and exits cleanly -- rerun the same "
                              "command to resume, upsert() skips anything already unchanged). "
                              "Unlimited if unset.")
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
    #
    # line_buffering=True too: Python fully block-buffers stdout (not
    # line-buffers) whenever it's not a real console -- i.e. every time this
    # is run with `> log.txt` or in the background, which is the normal way
    # to run a multi-hour import. Real incident, 2026-08-06: a long chatgpt
    # import looked "stopped" for ~10 minutes -- it wasn't; every per-item
    # print() (including the "[i/N] outcome" lines) was sitting in an
    # unflushed buffer while retries kept happening underneath (those go to
    # stderr, which was already unbuffered, so only *they* showed up in the
    # log -- misleadingly making it look like nothing but retries was
    # happening). Forcing line buffering here makes the log file reflect
    # reality in real time regardless of how stdout is redirected.
    sys.stdout.reconfigure(errors="replace", line_buffering=True)
    sys.stderr.reconfigure(errors="replace", line_buffering=True)

    lock_path = Path(str(args.db) + ".import-lock")
    stopped_early = False
    with import_lock(lock_path):
        conn = sqlite3.connect(args.db)
        conn.executescript(SCHEMA)
        try:
            if args.source == "claude":
                stopped_early = run_claude(conn, args)["stopped_early"]
            elif args.source == "chatgpt":
                stopped_early = run_chatgpt(conn, args)["stopped_early"]
            else:
                changed, total = rehash_all_content_hashes(conn)
                print(f"rehash done: {changed}/{total} rows had their content_hash recomputed")
        finally:
            conn.close()

    # Exit code 3 specifically (not just "nonzero") so a supervisor loop
    # (see resilient_import.py) can tell "ran out of --max-runtime-minutes,
    # more to do -- relaunch" apart from an actual crash. 0 means either a
    # normal full pass or a source with no time budget concept (rehash).
    if stopped_early:
        sys.exit(3)


if __name__ == "__main__":
    main()
