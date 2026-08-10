"""Derive a "knowledge state" per-topic representation from
conversations.db -- structurally distinct from personal_state.py's interest
state (see PROJECT_STATE.md / KNOWLEDGE_STATE_EXPERIMENT.md).

personal_state.py's `weight` is pure normalized FREQUENCY: how OFTEN a topic
(title token) comes up. That is an INTEREST signal. This module encodes a
different claim: KNOWLEDGE is accumulated exposure SPREAD OVER TIME and
decayed by how long ago it was last touched. Two topics with identical
conversation counts get different `familiarity` here if one was a
single-day burst and the other recurred across months -- that discrimination
is the entire point of this module existing alongside personal_state.py (see
test_knowledge_state.py's burst-vs-spread test).

This module MUST NOT duplicate personal_state's tokenizer or JSON writer --
it imports and reuses `personal_state._tokenize`, `personal_state._parse_ts`,
`personal_state.STOPWORDS`, and `personal_state.write`.

Same privacy invariant as personal_state.py: reads only `source`, `title`,
`updated_at` from `raw_conversations` (opened read-only) and never emits a
conversation_id, verbatim title, or raw_json content.

See KNOWLEDGE_STATE_EXPERIMENT.md for the pre-registered hypothesis and
evaluation protocol this representation is built for.
"""
import argparse
import math
import sqlite3
from datetime import datetime, timedelta, timezone

import personal_state as ps

# Reused, not duplicated -- see module docstring.
STOPWORDS = ps.STOPWORDS

# ---------------------------------------------------------------------------
# FROZEN familiarity formula. Pre-registered in KNOWLEDGE_STATE_EXPERIMENT.md
# before any evaluation number was observed. Do not tune these constants (or
# the formula shape) post-hoc based on eval_knowledge_state.py results -- see
# that document's "Pre-registration (frozen)" section and its stopping
# condition.
# ---------------------------------------------------------------------------
SATURATION = 8        # exposure saturates around ~8 conversations
SPREAD_MONTHS = 6      # spread saturates once active in ~6 distinct months
HALF_LIFE_DAYS = 120    # familiarity halves every 120 days of no contact


def _iso(dt):
    """Format a tz-aware datetime as UTC ISO-8601 (Z suffix), or None."""
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def derive(conn, *, now, window_days=0, max_topics=50, min_conversations=2):
    """Pure, deterministic aggregation over raw_conversations. No network, no
    LLM, no wall-clock read -- `now` is injected so this is fully
    deterministic and so eval_knowledge_state.py can evaluate familiarity as
    of the train/test split cutoff rather than as of today.

    Reads ONLY source/title/updated_at -- never raw_json. Returns a list of
    per-topic records (sorted by conversations desc, then key asc, then
    truncated to max_topics -- same convention as personal_state.derive()),
    each carrying only aggregate tokens/counts/dates, never a full title or
    conversation_id.

    window_days=0 means no window filter, matching personal_state.py's
    convention; otherwise rows are kept only if updated_at falls within the
    last window_days of `now`.
    """
    rows = conn.execute(
        "SELECT source, title, updated_at FROM raw_conversations ORDER BY id"
    ).fetchall()

    cutoff = None
    if window_days > 0:
        cutoff = now - timedelta(days=window_days)

    considered = []
    for _source, title, updated_at in rows:
        dt = ps._parse_ts(updated_at)
        if cutoff is not None and (dt is None or dt < cutoff):
            continue
        considered.append((title, dt))

    token_counts = {}
    token_first = {}   # token -> earliest parseable dt
    token_last = {}    # token -> latest parseable dt
    token_months = {}  # token -> set of (year, month) among parseable dts

    for title, dt in considered:
        for token in ps._tokenize(title):
            token_counts[token] = token_counts.get(token, 0) + 1
            if dt is not None:
                if token not in token_first or dt < token_first[token]:
                    token_first[token] = dt
                if token not in token_last or dt > token_last[token]:
                    token_last[token] = dt
                token_months.setdefault(token, set()).add((dt.year, dt.month))

    kept = [(tok, cnt) for tok, cnt in token_counts.items() if cnt >= min_conversations]
    kept.sort(key=lambda item: (-item[1], item[0]))

    topics = []
    for token, count in kept[:max_topics]:
        first_dt = token_first.get(token)
        last_dt = token_last.get(token)
        span_days = (last_dt - first_dt).days if first_dt is not None and last_dt is not None else 0
        recency_days = (now - last_dt).days if last_dt is not None else None
        active_months = len(token_months.get(token, ()))

        exposure = min(1.0, math.log(1 + count) / math.log(1 + SATURATION))
        spread = min(1.0, active_months / SPREAD_MONTHS)
        decay = 0.5 ** (recency_days / HALF_LIFE_DAYS) if recency_days is not None else 0.0
        familiarity = round((0.5 * exposure + 0.5 * spread) * decay, 4)

        topics.append({
            "key": token,
            "conversations": count,
            "first_seen": _iso(first_dt),
            "last_seen": _iso(last_dt),
            "span_days": span_days,
            "recency_days": recency_days,
            "active_months": active_months,
            "familiarity": familiarity,
        })

    return topics


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="conversations.db")
    parser.add_argument("--out", default="knowledge_state.json")
    parser.add_argument("--window-days", type=int, default=0)
    parser.add_argument("--max-topics", type=int, default=50)
    parser.add_argument("--min-conversations", type=int, default=2)
    args = parser.parse_args()

    now = datetime.now(timezone.utc)
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    try:
        topics = derive(
            conn,
            now=now,
            window_days=args.window_days,
            max_topics=args.max_topics,
            min_conversations=args.min_conversations,
        )
    finally:
        conn.close()

    state = {
        "generated_at": _iso(now),
        "window_days": args.window_days,
        "topics": topics,
    }
    ps.write(args.out, state)
    print(f"knowledge_state: {len(topics)} topics -> {args.out}")


if __name__ == "__main__":
    main()
