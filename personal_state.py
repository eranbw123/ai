"""Derive a versioned, privacy-safe "personal state" artifact from
conversations.db and write it out as JSON.

This is the ONLY file in this repo that publishes anything derived from
raw_conversations for consumption outside the ai repo (see the `internet`
repo's discovery/personal_state.py, the reader). It never writes to
conversations.db and never goes through export_to_sqlite.upsert() -- it is a
read-only, offline aggregation step, not part of the export path.

See PERSONAL_STATE_CONTRACT.md for the full schema doc and version-bump
procedure.
"""
import argparse
import json
import os
import re
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Versioning policy: bump CONTRACT_VERSION ONLY for a breaking change --
# removing/renaming a required field, or changing what a field means.
# Additive optional fields do NOT require a bump. Bumping requires updating
# PERSONAL_STATE_CONTRACT.md in this repo AND SUPPORTED_VERSIONS in the
# `internet` repo's discovery/personal_state.py.
CONTRACT_VERSION = 1

# English function words plus generic chat/export noise that would otherwise
# dominate topic counts without saying anything about what the owner actually
# talked about.
STOPWORDS = {
    "a", "an", "and", "the", "of", "to", "in", "on", "for", "with", "is",
    "are", "was", "were", "be", "been", "being", "this", "that", "these",
    "those", "it", "its", "as", "at", "by", "from", "or", "but", "not",
    "no", "yes", "you", "your", "i", "me", "my", "we", "our", "us", "they",
    "them", "their", "he", "she", "his", "her", "do", "does", "did", "can",
    "could", "will", "would", "should", "what", "when", "where", "why",
    "which", "who", "whom", "about", "into", "than", "then", "there",
    "here", "if", "so", "up", "out", "all", "any", "some", "more", "most",
    "other", "such", "only", "own", "same", "too", "very", "just", "also",
    "get", "got", "make", "made", "need", "want", "use", "used", "let",
    "hi", "hello", "thanks", "thank", "please",
    # generic chat/export noise
    "chat", "conversation", "new", "help", "question", "how", "using",
    "code", "file", "script", "error", "project", "untitled",
}

# Unicode-aware: \w (and its complement \W) already matches non-ASCII word
# characters in Python 3 str patterns, so this splits correctly on titles
# containing Hebrew, Cyrillic, accented Latin, etc. -- not just ASCII.
# Underscore is added explicitly because \w treats it as a word char, but
# "split on non-alphanumeric" (the spec) means underscore is a separator too.
_TOKEN_SPLIT_RE = re.compile(r"[\W_]+")


def _tokenize(title):
    """Lowercase + split on non-alphanumeric (Unicode-aware), then drop
    short/long/numeric/stopword tokens. Returns a set (each token counts
    once per conversation, regardless of how many times it appears in that
    title)."""
    if not title:
        return set()
    tokens = set()
    for tok in _TOKEN_SPLIT_RE.split(title.lower()):
        if not tok:
            continue
        if len(tok) < 3 or len(tok) > 30:
            continue
        if tok.isdigit():
            continue
        if tok in STOPWORDS:
            continue
        tokens.add(tok)
    return tokens


def _parse_ts(value):
    """Best-effort ISO-8601 parse; returns None (never raises) for missing
    or unparseable timestamps so a bad row can't crash the whole derive()."""
    if not value:
        return None
    try:
        v = value[:-1] + "+00:00" if value.endswith("Z") else value
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def derive(conn, *, window_days=180, max_topics=50, min_conversations=2):
    """Pure, deterministic aggregation over raw_conversations. No network, no
    LLM. Reads ONLY source/title/updated_at -- never raw_json.

    PRIVACY INVARIANT: the returned artifact carries only aggregate tokens
    and counts -- never full titles, never message bodies/raw_json, never
    conversation_ids. That's what makes it safe to publish outside this repo
    (see PERSONAL_STATE_CONTRACT.md). Do not add a field here that leaks any
    of those.

    window_days=0 means no window filter; otherwise rows are kept only if
    updated_at falls within the last window_days (consistent with this
    repo's existing --after/--before semantics, which filter on updated_at,
    not created_at, so an old conversation that got a new message recently
    is still "current").

    ORDER BY id makes row order (and therefore last_seen tie-breaking)
    deterministic regardless of physical storage order; topics themselves
    are explicitly sorted below so this function never depends on dict/set
    iteration order for its output.
    """
    rows = conn.execute(
        "SELECT source, title, updated_at FROM raw_conversations ORDER BY id"
    ).fetchall()

    cutoff = None
    if window_days > 0:
        cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)

    considered = []
    for source, title, updated_at in rows:
        dt = _parse_ts(updated_at)
        if cutoff is not None and (dt is None or dt < cutoff):
            continue
        considered.append((source, title, updated_at, dt))

    sources = {"claude": 0, "chatgpt": 0}
    token_counts = {}
    token_last_seen = {}  # token -> (dt or None, raw updated_at string)

    for source, title, updated_at, dt in considered:
        if source in sources:
            sources[source] += 1
        for token in _tokenize(title):
            token_counts[token] = token_counts.get(token, 0) + 1
            if token not in token_last_seen:
                token_last_seen[token] = (dt, updated_at)
            else:
                prev_dt, _ = token_last_seen[token]
                if dt is not None and (prev_dt is None or dt > prev_dt):
                    token_last_seen[token] = (dt, updated_at)

    kept = [(tok, cnt) for tok, cnt in token_counts.items() if cnt >= min_conversations]
    kept.sort(key=lambda item: (-item[1], item[0]))

    max_count = kept[0][1] if kept else 0

    topics = []
    for token, count in kept[:max_topics]:
        _, last_seen = token_last_seen[token]
        # last_seen may be null: updated_at is a nullable column in practice
        # (Claude API omitting it, markdown_reconstructed rows with no
        # header) and with window_days=0 such rows aren't filtered out. The
        # contract declares last_seen nullable for exactly this reason --
        # see PERSONAL_STATE_CONTRACT.md.
        topics.append({
            "key": token,
            "weight": round(count / max_count, 4) if max_count else 0.0,
            "conversations": count,
            "last_seen": last_seen,
        })

    return {
        "contract_version": CONTRACT_VERSION,
        "generated_at": _now_iso(),
        "window_days": window_days,
        "conversation_count": len(considered),
        "sources": sources,
        "topics": topics,
    }


def write(path, state):
    """Write state as UTF-8 JSON, atomically (temp file + os.replace) so a
    consumer never sees a partially-written artifact."""
    path = Path(path)
    payload = json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent) if str(path.parent) else ".",
        prefix=f".{path.name}.", suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(payload)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="conversations.db")
    parser.add_argument("--out", default="personal_state.json")
    parser.add_argument("--window-days", type=int, default=180)
    parser.add_argument("--max-topics", type=int, default=50)
    parser.add_argument("--min-conversations", type=int, default=2)
    parser.add_argument(
        "--with-knowledge-state", action="store_true", default=False,
        help="Merge knowledge_state.py's additive fields (first_seen, "
             "span_days, recency_days, active_months, familiarity) into "
             "each topic record. Default off; with it off, output is "
             "byte-identical to before this flag existed.",
    )
    args = parser.parse_args()

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    try:
        state = derive(
            conn,
            window_days=args.window_days,
            max_topics=args.max_topics,
            min_conversations=args.min_conversations,
        )
        if args.with_knowledge_state:
            import knowledge_state as ks

            ks_topics = ks.derive(
                conn,
                now=datetime.now(timezone.utc),
                window_days=args.window_days,
                max_topics=10 ** 9,  # candidate set must not be truncated to merge cleanly
                min_conversations=args.min_conversations,
            )
            ks_by_key = {t["key"]: t for t in ks_topics}
            for topic in state["topics"]:
                extra = ks_by_key.get(topic["key"])
                if not extra:
                    continue
                for field in ("first_seen", "span_days", "recency_days",
                              "active_months", "familiarity"):
                    topic[field] = extra[field]
    finally:
        conn.close()

    write(args.out, state)
    print(
        f"personal_state v{state['contract_version']}: "
        f"{len(state['topics'])} topics from {state['conversation_count']} "
        f"conversations -> {args.out}"
    )


if __name__ == "__main__":
    main()
