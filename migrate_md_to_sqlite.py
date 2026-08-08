#!/usr/bin/env python3
"""One-off migration: parse the already-exported markdown conversation files
(exports_*/*.md) and load them into the raw_conversations SQLite table.

Each markdown file was produced by claude_export.py/chatgpt_export.py's
convert_to_markdown(), which uses a fixed, deterministic template:

    # {title}

    **Created:** {created}
    **Updated:** {updated}
    **Model:** {model}        <- Claude only

    ---

    **{Sender}**:

    {text}

    ---

    **{Sender}**:
    ...

Message bodies frequently contain their own "---" horizontal-rule lines (the
model's own markdown formatting), so we can't split on "---" alone -- we
anchor on the exact sender markers ("**You**:", "**Claude**:", "**ChatGPT**:")
that only ever appear as the template's own blank-line-delimited headers.

To guard against a bad parse, every file is re-serialized from its parsed
form using the *same* template and diffed against the original text; any
mismatch aborts the migration for that file (printed, not inserted) rather
than silently loading corrupted data.

This is a one-time backfill for conversations that only exist as markdown
(no raw API JSON was ever saved for them). Rows from this path are marked
raw_source='markdown_reconstructed' so a future true-raw-JSON import can
supersede them.

Usage:
    python migrate_md_to_sqlite.py [--db conversations.db] [--dry-run]
"""
import argparse
import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent

SENDER_BY_SOURCE = {
    "claude": ("You", "Claude"),
    "chatgpt": ("You", "ChatGPT"),
}
ROLE_BY_SENDER = {
    "claude": {"You": "human", "Claude": "assistant"},
    "chatgpt": {"You": "user", "ChatGPT": "assistant"},
}

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


class ParseError(Exception):
    pass


def parse_markdown_export(path: Path, source: str):
    text = path.read_text(encoding="utf-8")

    if not text.startswith("# "):
        raise ParseError("does not start with '# Title'")
    header, sep, body = text.partition("\n\n---\n\n")
    if not sep:
        raise ParseError("no header/body '---' separator found")

    header_lines = header.split("\n")
    title = header_lines[0][2:]
    created = updated = model = None
    for line in header_lines[1:]:
        if line.startswith("**Created:** "):
            created = line[len("**Created:** "):]
        elif line.startswith("**Updated:** "):
            updated = line[len("**Updated:** "):]
        elif line.startswith("**Model:** "):
            model = line[len("**Model:** "):]

    you_label, other_label = SENDER_BY_SOURCE[source]
    anchor_re = re.compile(
        r"(?:\A|\n\n)\*\*(%s|%s)\*\*:\n\n" % (re.escape(you_label), re.escape(other_label))
    )
    anchors = list(anchor_re.finditer(body))
    if not anchors:
        raise ParseError("no message sender markers found in body")

    messages = []
    for i, m in enumerate(anchors):
        sender = m.group(1)
        chunk_start = m.end()
        if i + 1 < len(anchors):
            # anchors[i+1]'s match always starts at the "\n\n" that precedes
            # its "**Sender**:" (every anchor after the first is preceded by
            # a message, never \A) -- that "\n\n" belongs to *this* message's
            # trailing "\n\n---\n\n", so include it in this chunk rather than
            # letting the next match's span swallow it.
            chunk_end = anchors[i + 1].start() + 2
        else:
            chunk_end = len(body)
        chunk = body[chunk_start:chunk_end]
        # Each message's raw text is followed by its trailing "\n\n---\n\n"
        # (or, for the very last message, "\n\n---\n\n" then EOF).
        if not chunk.endswith("\n\n---\n\n"):
            raise ParseError(f"message {i} missing trailing '---' terminator")
        msg_text = chunk[: -len("\n\n---\n\n")]
        role = ROLE_BY_SENDER[source][sender]
        messages.append({"sender_label": sender, "role": role, "text": msg_text})

    # Round-trip verification: re-render with the exact same template and
    # diff against the original file content.
    rendered = f"# {title}\n\n"
    if created is not None:
        rendered += f"**Created:** {created}\n"
    if updated is not None:
        rendered += f"**Updated:** {updated}\n"
    if model is not None:
        rendered += f"**Model:** {model}\n"
    rendered += "\n---\n\n"
    for msg in messages:
        rendered += f"**{msg['sender_label']}**:\n\n{msg['text']}\n\n---\n\n"

    if rendered != text:
        # Show first differing offset to make debugging easy.
        for i in range(min(len(rendered), len(text))):
            if rendered[i] != text[i]:
                raise ParseError(
                    f"round-trip mismatch at offset {i}: "
                    f"parsed={rendered[max(0,i-30):i+30]!r} "
                    f"original={text[max(0,i-30):i+30]!r}"
                )
        raise ParseError(f"round-trip length mismatch: parsed={len(rendered)} original={len(text)}")

    return {
        "title": title,
        "created_at": created,
        "updated_at": updated,
        "model": model,
        "messages": messages,
    }


def normalize_timestamp(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat()
    except ValueError:
        return value


def conversation_id_from_filename(path: Path, source: str) -> str:
    stem = path.stem
    prefix = f"{source}-"
    return stem[len(prefix):] if stem.startswith(prefix) else stem


def iter_export_files():
    for export_dir in sorted(REPO_ROOT.glob("exports*")):
        if not export_dir.is_dir():
            continue
        for path in sorted(export_dir.glob("*.md")):
            if path.name.startswith("claude-"):
                yield path, "claude"
            elif path.name.startswith("chatgpt-"):
                yield path, "chatgpt"
            else:
                print(f"SKIP (unrecognized prefix): {path}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(REPO_ROOT / "conversations.db"))
    parser.add_argument("--dry-run", action="store_true", help="Parse and verify only, no DB writes")
    args = parser.parse_args()

    files = list(iter_export_files())
    print(f"Found {len(files)} markdown export files.")

    parsed_rows = []
    errors = []
    for path, source in files:
        try:
            parsed = parse_markdown_export(path, source)
        except ParseError as e:
            errors.append((path, str(e)))
            print(f"FAILED  {path}: {e}")
            continue

        conv_id = conversation_id_from_filename(path, source)
        created_at = normalize_timestamp(parsed["created_at"])
        updated_at = normalize_timestamp(parsed["updated_at"])
        raw_obj = {
            "title": parsed["title"],
            "created_at": created_at,
            "updated_at": updated_at,
            "model": parsed["model"],
            "messages": [{"role": m["role"], "text": m["text"]} for m in parsed["messages"]],
        }
        raw_json = json.dumps(raw_obj, ensure_ascii=False, indent=2)
        content_hash = hashlib.sha256(raw_json.encode("utf-8")).hexdigest()
        parsed_rows.append({
            "source": source,
            "conversation_id": conv_id,
            "title": parsed["title"],
            "model": parsed["model"],
            "created_at": created_at,
            "updated_at": updated_at,
            "raw_json": raw_json,
            "content_hash": content_hash,
            "message_count": len(parsed["messages"]),
        })
        print(f"OK      {path}  ({len(parsed['messages'])} messages)")

    print(f"\nParsed OK: {len(parsed_rows)}   Failed: {len(errors)}")
    if errors:
        print("Files that failed to parse (not inserted):")
        for path, msg in errors:
            print(f"  - {path}: {msg}")

    if args.dry_run:
        print("\n--dry-run: no database writes performed.")
        return

    conn = sqlite3.connect(args.db)
    try:
        conn.executescript(SCHEMA)
        inserted = updated = unchanged = 0
        for row in parsed_rows:
            cur = conn.execute(
                "SELECT content_hash FROM raw_conversations WHERE source = ? AND conversation_id = ?",
                (row["source"], row["conversation_id"]),
            )
            existing = cur.fetchone()
            if existing is None:
                conn.execute(
                    """INSERT INTO raw_conversations
                       (source, conversation_id, title, model, created_at, updated_at,
                        raw_json, raw_source, content_hash)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 'markdown_reconstructed', ?)""",
                    (row["source"], row["conversation_id"], row["title"], row["model"],
                     row["created_at"], row["updated_at"], row["raw_json"], row["content_hash"]),
                )
                inserted += 1
            elif existing[0] != row["content_hash"]:
                conn.execute(
                    """UPDATE raw_conversations
                       SET title = ?, model = ?, created_at = ?, updated_at = ?,
                           raw_json = ?, raw_source = 'markdown_reconstructed',
                           content_hash = ?, imported_at = datetime('now')
                       WHERE source = ? AND conversation_id = ?""",
                    (row["title"], row["model"], row["created_at"], row["updated_at"],
                     row["raw_json"], row["content_hash"], row["source"], row["conversation_id"]),
                )
                updated += 1
            else:
                unchanged += 1
        conn.commit()
        print(f"\nDB: {args.db}")
        print(f"Inserted: {inserted}   Updated: {updated}   Unchanged: {unchanged}")

        total = conn.execute("SELECT COUNT(*) FROM raw_conversations").fetchone()[0]
        by_source = conn.execute(
            "SELECT source, COUNT(*) FROM raw_conversations GROUP BY source"
        ).fetchall()
        print(f"Total rows in raw_conversations: {total}")
        for source, count in by_source:
            print(f"  {source}: {count}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
