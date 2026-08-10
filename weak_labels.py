#!/usr/bin/env python3
"""Derive five weak, per-conversation behavioral labels from raw_conversations
and write them to a local JSON artifact, plus a distribution/sanity report.

This is an EXPERIMENT: it tests whether the corpus already contains enough
structure (turn counts, message timestamps, branch shape, title-token
overlap) to compute non-degenerate weak labels with no LLM and no network.
See WEAK_LABELS.md for the label definitions, the recorded distribution, and
the not-gold boundary rule this module enforces (weak_labels.json is a local,
gitignored artifact -- never published, never part of the cross-repo
personal-state contract, and not imported by anything else in this repo).

Like personal_state.py, this is read-only: it opens the DB with mode=ro,
never writes to conversations.db, and never goes through
export_to_sqlite.upsert().
"""
import argparse
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from statistics import median

import chatgpt_export
import claude_export
from export_to_sqlite import SCHEMA
from personal_state import _tokenize, write

EXTRACTOR_VERSION = 1

LABEL_ORDER = [
    "depth",
    "sustained_followup",
    "rapid_abandonment",
    "response_rejection",
    "recurrence",
]

# Short, literal, English-only cue list for response_rejection. Deliberately
# small -- see WEAK_LABELS.md for why this is a known, recorded blind spot
# (degraded_reasons always carries "lexical_cue_english_only" for this
# label). Matched on word boundaries, not bare substrings: a bare
# `cue in normalized` check let short cues like "no" match inside "know",
# "another", "not", "now", "cannot" etc., which would count almost any
# follow-up as a rejection. \b...\b still matches the multi-word phrases as
# whole fragments (e.g. "that's not what I asked for" still matches "that's
# not"), it just requires them to appear as actual words/phrases rather than
# arbitrary substrings.
REJECTION_CUES = [
    "no", "nope", "wrong", "that's not", "not what i", "doesn't work",
    "does not work", "still broken", "try again", "you misunderstood",
]
_REJECTION_CUE_PATTERNS = [re.compile(r"\b" + re.escape(c) + r"\b") for c in REJECTION_CUES]

# Fields read per source, recorded in each record's provenance.inputs so a
# downstream reader can see exactly what evidence a label's extraction used.
_CLAUDE_STRUCT_INPUTS = [
    "chat_messages.uuid", "chat_messages.sender", "chat_messages.text",
    "chat_messages.content", "chat_messages.parent_message_uuid",
    "current_leaf_message_uuid",
]
_CLAUDE_TS_INPUTS = _CLAUDE_STRUCT_INPUTS + ["chat_messages.created_at"]
_CHATGPT_STRUCT_INPUTS = [
    "mapping.message.author.role", "mapping.message.content.parts",
    "mapping.message.content.text", "mapping.parent", "mapping.children",
    "current_node",
]
_CHATGPT_TS_INPUTS = _CHATGPT_STRUCT_INPUTS + ["mapping.message.create_time"]
_RECURRENCE_INPUTS = ["title", "updated_at"]


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_updated_at_day(value):
    """Best-effort calendar-day extraction from an ISO updated_at string;
    never raises (same defensive shape as personal_state._parse_ts, kept
    separate/local here since it only needs a date, not a datetime)."""
    if not value:
        return None
    try:
        v = value[:-1] + "+00:00" if value.endswith("Z") else value
        return datetime.fromisoformat(v).date().isoformat()
    except ValueError:
        return None


def _as_utc(dt):
    """claude_export/chatgpt_export's parse_api_timestamp return a naive
    datetime for an ISO string with no Z/offset; comparing/min()/max()-ing
    that against tz-aware datetimes elsewhere in the same turn list raises
    TypeError. Normalise to UTC (matching personal_state._parse_ts's
    same-shaped fallback) rather than let that escape."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _safe_claude_ts(value):
    if not value:
        return None
    try:
        dt = claude_export.parse_api_timestamp(value)
    except (ValueError, TypeError, AttributeError):
        # AttributeError: parse_api_timestamp does value.replace(...), which
        # raises on a non-str created_at (e.g. a stray number) -- must never
        # raise, just treat as unparseable.
        return None
    return _as_utc(dt)


def _safe_chatgpt_ts(value):
    try:
        dt = chatgpt_export.parse_api_timestamp(value)
    except (ValueError, TypeError, OSError, OverflowError, AttributeError):
        return None
    if dt is None:
        return None
    return _as_utc(dt)


def _claude_turns(data):
    """Ordered (role, text, ts) turns from the current branch. Deliberate
    reuse of claude_export.get_current_branch/_message_text -- the existing
    branch-walking/parts-parsing logic -- rather than a parallel path; single
    call site for _message_text per the task plan."""
    turns = []
    for message in claude_export.get_current_branch(data):
        sender = message.get("sender")
        if sender not in ("human", "assistant"):
            continue
        role = "user" if sender == "human" else "assistant"
        text = claude_export._message_text(message)
        ts = _safe_claude_ts(message.get("created_at"))
        turns.append((role, text, ts))
    return turns


def _claude_regenerated_groups(data):
    """Count of parent_message_uuid groups with more than one assistant
    child anywhere in chat_messages (not just the current branch) -- the
    structural "assistant message with an assistant sibling" signal."""
    counts = {}
    for message in data.get("chat_messages") or []:
        if message.get("sender") == "assistant":
            parent = message.get("parent_message_uuid")
            counts[parent] = counts.get(parent, 0) + 1
    return sum(1 for c in counts.values() if c > 1)


def _chatgpt_turns(data):
    """Ordered (role, text, ts) turns from the current branch. Deliberate
    reuse of chatgpt_export.get_current_branch/_message_text; single call
    site for _message_text per the task plan."""
    turns = []
    for node in chatgpt_export.get_current_branch(data):
        message = node.get("message") or {}
        role_raw = (message.get("author") or {}).get("role")
        if role_raw not in ("user", "assistant"):
            continue
        text = chatgpt_export._message_text(node)
        ts = _safe_chatgpt_ts(message.get("create_time"))
        turns.append((role_raw, text, ts))
    return turns


def _chatgpt_regenerated_groups(data):
    """Count of mapping nodes with more than one assistant-role child --
    the ChatGPT-tree equivalent of _claude_regenerated_groups."""
    mapping = data.get("mapping") or {}
    groups = 0
    for node in mapping.values():
        assistant_children = 0
        for child_id in node.get("children") or []:
            child = mapping.get(child_id) or {}
            msg = child.get("message")
            if msg and (msg.get("author") or {}).get("role") == "assistant":
                assistant_children += 1
        if assistant_children > 1:
            groups += 1
    return groups


def _label_depth(turns):
    if not turns:
        # Not covered (matches the confidence-0.0 not-covered shape used by
        # every other label): an empty branch means get_current_branch found
        # nothing, so there's no structure to report a base rate on.
        return None, False, 0.0, ["empty_branch"], {
            "user_turns": 0, "assistant_turns": 0, "total_user_chars": 0,
        }
    user_turns = sum(1 for r, _, _ in turns if r == "user")
    assistant_turns = sum(1 for r, _, _ in turns if r == "assistant")
    total_user_chars = sum(len(t) for r, t, _ in turns if r == "user")
    value = min(user_turns / 10, 1.0)
    fired = user_turns >= 4
    evidence = {
        "user_turns": user_turns,
        "assistant_turns": assistant_turns,
        "total_user_chars": total_user_chars,
    }
    return value, fired, 0.9, [], evidence


def _label_sustained_followup(turns):
    user_turns = [(t, ts) for r, t, ts in turns if r == "user"]
    ts_values = [ts for _, ts in user_turns if ts is not None]
    if len(user_turns) < 2 or len(ts_values) < 2:
        return None, False, 0.0, ["no_message_timestamps"], {
            "user_turns": len(user_turns),
        }
    first_ts, last_ts = min(ts_values), max(ts_values)
    span_hours = (last_ts - first_ts).total_seconds() / 3600
    value = min(span_hours / 72, 1.0)
    fired = span_hours >= 24
    evidence = {"user_turns": len(user_turns), "span_hours": round(span_hours, 3)}
    return value, fired, 0.8, [], evidence


def _label_rapid_abandonment(turns):
    if not turns:
        return None, False, 0.0, ["empty_branch"], {
            "user_turns": 0, "assistant_turns": 0,
        }
    user_idx = [i for i, (r, _, _) in enumerate(turns) if r == "user"]
    asst_idx = [i for i, (r, _, _) in enumerate(turns) if r == "assistant"]
    # Deliberately low confidence (0.5): a single exchange is genuinely
    # ambiguous between "answer was perfect" and "user bailed" -- see
    # WEAK_LABELS.md.
    fired = len(user_idx) == 1 and len(asst_idx) >= 1 and user_idx[0] < asst_idx[0]
    value = 1.0 if fired else 0.0
    evidence = {"user_turns": len(user_idx), "assistant_turns": len(asst_idx)}
    return value, fired, 0.5, [], evidence


def _label_response_rejection(turns, regenerated_count):
    if not turns:
        return None, False, 0.0, ["empty_branch"], {
            "rejection_count": 0, "assistant_turns": 0,
            "matched_cue_ids": [], "regenerated_count": 0,
        }
    assistant_turns = sum(1 for r, _, _ in turns if r == "assistant")
    rejection_count = 0
    matched_cue_ids = set()
    for i in range(1, len(turns)):
        role, text, _ = turns[i]
        prev_role = turns[i - 1][0]
        if role != "user" or prev_role != "assistant":
            continue
        normalized = text.strip().lower()
        hit = False
        for idx, pattern in enumerate(_REJECTION_CUE_PATTERNS):
            if pattern.search(normalized):
                matched_cue_ids.add(idx)
                hit = True
        if hit:
            rejection_count += 1
    # "rejections" combines two independent weak signals: an explicit
    # lexical cue (user said something rejection-shaped) and an implicit
    # structural one (the model's response was regenerated -- kept as its
    # own siblings-of-an-assistant-message evidence key, regenerated_count).
    # Both indicate the same underlying event ("this response didn't land"),
    # so both count toward fired/value; they stay separate in evidence so a
    # reader can tell which signal actually fired.
    total = rejection_count + regenerated_count
    value = min(total / max(assistant_turns, 1), 1.0)
    fired = total >= 1
    evidence = {
        "rejection_count": rejection_count,
        "assistant_turns": assistant_turns,
        "matched_cue_ids": sorted(matched_cue_ids),
        "regenerated_count": regenerated_count,
    }
    return value, fired, 0.4, ["lexical_cue_english_only"], evidence


def _label_recurrence(key, conv_tokens, token_to_keys, conv_day):
    tokens = conv_tokens.get(key, set())
    token_count = len(tokens)
    if token_count == 0:
        # Same not-covered shape as sustained_followup's missing-timestamps
        # case: value/fired are meaningless without any tokens, so confidence
        # drops to 0.0 rather than reporting the label's normal base rate.
        return None, False, 0.0, ["no_title_tokens"], {"token_count": 0}
    shared_keys = set()
    for tok in tokens:
        shared_keys |= token_to_keys.get(tok, set())
    shared_keys.discard(key)
    shared_conversations = len(shared_keys)
    distinct_days = len({conv_day[k] for k in shared_keys if conv_day.get(k) is not None})
    value = min(shared_conversations / 10, 1.0)
    fired = shared_conversations >= 2 and distinct_days >= 2
    evidence = {
        "shared_conversations": shared_conversations,
        "distinct_days": distinct_days,
        "token_count": token_count,
    }
    return value, fired, 0.6, [], evidence


def _build_recurrence_index(conn):
    """Corpus-level pre-pass for the recurrence label: title/updated_at only
    (no raw_json), so this is cheap even at ~1600 rows."""
    rows = conn.execute(
        "SELECT source, conversation_id, title, updated_at FROM raw_conversations ORDER BY id"
    ).fetchall()
    conv_tokens = {}
    token_to_keys = {}
    conv_day = {}
    for source, conversation_id, title, updated_at in rows:
        key = (source, conversation_id)
        tokens = _tokenize(title)
        conv_tokens[key] = tokens
        for tok in tokens:
            token_to_keys.setdefault(tok, set()).add(key)
        conv_day[key] = _parse_updated_at_day(updated_at)
    return conv_tokens, token_to_keys, conv_day


def _make_record(source, conversation_id, label, value, fired, confidence,
                  degraded_reasons, evidence, inputs, corpus_row_id, raw_source):
    degraded_reasons = list(degraded_reasons)
    # Lossy reconstructions (see PROJECT_STATE.md) get a flat confidence
    # haircut on every label, regardless of which label or whether it was
    # otherwise covered.
    if raw_source == "markdown_reconstructed":
        confidence = confidence * 0.5
        if "markdown_reconstructed" not in degraded_reasons:
            degraded_reasons.append("markdown_reconstructed")
    return {
        "source": source,
        "conversation_id": conversation_id,
        "label": label,
        "value": round(value, 4) if value is not None else None,
        "fired": bool(fired),
        "confidence": round(confidence, 6),
        "degraded_reasons": degraded_reasons,
        "evidence": evidence,
        "provenance": {
            "extractor": f"weak_labels.{label}",
            "extractor_version": EXTRACTOR_VERSION,
            "inputs": inputs,
            "corpus_row_id": corpus_row_id,
            "raw_source": raw_source,
        },
    }


def derive(conn, *, db_desc):
    """Stream raw_conversations once (plus one cheap title/updated_at-only
    pre-pass for recurrence) and emit 5 label records per conversation.
    Never materializes all raw_json blobs at once -- json.loads happens
    inside the loop, one row at a time."""
    conv_tokens, token_to_keys, conv_day = _build_recurrence_index(conn)

    records = []
    sources = {"claude": 0, "chatgpt": 0}
    conversation_count = 0

    cur = conn.execute(
        "SELECT id, source, conversation_id, title, created_at, updated_at, "
        "raw_json, raw_source FROM raw_conversations ORDER BY id"
    )
    for row_id, source, conversation_id, title, created_at, updated_at, raw_json, raw_source in cur:
        conversation_count += 1
        if source in sources:
            sources[source] += 1
        try:
            data = json.loads(raw_json)
        except (json.JSONDecodeError, TypeError):
            data = {}

        if source == "claude":
            turns = _claude_turns(data)
            regenerated_count = _claude_regenerated_groups(data)
            struct_inputs, ts_inputs = _CLAUDE_STRUCT_INPUTS, _CLAUDE_TS_INPUTS
        elif source == "chatgpt":
            turns = _chatgpt_turns(data)
            regenerated_count = _chatgpt_regenerated_groups(data)
            struct_inputs, ts_inputs = _CHATGPT_STRUCT_INPUTS, _CHATGPT_TS_INPUTS
        else:
            turns, regenerated_count = [], 0
            struct_inputs, ts_inputs = [], []

        key = (source, conversation_id)
        per_label = (
            ("depth", _label_depth(turns), struct_inputs),
            ("sustained_followup", _label_sustained_followup(turns), ts_inputs),
            ("rapid_abandonment", _label_rapid_abandonment(turns), struct_inputs),
            ("response_rejection", _label_response_rejection(turns, regenerated_count), struct_inputs),
            ("recurrence", _label_recurrence(key, conv_tokens, token_to_keys, conv_day), _RECURRENCE_INPUTS),
        )
        for label, (value, fired, confidence, degraded_reasons, evidence), inputs in per_label:
            records.append(_make_record(
                source, conversation_id, label, value, fired, confidence,
                degraded_reasons, evidence, inputs, row_id, raw_source,
            ))

    records.sort(key=lambda r: (r["source"], r["conversation_id"], r["label"]))

    return {
        "schema_version": 1,
        "ground_truth": False,
        "generated_at": _now_iso(),
        "extractor_version": EXTRACTOR_VERSION,
        "corpus": {
            "db": db_desc,
            "conversation_count": conversation_count,
            "sources": sources,
        },
        "labels": records,
    }


def build_synthetic_corpus(conn):
    """Deterministic fixture corpus shared by test_weak_labels.py and the
    --synthetic-corpus CLI path (see CORPUS AVAILABILITY: conversations.db is
    gitignored and not present in every checkout). Covers, at minimum: a
    deep multi-turn conversation, a single-exchange conversation, user turns
    days apart, a rejection-cue turn, a regenerated assistant branch, titles
    shared across conversations on different days, a conversation with no
    per-message timestamps, and a markdown_reconstructed row."""
    conn.executescript(SCHEMA)

    def insert(source, conversation_id, title, created_at, updated_at, raw_obj, raw_source="api_json"):
        conn.execute(
            "INSERT INTO raw_conversations "
            "(source, conversation_id, title, model, created_at, updated_at, "
            "raw_json, raw_source, content_hash) VALUES (?,?,?,?,?,?,?,?,?)",
            (source, conversation_id, title, "test-model", created_at, updated_at,
             json.dumps(raw_obj), raw_source, f"hash-{source}-{conversation_id}"),
        )

    # 1. Deep multi-turn Claude conversation -> depth fires (user_turns=4).
    deep_messages = []
    prev = None
    for i in range(4):
        u, a = f"deep-u{i}", f"deep-a{i}"
        deep_messages.append({
            "uuid": u, "sender": "human", "parent_message_uuid": prev,
            "text": f"deep question {i}", "created_at": f"2026-01-01T10:0{i}:00Z",
        })
        deep_messages.append({
            "uuid": a, "sender": "assistant", "parent_message_uuid": u,
            "text": f"deep answer {i}", "created_at": f"2026-01-01T10:0{i}:30Z",
        })
        prev = a
    insert("claude", "deep-1", "Deep debugging session",
           "2026-01-01T10:00:00Z", "2026-01-01T10:10:00Z",
           {"uuid": "deep-1", "name": "Deep debugging session",
            "current_leaf_message_uuid": "deep-a3", "chat_messages": deep_messages})

    # 2. Single-exchange Claude conversation -> rapid_abandonment fires,
    # depth does not.
    insert("claude", "quick-1", "Quick one-off question",
           "2026-01-02T09:00:00Z", "2026-01-02T09:01:00Z",
           {"uuid": "quick-1", "name": "Quick one-off question",
            "current_leaf_message_uuid": "q-a1", "chat_messages": [
                {"uuid": "q-u1", "sender": "human", "parent_message_uuid": None,
                 "text": "one question", "created_at": "2026-01-02T09:00:00Z"},
                {"uuid": "q-a1", "sender": "assistant", "parent_message_uuid": "q-u1",
                 "text": "one answer", "created_at": "2026-01-02T09:01:00Z"},
            ]})

    # 3. User turns 2 days apart -> sustained_followup fires (span 48h).
    insert("claude", "followup-1", "Long running project check-in",
           "2026-01-01T08:00:00Z", "2026-01-03T08:05:00Z",
           {"uuid": "followup-1", "name": "Long running project check-in",
            "current_leaf_message_uuid": "f-a2", "chat_messages": [
                {"uuid": "f-u1", "sender": "human", "parent_message_uuid": None,
                 "text": "day1 question", "created_at": "2026-01-01T08:00:00Z"},
                {"uuid": "f-a1", "sender": "assistant", "parent_message_uuid": "f-u1",
                 "text": "day1 answer", "created_at": "2026-01-01T08:05:00Z"},
                {"uuid": "f-u2", "sender": "human", "parent_message_uuid": "f-a1",
                 "text": "day3 followup", "created_at": "2026-01-03T08:00:00Z"},
                {"uuid": "f-a2", "sender": "assistant", "parent_message_uuid": "f-u2",
                 "text": "day3 answer", "created_at": "2026-01-03T08:05:00Z"},
            ]})

    # 4b. Claude conversation whose post-assistant follow-up is benign but
    # contains "no" as a bare substring inside other words ("now", "know")
    # -- regression fixture for the cue-matcher substring blowup: must NOT
    # count as a rejection.
    insert("claude", "benign-1", "Benign thanks follow-up",
           "2026-01-08T00:00:00Z", "2026-01-08T00:02:00Z",
           {"uuid": "benign-1", "name": "Benign thanks follow-up",
            "current_leaf_message_uuid": "bn-u2", "chat_messages": [
                {"uuid": "bn-u1", "sender": "human", "parent_message_uuid": None,
                 "text": "how do I fix this bug?", "created_at": "2026-01-08T00:00:00Z"},
                {"uuid": "bn-a1", "sender": "assistant", "parent_message_uuid": "bn-u1",
                 "text": "here is the fix", "created_at": "2026-01-08T00:01:00Z"},
                {"uuid": "bn-u2", "sender": "human", "parent_message_uuid": "bn-a1",
                 "text": "thanks, now I know what to do", "created_at": "2026-01-08T00:02:00Z"},
            ]})

    def cg_node(node_id, parent, role=None, text=None, create_time=None, children=None):
        message = None
        if role is not None:
            message = {
                "author": {"role": role},
                "content": {"content_type": "text", "parts": [text] if text is not None else []},
                "create_time": create_time,
            }
        return node_id, {"id": node_id, "parent": parent, "children": children or [], "message": message}

    # 4. ChatGPT conversation with a lexical rejection cue turn.
    reject_mapping = dict([
        cg_node("root", None, role=None, children=["u1"]),
        cg_node("u1", "root", role="user", text="please fix my code",
                create_time=1767254400, children=["a1"]),
        cg_node("a1", "u1", role="assistant", text="here's a fix",
                create_time=1767254460, children=["u2"]),
        cg_node("u2", "a1", role="user", text="No, that's not what I need, try again",
                create_time=1767254520, children=["a2"]),
        cg_node("a2", "u2", role="assistant", text="here's another fix",
                create_time=1767254580, children=[]),
    ])
    insert("chatgpt", "reject-1", "Fix my code please",
           "2026-01-01T00:00:00Z", "2026-01-01T00:03:00Z",
           {"id": "reject-1", "title": "Fix my code please",
            "create_time": 1767254400, "update_time": 1767254580,
            "current_node": "a2", "mapping": reject_mapping})

    # 5. ChatGPT conversation with a regenerated assistant branch (two
    # assistant children of the same parent) but no lexical cue -- tests the
    # structural signal firing response_rejection on its own.
    regen_mapping = dict([
        cg_node("root", None, role=None, children=["u1"]),
        cg_node("u1", "root", role="user", text="explain recursion",
                create_time=1767340800, children=["a1", "a1b"]),
        cg_node("a1", "u1", role="assistant", text="explanation attempt 1",
                create_time=1767340860, children=["u2"]),
        cg_node("a1b", "u1", role="assistant", text="explanation attempt 2 (regenerated)",
                create_time=1767340870, children=[]),
        cg_node("u2", "a1", role="user", text="thanks, that makes sense",
                create_time=1767340920, children=[]),
    ])
    insert("chatgpt", "regen-1", "Explain recursion",
           "2026-01-02T00:00:00Z", "2026-01-02T00:02:00Z",
           {"id": "regen-1", "title": "Explain recursion",
            "create_time": 1767340800, "update_time": 1767340920,
            "current_node": "u2", "mapping": regen_mapping})

    # 6/7/8. Three conversations sharing title tokens ("widget"/"pipeline")
    # across different days -> recurrence fires for the ones with >=2
    # distinct shared days; a solo title is the not-fired control.
    insert("claude", "recur-a", "Widget Pipeline Refactor Notes",
           "2026-01-10T00:00:00Z", "2026-01-10T00:00:00Z",
           {"uuid": "recur-a", "name": "Widget Pipeline Refactor Notes",
            "current_leaf_message_uuid": "ra-a1", "chat_messages": [
                {"uuid": "ra-u1", "sender": "human", "parent_message_uuid": None,
                 "text": "widget pipeline notes"},
                {"uuid": "ra-a1", "sender": "assistant", "parent_message_uuid": "ra-u1",
                 "text": "ok"},
            ]})
    insert("claude", "recur-b", "Widget Pipeline Cleanup",
           "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z",
           {"uuid": "recur-b", "name": "Widget Pipeline Cleanup",
            "current_leaf_message_uuid": "rb-a1", "chat_messages": [
                {"uuid": "rb-u1", "sender": "human", "parent_message_uuid": None,
                 "text": "cleanup"},
                {"uuid": "rb-a1", "sender": "assistant", "parent_message_uuid": "rb-u1",
                 "text": "ok"},
            ]})
    insert("claude", "recur-c", "Completely Different Widget Migration",
           "2026-01-05T00:00:00Z", "2026-01-05T00:00:00Z",
           {"uuid": "recur-c", "name": "Completely Different Widget Migration",
            "current_leaf_message_uuid": "rc-a1", "chat_messages": [
                {"uuid": "rc-u1", "sender": "human", "parent_message_uuid": None,
                 "text": "migration"},
                {"uuid": "rc-a1", "sender": "assistant", "parent_message_uuid": "rc-u1",
                 "text": "ok"},
            ]})
    insert("claude", "recur-solo", "Zzyxquv Solo Analysis",
           "2026-01-06T00:00:00Z", "2026-01-06T00:00:00Z",
           {"uuid": "recur-solo", "name": "Zzyxquv Solo Analysis",
            "current_leaf_message_uuid": "rs-a1", "chat_messages": [
                {"uuid": "rs-u1", "sender": "human", "parent_message_uuid": None,
                 "text": "solo"},
                {"uuid": "rs-a1", "sender": "assistant", "parent_message_uuid": "rs-u1",
                 "text": "ok"},
            ]})

    # 9. Zero-title-token conversation -> recurrence not covered.
    insert("claude", "no-title-1", "The Of And",
           "2026-01-07T00:00:00Z", "2026-01-07T00:00:00Z",
           {"uuid": "no-title-1", "name": "The Of And",
            "current_leaf_message_uuid": "nt2-a1", "chat_messages": [
                {"uuid": "nt2-u1", "sender": "human", "parent_message_uuid": None,
                 "text": "hi"},
                {"uuid": "nt2-a1", "sender": "assistant", "parent_message_uuid": "nt2-u1",
                 "text": "ok"},
            ]})

    # 10. No per-message timestamps -> sustained_followup not covered.
    insert("claude", "no-ts-1", "Untimestamped multi turn chat",
           None, None,
           {"uuid": "no-ts-1", "name": "Untimestamped multi turn chat",
            "current_leaf_message_uuid": "nt-a2", "chat_messages": [
                {"uuid": "nt-u1", "sender": "human", "parent_message_uuid": None, "text": "question A"},
                {"uuid": "nt-a1", "sender": "assistant", "parent_message_uuid": "nt-u1", "text": "answer A"},
                {"uuid": "nt-u2", "sender": "human", "parent_message_uuid": "nt-a1", "text": "question B"},
                {"uuid": "nt-a2", "sender": "assistant", "parent_message_uuid": "nt-u2", "text": "answer B"},
            ]})

    # 11. markdown_reconstructed row -> confidence downgraded on every label.
    # Shaped like migrate_md_to_sqlite.py's actual raw_obj ({title,
    # created_at, updated_at, model, messages: [{role, text}]}) -- NOT the
    # claude-API chat_messages shape. get_current_branch() finds neither
    # chat_messages nor mapping in this shape and returns [], so the
    # branch-based labels come back not-covered (empty_branch) rather than
    # covered-and-downgraded; that's the real, self-diagnosing behavior for
    # these rows today (recorded as a coverage gap, not silently modeled).
    insert("claude", "md-1", "Reconstructed markdown chat",
           "2026-01-04T00:00:00Z", "2026-01-04T00:01:00Z",
           {"title": "Reconstructed markdown chat",
            "created_at": "2026-01-04T00:00:00Z", "updated_at": "2026-01-04T00:01:00Z",
            "model": "test-model", "messages": [
                {"role": "human", "text": "reconstructed question"},
                {"role": "assistant", "text": "reconstructed answer"},
            ]}, raw_source="markdown_reconstructed")

    conn.commit()


def format_report(state):
    corpus = state["corpus"]
    by_label = {}
    for rec in state["labels"]:
        by_label.setdefault(rec["label"], []).append(rec)

    lines = [
        f"Corpus: {corpus['db']} -- {corpus['conversation_count']} conversations "
        f"(claude={corpus['sources'].get('claude', 0)}, "
        f"chatgpt={corpus['sources'].get('chatgpt', 0)})",
        "",
        "| label | covered_n | coverage | fired_n | prevalence | value_min | "
        "value_median | value_max | mean_confidence | informative | reason |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]

    def fmt(x):
        return "n/a" if x is None else f"{x:.4f}"

    for label in LABEL_ORDER:
        recs = by_label.get(label, [])
        total = len(recs)
        covered = [r for r in recs if r["value"] is not None]
        fired = [r for r in covered if r["fired"]]
        coverage = len(covered) / total if total else 0.0
        prevalence = len(fired) / len(covered) if covered else 0.0
        values = [r["value"] for r in covered]
        vmin = min(values) if values else None
        vmed = median(values) if values else None
        vmax = max(values) if values else None
        mean_conf = sum(r["confidence"] for r in recs) / total if total else 0.0

        informative, reason = True, ""
        if coverage < 0.20:
            informative, reason = False, "coverage < 0.20"
        elif prevalence < 0.02:
            informative, reason = False, "prevalence < 0.02"
        elif prevalence > 0.90:
            informative, reason = False, "prevalence > 0.90"

        lines.append(
            f"| {label} | {len(covered)} | {coverage:.4f} | {len(fired)} | "
            f"{prevalence:.4f} | {fmt(vmin)} | {fmt(vmed)} | {fmt(vmax)} | "
            f"{mean_conf:.4f} | {informative} | {reason} |"
        )
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="conversations.db")
    parser.add_argument("--out", default="weak_labels.json")
    parser.add_argument("--report", action="store_true",
                         help="print a markdown distribution/sanity report to stdout")
    parser.add_argument("--synthetic-corpus", metavar="PATH",
                         help="build the deterministic synthetic fixture corpus at PATH "
                              "and run against that instead of --db (use when "
                              "conversations.db isn't reachable -- see WEAK_LABELS.md)")
    args = parser.parse_args()

    if args.synthetic_corpus:
        if os.path.exists(args.synthetic_corpus):
            # --synthetic-corpus writes fixture rows outside upsert() --
            # refuse to run against an existing file (e.g. a real
            # conversations.db passed by mistake) rather than silently
            # injecting fixture rows into it.
            parser.error(
                f"--synthetic-corpus path already exists: {args.synthetic_corpus} "
                "(refusing to write fixture rows into an existing DB -- pass a "
                "fresh throwaway path)"
            )
        conn = sqlite3.connect(args.synthetic_corpus)
        try:
            build_synthetic_corpus(conn)
            state = derive(conn, db_desc="synthetic fixture")
        finally:
            conn.close()
    else:
        conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
        try:
            state = derive(conn, db_desc=args.db)
        finally:
            conn.close()

    write(args.out, state)
    print(
        f"weak_labels v{state['extractor_version']}: {len(state['labels'])} records from "
        f"{state['corpus']['conversation_count']} conversations "
        f"({state['corpus']['db']}) -> {args.out}"
    )

    if args.report:
        print()
        print(format_report(state))


if __name__ == "__main__":
    main()
