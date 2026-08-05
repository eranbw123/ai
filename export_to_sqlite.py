#!/usr/bin/env python3
"""Fetch conversations live via CDP (real, logged-in Chrome tab) and load them
directly into conversations.db's raw_conversations table -- no intermediate
markdown/json files, per the decision that source files aren't needed since
we're going straight to SQLite.

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


def upsert(conn, *, source, conversation_id, title, model, created_at, updated_at, raw_obj):
    raw_json = json.dumps(raw_obj, ensure_ascii=False, indent=2)
    content_hash = hashlib.sha256(raw_json.encode("utf-8")).hexdigest()

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
        filtered = claude_filter(conversations, args.after, args.before, "created", None)
        if args.limit:
            filtered = filtered[: args.limit]
        print(f"claude: {len(filtered)} of {len(conversations)} conversations to import")

        stats = {"inserted": 0, "updated": 0, "unchanged": 0, "superseded": 0, "failed": 0, "inserted_titles": []}
        for i, conv in enumerate(filtered, 1):
            if not cc.UUID_RE.match(conv["uuid"]):
                continue
            try:
                data = cconn.evaluate(cc.js_fetch_conversation(org_id, conv["uuid"]))
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
        filtered = gpt_filter(conversations, args.after, args.before, "created")
        if args.limit:
            filtered = filtered[: args.limit]
        print(f"chatgpt: {len(filtered)} of {len(conversations)} conversations to import (limit={args.limit})")

        stats = {"inserted": 0, "updated": 0, "unchanged": 0, "superseded": 0, "failed": 0, "inserted_titles": []}
        for i, conv in enumerate(filtered, 1):
            if not gc.UUID_RE.match(conv["id"]):
                continue
            try:
                data = gconn.evaluate(gc.js_fetch_conversation(token, conv["id"]))
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
    args = parser.parse_args()

    # Windows console defaults to a non-UTF-8 codepage; conversation titles
    # routinely contain characters it can't encode (Hebrew, emoji, etc.).
    # Replace rather than crash -- this only affects what's printed, never
    # what's written to the DB (raw_json/title always go in as true UTF-8).
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")

    conn = sqlite3.connect(args.db)
    conn.executescript(SCHEMA)
    try:
        if args.source == "claude":
            run_claude(conn, args)
        else:
            run_chatgpt(conn, args)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
