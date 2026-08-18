"""Derive candidate news-discovery interests from what the owner actually
talks about, with the evidence attached.

This is the producer half of the interest-intelligence bridge (design doc
2026-08-17, PR G). `personal_state.py` already publishes a token-level view
of the corpus; measured against its own pre-registered eval that view turned
out to be too crude to generate interests from -- it surfaces `deck`, `adhd`
and `זיכרון` but also `issue`, `best` and `low`, it cannot merge the owner's
recurring `dissangement` typo with `disengagement`, cannot join a theme that
lives half in Hebrew and half in English, and cannot produce a description or
signals. So this module reads conversation BODIES through an LLM instead of
counting title tokens.

Two stages, deliberately split:

  map     one pass per conversation (batched), cheap and incremental. Emits
          per-conversation topics: an English label, domain, language,
          register, how deep the owner pushed, entities, and one verbatim
          quote. Checkpointed into digests.db keyed by content_hash, so a
          re-imported or edited conversation re-digests and everything else
          is skipped.
  reduce  one pass over the aggregated digests. Sees the current interest
          set and proposes candidates that are NOT already covered, each
          carrying evidence quotes, durability statistics, and a similarity
          rating against every existing interest.

Ranking is code's job, not the model's (§5.2 of the design, and this repo's
standing preference): the model rates `expected_yield` and similarity, and
`score_candidate()` here combines them with measured recurrence/recency/
evidence into the offer score. That keeps the ranking auditable and stable
across model drift.

NO ANTHROPIC API. Every call goes through claude_browser.BrowserClaude --
the owner's own logged-in claude.ai tab over CDP, the same transport
council_bot.py has always used. There is no API key and no SDK import
anywhere in this path, by requirement.

Because that is ONE serialized browser tab shared with other automation,
this is built to be interrupted: work is batched, every batch is committed
before the next starts, and a resumed run re-reads digests.db and does only
what is left. Killing a backfill halfway and restarting it costs at most one
batch.

conversations.db is opened strictly read-only. This module never writes to
it (it is not part of the export path, and `export_to_sqlite.upsert()` stays
the single writer); its own state lives in a separate digests.db.

Usage:
    python interest_extractor.py status
    python interest_extractor.py map --limit 20
    python interest_extractor.py map                    # full backfill, resumable
    python interest_extractor.py reduce --out interest_candidates.json
"""
import argparse
import json
import math
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import export_to_sqlite as ets
import personal_state as ps
from claude_browser import BrowserClaude, BrowserClaudeError, is_scratch_conversation
from chatgpt_export import convert_to_markdown as chatgpt_convert_to_markdown
from claude_export import convert_to_markdown as claude_convert_to_markdown

REPO_ROOT = Path(__file__).resolve().parent

# Contract v2 = v1's `topics[]` plus an evidence-bearing `candidates[]`.
# v1 consumers (the token ladder) keep reading `topics` untouched; see
# PERSONAL_STATE_CONTRACT.md's v2 section for the schema and the deliberate
# relaxation of v1's tokens-only privacy invariant.
CONTRACT_VERSION = 2

# Bump to invalidate every stored digest (prompt or output-shape change).
# Digests are keyed (content_hash, digest_version), so a bump makes the next
# map run re-digest the corpus instead of silently mixing shapes.
DIGEST_VERSION = 1

DEFAULT_DB = str(REPO_ROOT / "conversations.db")
DEFAULT_DIGESTS = str(REPO_ROOT / "digests.db")
DEFAULT_OUT = str(REPO_ROOT / "interest_candidates.json")

# Per-conversation body cap before batching. The design budgets 8K tokens per
# conversation; at ~4 chars/token that is ~32K chars, but several of those
# have to share one browser call, so the cap here is per conversation and the
# batch size is what keeps the total sane.
DEFAULT_MAX_CHARS = 8000
DEFAULT_BATCH_SIZE = 3
DEFAULT_MAX_ATTEMPTS = 3
# Effectively 'send them all': the reduce pass does the merging, so a cap
# here silently decides which interests are allowed to exist.
DEFAULT_MAX_THEMES = 1200

# --- ranking constants (design §5.2, do not retune without saying so) --------
WEIGHTS = {
    "evidence_strength": 0.30,
    "recurrence": 0.15,
    "recency": 0.15,
    "novelty": 0.20,
    "expected_yield": 0.20,
}
EVIDENCE_SATURATION = 8      # conversations at which evidence_strength hits 1.0
RECURRENCE_MONTHS = 4        # active months at which recurrence hits 1.0
RECENCY_HALF_LIFE_DAYS = 90
OFFER_SCORE_FLOOR = 0.45
MAX_OFFERS_PER_RUN = 5       # the inbox is a two-minute ritual, not a feed
MAX_EVIDENCE_QUOTES = 4
MAX_QUOTE_CHARS = 140

# --- durability separator (design §3.1, measured on this corpus) -------------
# Of 131 title tokens appearing in >=2 conversations, 69 span >=30 days across
# >=2 calendar months and 37 live entirely inside a single <7-day burst. That
# measured split IS the separator; it is not re-derived here.
DURABLE_MIN_SPAN_DAYS = 30
DURABLE_MIN_ACTIVE_MONTHS = 2
TRANSIENT_MAX_SPAN_DAYS = 7

# Durability floors an aggregated theme must clear before it can be offered
# at all (design §5.2). The second clause lets a deep two-conversation
# research dive through; five AirPlay support tickets never qualify, because
# support-register topics are dropped at map time before they get here.
GATE_MIN_CONVS = 3
GATE_MIN_ACTIVE_MONTHS = 2
GATE_DEEP_MIN_CONVS = 2
GATE_DEEP_MIN_DEPTH = 0.6

# Registers the map pass assigns. "support" is the transient-errand bucket --
# printers, AirPlay, a vape that won't charge -- and is excluded from
# aggregation entirely rather than being filtered later by keyword.
SUPPORT_REGISTER = "support"
VALID_REGISTERS = ("hobby-question", "task-execution", "research", SUPPORT_REGISTER)


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg):
    print(f"[{_now_iso()}] {msg}", flush=True)


# --- language handling -------------------------------------------------------

_HEBREW_RE = re.compile(r"[֐-׿]")
_LATIN_RE = re.compile(r"[A-Za-z]")


def detect_lang(text):
    """'he', 'en', 'mixed', or 'und' for a string.

    28% of this corpus's titles are Hebrew and several themes are code-
    switched mid-sentence, so this is a ratio test rather than a
    first-character test: a Hebrew sentence quoting an English drug name is
    still Hebrew, and a genuinely half-and-half string is 'mixed' rather
    than being forced into one bucket.
    """
    he = len(_HEBREW_RE.findall(text or ""))
    en = len(_LATIN_RE.findall(text or ""))
    if not he and not en:
        return "und"
    if not he:
        return "en"
    if not en:
        return "he"
    # Thresholds are deliberately wide: a Hebrew sentence that names one
    # English drug or product ("כמה מודפיניל modafinil לקחת") is still a
    # meaningfully code-switched string, and calling it plain "he" would hide
    # that the theme spans both languages -- which is exactly the signal the
    # reduce pass needs to merge a Hebrew theme with its English twin.
    ratio = he / (he + en)
    if ratio >= 0.75:
        return "he"
    if ratio <= 0.25:
        return "en"
    return "mixed"


# Theme-key stopwords: personal_state's list plus the words an LLM reliably
# pads a topic label with. Kept separate from ps.STOPWORDS (which is tuned
# for raw titles) so neither can silently change the other's behaviour.
_LABEL_STOPWORDS = ps.STOPWORDS | {
    "and", "for", "with", "the", "via", "vs", "versus", "into", "about",
    "strategy", "strategies", "general", "related", "topic", "topics",
    "discussion", "advice", "options", "issues", "questions",
}

_LABEL_SPLIT_RE = re.compile(r"[\W_]+")


def theme_key(label):
    """Normalize a topic label into a merge key.

    Layer (1) of the design's three dedup layers: lowercase, split on
    non-alphanumerics (Unicode-aware, so Hebrew labels survive as Hebrew
    rather than collapsing to empty), drop stopwords, strip a plural 's',
    and sort -- so "Binding of Isaac unlocks" and "unlocks in Binding of
    Isaac" merge, while "Isaac" alone stays distinct. Cross-language merges
    (זיכרון עבודה <-> working memory) are NOT attempted here; that is the
    reduce pass's semantic job, and doing it with string rules would be
    guesswork.

    Falls back to the squashed raw label when normalization empties the
    string (a label made entirely of stopwords), so a theme can never
    silently acquire the empty key and swallow every other empty one.
    """
    toks = []
    for tok in _LABEL_SPLIT_RE.split((label or "").lower()):
        if not tok or tok.isdigit():
            continue
        if len(tok) > 3 and tok.endswith("s") and not tok.endswith("ss"):
            tok = tok[:-1]
        if tok in _LABEL_STOPWORDS:
            continue
        toks.append(tok)
    if not toks:
        squashed = _LABEL_SPLIT_RE.sub("", (label or "").lower())
        return squashed or "untitled"
    return " ".join(sorted(set(toks)))


def slugify(label):
    """Interest-key slug. ASCII where possible; a purely Hebrew label has no
    sensible ASCII slug, so it keeps its own characters rather than becoming
    an empty or numeric key (the reduce pass is asked for English keys, but
    this must not corrupt the data if it returns one anyway)."""
    s = (label or "").strip().lower()
    s = _LABEL_SPLIT_RE.sub("-", s).strip("-")
    return s or "candidate"


# --- digests.db --------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversation_digests (
    content_hash         TEXT    NOT NULL,
    digest_version       INTEGER NOT NULL,
    conversation_id      TEXT    NOT NULL,
    source               TEXT    NOT NULL,
    title                TEXT,
    created_at           TEXT,
    updated_at           TEXT,
    topics               TEXT    NOT NULL DEFAULT '[]',
    is_transient_support INTEGER NOT NULL DEFAULT 0,
    model                TEXT,
    digested_at          TEXT    NOT NULL,
    PRIMARY KEY (content_hash, digest_version)
);
CREATE INDEX IF NOT EXISTS idx_digests_conv ON conversation_digests(conversation_id);

-- A conversation the map pass keeps failing on (unparseable reply, a body
-- that trips a filter) must not block the backfill forever: attempts are
-- counted here and --max-attempts retires it, so a full run converges
-- instead of retrying the same poison row on every resume.
CREATE TABLE IF NOT EXISTS digest_failures (
    content_hash    TEXT    NOT NULL,
    digest_version  INTEGER NOT NULL,
    conversation_id TEXT    NOT NULL,
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT    NOT NULL DEFAULT '',
    last_attempt_at TEXT    NOT NULL,
    PRIMARY KEY (content_hash, digest_version)
);
"""


def open_digests(path):
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def digested_hashes(dconn, digest_version=DIGEST_VERSION):
    return {r[0] for r in dconn.execute(
        "SELECT content_hash FROM conversation_digests WHERE digest_version = ?",
        (digest_version,))}


def exhausted_hashes(dconn, max_attempts, digest_version=DIGEST_VERSION):
    return {r[0] for r in dconn.execute(
        "SELECT content_hash FROM digest_failures WHERE digest_version = ? AND attempts >= ?",
        (digest_version, max_attempts))}


def record_digest(dconn, row, digest, model, digest_version=DIGEST_VERSION):
    dconn.execute(
        "INSERT OR REPLACE INTO conversation_digests "
        "(content_hash, digest_version, conversation_id, source, title, created_at, "
        " updated_at, topics, is_transient_support, model, digested_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (row["content_hash"], digest_version, row["conversation_id"], row["source"],
         row["title"], row["created_at"], row["updated_at"],
         json.dumps(digest.get("topics") or [], ensure_ascii=False),
         1 if digest.get("is_transient_support") else 0, model, _now_iso()),
    )
    # A conversation that just succeeded should not keep a failure record
    # around to be counted against --max-attempts on some later re-digest.
    dconn.execute("DELETE FROM digest_failures WHERE content_hash = ? AND digest_version = ?",
                   (row["content_hash"], digest_version))


def record_failure(dconn, row, error, digest_version=DIGEST_VERSION):
    dconn.execute(
        "INSERT INTO digest_failures (content_hash, digest_version, conversation_id, "
        " attempts, last_error, last_attempt_at) VALUES (?,?,?,1,?,?) "
        "ON CONFLICT(content_hash, digest_version) DO UPDATE SET "
        " attempts = attempts + 1, last_error = excluded.last_error, "
        " last_attempt_at = excluded.last_attempt_at",
        (row["content_hash"], digest_version, row["conversation_id"],
         str(error)[:500], _now_iso()),
    )


def load_digests(dconn, digest_version=DIGEST_VERSION):
    """Every stored digest, as plain dicts. ORDER BY makes aggregation
    deterministic regardless of physical row order."""
    out = []
    for row in dconn.execute(
        "SELECT content_hash, conversation_id, source, title, created_at, updated_at, "
        "topics, is_transient_support FROM conversation_digests WHERE digest_version = ? "
        "ORDER BY conversation_id", (digest_version,)
    ):
        try:
            topics = json.loads(row[6])
        except (TypeError, ValueError):
            topics = []
        out.append({
            "content_hash": row[0], "conversation_id": row[1], "source": row[2],
            "title": row[3], "created_at": row[4], "updated_at": row[5],
            "topics": topics if isinstance(topics, list) else [],
            "is_transient_support": bool(row[7]),
        })
    return out


# --- corpus reading (read-only) ----------------------------------------------

def read_corpus(conn):
    """Conversation rows, oldest first. Read-only by construction: callers
    open conversations.db with mode=ro, and the import agent is its only
    writer.

    Two kinds of the machine's own chatter are excluded here: council_bot.py's
    scratch conversations (reusing the import path's guard) and this module's
    OWN scratch conversations (claude_browser.SCRATCH_TITLE_PREFIX), which are
    real claude.ai conversations whose prompts contain conversation bodies --
    importing one and then digesting it would feed the extractor its own input
    back as evidence. They are normally filtered at import time, but rows
    that predate that guard are still in the DB (3 of them at time of
    writing) -- and they are precisely the conversations that must not shape
    interests, since they contain the machine's own deliberations rather than
    the owner's thinking. Same loop-closure reasoning as step-08: nothing
    this system generated may re-enter its own derivation.
    """
    rows = []
    for r in conn.execute(
        "SELECT source, conversation_id, title, created_at, updated_at, content_hash, raw_json "
        "FROM raw_conversations ORDER BY id"
    ):
        if ets.is_council_bot_scratch_conversation(r[2]) or is_scratch_conversation(r[2]):
            continue
        rows.append({
            "source": r[0], "conversation_id": r[1], "title": r[2],
            "created_at": r[3], "updated_at": r[4], "content_hash": r[5], "raw_json": r[6],
        })
    return rows


def render_conversation(source, raw_json):
    """Same renderer the markdown exports use, sourced from the DB. Reused
    rather than reimplemented so the model reads conversations in the format
    this repo has always produced."""
    data = json.loads(raw_json)
    convert = claude_convert_to_markdown if source == "claude" else chatgpt_convert_to_markdown
    return convert(data, include_metadata=True)


def truncate_middle(text, max_chars):
    """Middle-out truncation: an opening and an ending, with the middle
    elided. Conversations put the question at the top and the conclusion at
    the bottom -- a head-only cut loses whatever the owner actually decided.
    """
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    marker = "\n\n...[{n} characters elided]...\n\n"
    head_len = int(max_chars * 0.6)
    tail_len = max(0, max_chars - head_len - len(marker))
    elided = len(text) - head_len - tail_len
    return text[:head_len] + marker.format(n=elided) + (text[-tail_len:] if tail_len else "")


# --- stage 1: map ------------------------------------------------------------

MAP_SYSTEM = """\
You are reading someone's real AI-chat history to work out what they are \
genuinely, durably interested in -- the themes worth following in a personal \
news-discovery engine -- as opposed to one-off errands.

For EACH conversation you are given, list its topics. A topic is a theme the \
person was engaging with, not a summary of the conversation.

Rules:
- label_en MUST be in English, even when the conversation is in Hebrew or \
mixes Hebrew and English. Write it the way a magazine section would be named: \
concrete and specific ("Binding of Isaac progression and unlocks"), never \
generic ("gaming", "health", "questions").
- lang: the language the CONVERSATION was in ("he", "en", or "mixed").
- register: one of "hobby-question", "task-execution", "research", "support".
  Use "support" for transient errands -- a device that won't connect, a \
printer, an app that broke, a one-off how-do-I. These get discarded, so be \
honest: if it is troubleshooting, say "support".
- depth: 0.0-1.0, how far past the surface the person pushed. A single \
question answered and dropped is ~0.2; sustained back-and-forth where they \
challenged answers and went deeper is ~0.8.
- entities: concrete proper nouns (products, drugs, games, companies, people).
- quote: ONE short verbatim fragment of the PERSON'S OWN words (never the \
assistant's), at most 140 characters, in its original language -- Hebrew stays \
in Hebrew. This is shown to the owner as evidence, so it must be a real \
substring of what they wrote.
- is_transient_support: true if the WHOLE conversation was a one-off errand.
- 1-4 topics per conversation. Fewer is better than padding with vague ones. \
An empty list is a valid answer for an empty or contentless conversation.
"""

MAP_SHAPE = """\
{"conversations": [{"index": <int, the CONVERSATION number given>,
                    "is_transient_support": <bool>,
                    "topics": [{"label_en": "<English label>",
                                "domain": "<one word, e.g. gaming/health/finance/psychology/tech/career>",
                                "lang": "he|en|mixed",
                                "register": "hobby-question|task-execution|research|support",
                                "depth": <0.0-1.0>,
                                "entities": ["..."],
                                "quote": "<=140 chars of the person's own words"}]}]}"""


def build_map_prompt(batch):
    """One prompt covering several conversations. Batching is not an
    optimization here -- it is what makes a 263+ conversation backfill
    finishable through a single shared browser tab."""
    parts = []
    for i, item in enumerate(batch):
        parts.append(
            f"### CONVERSATION {i}\n"
            f"title: {item['title'] or '(untitled)'}\n"
            f"date: {item['created_at'] or 'unknown'}\n"
            f"source: {item['source']}\n\n"
            f"{item['body']}\n"
        )
    return (
        f"{MAP_SYSTEM}\n\n"
        f"Here are {len(batch)} conversations. Return one entry per conversation, "
        f"using the same `index` numbers.\n\n" + "\n".join(parts)
    )


def parse_map_reply(reply, batch_len):
    """Pull per-conversation digests out of the model's reply, indexed.

    Tolerant on purpose: an index the model invented, a topics value that
    isn't a list, a missing register -- none of those should lose the whole
    batch, because a batch is several conversations' worth of work through a
    slow serialized tab. Anything unusable for a given index simply leaves
    that conversation undigested, and the next run retries it.
    """
    out = {}
    entries = reply.get("conversations")
    if not isinstance(entries, list):
        return out
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        try:
            idx = int(entry.get("index"))
        except (TypeError, ValueError):
            continue
        if not 0 <= idx < batch_len:
            continue
        topics = []
        for t in (entry.get("topics") or []):
            if not isinstance(t, dict):
                continue
            label = (t.get("label_en") or "").strip()
            if not label:
                continue
            register = t.get("register")
            if register not in VALID_REGISTERS:
                register = "research"
            quote = (t.get("quote") or "").strip()[:MAX_QUOTE_CHARS]
            try:
                depth = float(t.get("depth"))
            except (TypeError, ValueError):
                depth = 0.0
            topics.append({
                "label_en": label,
                "domain": (t.get("domain") or "").strip().lower() or "other",
                "lang": t.get("lang") if t.get("lang") in ("he", "en", "mixed") else detect_lang(quote or label),
                "register": register,
                "depth": min(max(depth, 0.0), 1.0),
                "entities": [str(e) for e in (t.get("entities") or []) if str(e).strip()][:8],
                "quote": quote,
            })
        out[idx] = {"topics": topics, "is_transient_support": bool(entry.get("is_transient_support"))}
    return out


def run_map(conn, dconn, llm, *, batch_size=DEFAULT_BATCH_SIZE, max_chars=DEFAULT_MAX_CHARS,
            limit=None, max_attempts=DEFAULT_MAX_ATTEMPTS, model="", timeout=420):
    """Digest every not-yet-digested conversation. Resumable: the skip set is
    read from digests.db at the start, and each batch is committed before the
    next one is sent, so an interrupted run loses at most one batch."""
    done = digested_hashes(dconn)
    skip = done | exhausted_hashes(dconn, max_attempts)
    rows = [r for r in read_corpus(conn) if r["content_hash"] not in skip]
    if limit:
        rows = rows[:limit]

    stats = {"digested": 0, "failed": 0, "batches": 0, "skipped_existing": len(done), "empty": 0}
    if not rows:
        log(f"map: nothing to do ({len(done)} conversations already digested)")
        return stats

    log(f"map: {len(rows)} conversations to digest ({len(done)} already done), "
        f"batch size {batch_size}")

    for start in range(0, len(rows), batch_size):
        chunk = rows[start : start + batch_size]
        batch = []
        for row in chunk:
            try:
                body = render_conversation(row["source"], row["raw_json"])
            except (ValueError, KeyError, TypeError) as e:
                # A malformed raw_json is that row's problem, not the batch's.
                record_failure(dconn, row, f"render failed: {e}")
                stats["failed"] += 1
                continue
            item = dict(row)
            item["body"] = truncate_middle(body, max_chars)
            batch.append(item)
        if not batch:
            dconn.commit()
            continue

        stats["batches"] += 1
        prompt = build_map_prompt(batch)
        try:
            reply = llm.complete_json(prompt, MAP_SHAPE, timeout=timeout)
            digests = parse_map_reply(reply, len(batch))
        except (BrowserClaudeError, ValueError, TypeError) as e:
            for row in batch:
                record_failure(dconn, row, e)
            stats["failed"] += len(batch)
            dconn.commit()
            log(f"map: batch {stats['batches']} failed ({e}) -- recorded, continuing")
            continue

        for i, row in enumerate(batch):
            digest = digests.get(i)
            if digest is None:
                record_failure(dconn, row, "no entry for this index in the reply")
                stats["failed"] += 1
                continue
            record_digest(dconn, row, digest, model)
            stats["digested"] += 1
            if not digest["topics"]:
                stats["empty"] += 1
        dconn.commit()  # checkpoint: this batch is now permanently done
        log(f"map: batch {stats['batches']} -- {stats['digested']} digested, "
            f"{stats['failed']} failed, {len(rows) - start - len(chunk)} remaining")

    return stats


# --- aggregation (pure, no LLM) ----------------------------------------------

def _parse_dt(value):
    return ps._parse_ts(value)


def _conv_dt(digest):
    """A conversation's date for durability purposes. created_at first: a
    theme's span should reflect when the owner was thinking about it, and
    updated_at moves whenever an old conversation is touched."""
    return _parse_dt(digest.get("created_at")) or _parse_dt(digest.get("updated_at"))


def aggregate_themes(digests, *, now=None):
    """Collapse per-conversation topics into themes with durability stats.

    Pure and deterministic -- this is the half of the extractor that can be
    tested without a browser, and it is where the design's measured
    durability axes (n_convs, active_months, span, recency, depth) are
    actually computed. Support-register topics and whole conversations the
    map pass flagged as transient errands are dropped here rather than
    downstream, so they can never reach a candidate.
    """
    now = now or datetime.now(timezone.utc)
    themes = {}
    for d in digests:
        if d.get("is_transient_support"):
            continue
        dt = _conv_dt(d)
        for topic in d.get("topics") or []:
            if topic.get("register") == SUPPORT_REGISTER:
                continue
            label = (topic.get("label_en") or "").strip()
            if not label:
                continue
            key = theme_key(label)
            th = themes.setdefault(key, {
                "key": key, "labels": {}, "domains": {}, "langs": {}, "entities": {},
                "conversations": set(), "dates": [], "depths": [], "evidence": [],
            })
            th["labels"][label] = th["labels"].get(label, 0) + 1
            th["domains"][topic.get("domain") or "other"] = th["domains"].get(topic.get("domain") or "other", 0) + 1
            th["langs"][topic.get("lang") or "und"] = th["langs"].get(topic.get("lang") or "und", 0) + 1
            for ent in topic.get("entities") or []:
                th["entities"][ent] = th["entities"].get(ent, 0) + 1
            th["conversations"].add(d["conversation_id"])
            if dt is not None:
                th["dates"].append(dt)
            th["depths"].append(float(topic.get("depth") or 0.0))
            quote = (topic.get("quote") or "").strip()
            if quote:
                th["evidence"].append({
                    "date": (dt.strftime("%Y-%m-%d") if dt else None),
                    "quote": quote[:MAX_QUOTE_CHARS],
                    "lang": topic.get("lang") or detect_lang(quote),
                    "depth": round(float(topic.get("depth") or 0.0), 2),
                    # The design's §6.4 boundary said dates only; the owner
                    # explicitly asked for conversation ids so an offer can be
                    # traced back to the conversation it came from. Privacy is
                    # a non-issue for the owner's own data (see the contract's
                    # v2 section) -- this is a deliberate, documented choice.
                    "conversation_id": d["conversation_id"],
                })

    out = []
    for key, th in themes.items():
        dates = sorted(th["dates"])
        first, last = (dates[0], dates[-1]) if dates else (None, None)
        span_days = int((last - first).total_seconds() // 86400) if dates else 0
        active_months = len({(d.year, d.month) for d in dates})
        recency_days = int((now - last).total_seconds() // 86400) if last else 10 ** 6
        depths = th["depths"] or [0.0]
        # Evidence is sorted newest-first so a trimmed list keeps the quotes
        # the owner will actually recognise.
        evidence = sorted(th["evidence"], key=lambda e: (e["date"] or ""), reverse=True)
        out.append({
            "key": key,
            "label": max(sorted(th["labels"]), key=lambda l: (th["labels"][l], -len(l))),
            "labels": sorted(th["labels"]),
            "domain": max(sorted(th["domains"]), key=lambda d: th["domains"][d]),
            "langs": sorted(th["langs"]),
            "entities": sorted(th["entities"], key=lambda e: (-th["entities"][e], e))[:8],
            "months": sorted({d.strftime("%Y-%m") for d in dates}),
            "n_convs": len(th["conversations"]),
            "conversation_ids": sorted(th["conversations"]),
            "first_seen": first.strftime("%Y-%m-%d") if first else None,
            "last_seen": last.strftime("%Y-%m-%d") if last else None,
            "span_days": span_days,
            "active_months": active_months,
            "recency_days": recency_days,
            "depth_max": round(max(depths), 2),
            "depth_mean": round(sum(depths) / len(depths), 2),
            "evidence": evidence[:MAX_EVIDENCE_QUOTES],
            "durability": classify_durability(span_days, active_months),
        })
    # Deterministic: strongest evidence first, key ascending to break ties.
    out.sort(key=lambda t: (-t["n_convs"], -t["active_months"], t["key"]))
    return out


def classify_durability(span_days, active_months):
    """The design's measured separator (§3.1), applied verbatim.

    Note the asymmetry is intentional: "durable" needs BOTH a >=30 day span
    and >=2 calendar months (a fortnight of daily conversations is a binge,
    not a durable interest), while "transient" is the single sub-7-day burst.
    Everything in between is "emerging" -- real but not yet proven, which is
    the population the offer floors then adjudicate.
    """
    if span_days >= DURABLE_MIN_SPAN_DAYS and active_months >= DURABLE_MIN_ACTIVE_MONTHS:
        return "durable"
    if span_days < TRANSIENT_MAX_SPAN_DAYS:
        return "transient"
    return "emerging"


def passes_durability_gate(theme):
    """Design §5.2's floors, before an aggregated theme may be offered."""
    if theme["n_convs"] >= GATE_MIN_CONVS and theme["active_months"] >= GATE_MIN_ACTIVE_MONTHS:
        return True
    return theme["n_convs"] >= GATE_DEEP_MIN_CONVS and theme["depth_max"] >= GATE_DEEP_MIN_DEPTH


def score_candidate(theme, *, expected_yield=0.5, max_similarity=0.0):
    """Design §5.2's composite. Code ranks; the model only rates.

    Returns (score, terms) with every term kept for the provenance UI -- the
    owner should be able to see WHY an offer outranked another, not just that
    it did.
    """
    n = max(theme.get("n_convs") or 0, 0)
    terms = {
        "evidence_strength": min(math.log(1 + n) / math.log(1 + EVIDENCE_SATURATION), 1.0),
        "recurrence": min((theme.get("active_months") or 0) / RECURRENCE_MONTHS, 1.0),
        "recency": 0.5 ** ((theme.get("recency_days") or 0) / RECENCY_HALF_LIFE_DAYS),
        "novelty": 1.0 - min(max(max_similarity, 0.0), 1.0),
        "expected_yield": min(max(expected_yield, 0.0), 1.0),
    }
    score = sum(WEIGHTS[k] * v for k, v in terms.items())
    return round(score, 4), {k: round(v, 4) for k, v in terms.items()}


# --- stage 2: reduce ---------------------------------------------------------

REDUCE_SYSTEM = """\
You curate a personal news-discovery engine. Below are themes aggregated from \
the owner's own AI conversations, with how many conversations each appeared \
in and over how long, plus the interests the engine ALREADY follows.

Propose interests the engine should FOLLOW: durable curiosities, not one-off \
errands. Write key, title, description, positive_signals and negative_signals \
in English, in the house style of the existing interests shown, regardless of \
the source language of the evidence.

Rules:
- NEVER propose something an existing interest already covers. If a theme is \
close to an existing interest, do not propose it as new -- either skip it, or \
propose kind="merge" naming the existing key.
- Prefer proposing a connection ("bridge") between two existing interests over \
a near-duplicate of either. A bridge names both parents in related_keys.
- similarity_to_existing MUST rate the candidate against every existing \
interest it resembles at all, 0.0-1.0. This is how duplicates get caught \
across languages, so be honest and thorough -- a Hebrew-evidenced theme that \
restates an English interest is a 0.9, not a 0.2.
- expected_yield, 0.0-1.0: will a web search actually produce fresh material \
on this WEEKLY? A deeply personal errand scores low even with strong evidence. \
A fast-moving public field scores high. Give a one-line rationale.
- Mark exactly one candidate exploratory=true: a deliberate wildcard.
- Merge themes that are the same interest in different languages or phrasings \
(Hebrew and English versions of one topic, or a recurring misspelling) into a \
single candidate, and list every source theme_key you merged in from_theme_keys.
"""

REDUCE_SHAPE = """\
{"candidates": [{"kind": "new|bridge|merge|split",
                 "key": "<lowercase-hyphenated-english-key>",
                 "title": "<short English title>",
                 "description": "<2-4 sentences, house style>",
                 "positive_signals": ["..."], "negative_signals": ["..."],
                 "suggested_min_score": <0.0-1.0>,
                 "parent_key": "<existing interest key or null>",
                 "related_keys": ["..."],
                 "from_theme_keys": ["<theme_key values this came from>"],
                 "expected_yield": <0.0-1.0>,
                 "expected_yield_rationale": "<one line>",
                 "exploratory": <bool>,
                 "similarity_to_existing": [{"key": "<existing key>", "similarity": <0.0-1.0>}]}]}"""


def load_existing_interests(path):
    """Read the consumer's interests.json, fail-soft.

    Deliberately tolerant: this producer must not break because the other
    repo moved a file or is mid-edit. No interests file just means every
    candidate looks novel, which the owner will notice immediately in the
    offers -- far better than refusing to run.
    """
    if not path:
        return [], []
    p = Path(path)
    if not p.exists():
        log(f"reduce: no interests file at {p} -- novelty will be unmeasured")
        return [], []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        log(f"reduce: could not read {p} ({e}) -- continuing without it")
        return [], []
    items = data.get("interests") if isinstance(data, dict) else data
    interests = []
    for it in items or []:
        if isinstance(it, dict) and it.get("key"):
            interests.append({
                "key": it["key"],
                "title": it.get("title") or "",
                "description": (it.get("description") or "")[:400],
            })
    blocked = []
    if isinstance(data, dict):
        blocked = data.get("blocked_derived_terms") or (data.get("defaults") or {}).get("blocked_derived_terms") or []
    return interests, [str(b) for b in blocked]


def build_reduce_prompt(themes, interests, blocked, *, max_themes=DEFAULT_MAX_THEMES,
                        inline_limit=20000):
    """Build the reduce call as a (prompt, context) pair.

    Two things this has to get right, both learned from the real corpus:

    1. The map pass writes deliberately SPECIFIC labels ("Binding of Isaac
       progression and unlocks"), so almost every theme holds exactly one
       conversation and label-key aggregation merges almost nothing.
       Essentially all of the merging happens here, in the model's semantic
       pass -- which means truncating the theme list to a round number would
       silently decide which of the owner's interests are allowed to exist.
       So themes are grouped by domain (the clusters the owner would
       recognise) and, once the block gets large, shipped as an uploaded
       context FILE instead of being cut down.
    2. Quotes are NOT sent. The model's job is naming and de-duplication;
       evidence is attached afterwards from local data, which is what keeps
       the quotes verbatim rather than paraphrased.
    """
    by_domain = {}
    for t in themes[:max_themes]:
        by_domain.setdefault(t["domain"], []).append(t)
    # Biggest domains first, and inside a domain the most-discussed themes
    # first -- so if anything ever is cut, it is the thinnest tail.
    ordered = sorted(by_domain.items(),
                     key=lambda kv: (-sum(t["n_convs"] for t in kv[1]), kv[0]))

    blocks = []
    for domain, group in ordered:
        group = sorted(group, key=lambda t: (-t["n_convs"], t["recency_days"], t["key"]))
        lines = [json.dumps({
            "theme_key": t["key"], "label": t["label"], "langs": t["langs"],
            "entities": t["entities"][:5], "n_convs": t["n_convs"],
            "active_months": t["active_months"], "span_days": t["span_days"],
            "recency_days": t["recency_days"], "depth_max": t["depth_max"],
            "durability": t["durability"], "first_seen": t["first_seen"],
            "last_seen": t["last_seen"],
        }, ensure_ascii=False) for t in group]
        header = (f"### DOMAIN: {domain} ({len(group)} themes, "
                  f"{sum(t['n_convs'] for t in group)} conversations)")
        blocks.append(header + "\n" + "\n".join(lines))

    total = sum(len(g) for _, g in ordered)
    theme_block = (f"## THEMES FROM THE OWNER'S CONVERSATIONS ({total})\n"
                   + "\n\n".join(blocks))

    interest_lines = [json.dumps(i, ensure_ascii=False) for i in interests]
    head = (
        REDUCE_SYSTEM
        + f"\n\n## INTERESTS ALREADY FOLLOWED ({len(interest_lines)})\n"
        + ("\n".join(interest_lines) if interest_lines else "(none)")
        + "\n\n## BLOCKED TERMS (never propose these)\n"
        + (", ".join(blocked) or "(none)")
        + "\n\nPropose up to 30 candidates, best first. Most themes below hold a single "
          "conversation, because the labels are deliberately specific -- so MERGING is the "
          "main job: gather the themes that are really one interest (across domains, "
          "phrasings and languages) into one candidate, and list every one of them in "
          "from_theme_keys. A candidate's evidence and durability are recomputed locally "
          "from the themes you name, so naming all of them is what gives a candidate its "
          "weight. A candidate that names one theme out of fifteen looks fifteen times "
          "weaker than it is."
    )
    if len(theme_block) > inline_limit:
        # Uploaded as a file, exactly as council_bot.py ships large context.
        return head + "\n\nThe themes are in the attached file.", theme_block
    return head + "\n\n" + theme_block, None


def parse_reduce_reply(reply):
    """Normalize the model's candidate list. Tolerant for the same reason
    parse_map_reply is: one malformed candidate must not cost the run."""
    out = []
    for c in (reply.get("candidates") or []):
        if not isinstance(c, dict):
            continue
        title = (c.get("title") or "").strip()
        key = slugify(c.get("key") or title)
        if not title and not c.get("key"):
            continue
        sims = []
        for s in (c.get("similarity_to_existing") or []):
            if not isinstance(s, dict) or not s.get("key"):
                continue
            try:
                sims.append({"key": str(s["key"]),
                              "sim": min(max(float(s.get("sim", s.get("similarity")) or 0.0), 0.0), 1.0)})
            except (TypeError, ValueError):
                continue
        try:
            yield_ = float(c.get("expected_yield"))
        except (TypeError, ValueError):
            yield_ = 0.5
        kind = c.get("kind") if c.get("kind") in ("new", "bridge", "merge", "split", "revive") else "new"
        out.append({
            "kind": kind,
            "key": key,
            "title": title or key,
            "description": (c.get("description") or "").strip(),
            "positive_signals": [str(x) for x in (c.get("positive_signals") or []) if str(x).strip()],
            "negative_signals": [str(x) for x in (c.get("negative_signals") or []) if str(x).strip()],
            "suggested_min_score": c.get("suggested_min_score"),
            "suggested_sources": ["web_search"],
            "parent_key": c.get("parent_key") or None,
            "related_keys": [str(x) for x in (c.get("related_keys") or []) if str(x).strip()],
            "from_theme_keys": [str(x) for x in (c.get("from_theme_keys") or []) if str(x).strip()],
            "expected_yield": min(max(yield_, 0.0), 1.0),
            "expected_yield_rationale": (c.get("expected_yield_rationale") or "").strip(),
            "exploratory": bool(c.get("exploratory")),
            "similarity_to_existing": sims,
        })
    return out


def attach_evidence(candidates, themes):
    """Join each candidate back to the themes it came from, locally.

    The model names and de-duplicates; the numbers and the quotes come from
    aggregate_themes(). That split is the point: a candidate can never claim
    durability it does not have, and quotes stay verbatim because the model
    never had the chance to paraphrase them.

    A candidate whose from_theme_keys don't match anything falls back to a
    label match, and failing that keeps zeroed durability -- which the floors
    then reject, so an invented candidate cannot reach the owner.
    """
    by_key = {t["key"]: t for t in themes}
    by_label = {t["label"].lower(): t for t in themes}
    enriched = []
    for cand in candidates:
        matched = []
        for tk in cand["from_theme_keys"]:
            t = by_key.get(tk) or by_key.get(theme_key(tk)) or by_label.get(tk.lower())
            if t is not None and t not in matched:
                matched.append(t)
        if not matched:
            t = by_label.get(cand["title"].lower()) or by_key.get(theme_key(cand["title"]))
            if t is not None:
                matched = [t]

        conv_ids, dates, evidence, depths, months = set(), [], [], [0.0], set()
        for t in matched:
            conv_ids.update(t["conversation_ids"])
            dates += [d for d in (t["first_seen"], t["last_seen"]) if d]
            evidence += t["evidence"]
            depths.append(t["depth_max"])
            # UNION of the calendar months each theme was actually active in,
            # not the max of their counts. The map pass writes very specific
            # labels ("Binding of Isaac progression and unlocks"), so most
            # themes hold a single conversation and the real recurrence only
            # appears once the reduce pass merges them into one candidate. Max
            # would report active_months=1 for a fourteen-conversation,
            # seven-month cluster and fail it at the durability gate -- the
            # exact cluster this extractor exists to surface.
            months.update(t.get("months") or [])
        first = min(dates) if dates else None
        last = max(dates) if dates else None
        span = 0
        if first and last:
            span = (datetime.strptime(last, "%Y-%m-%d") - datetime.strptime(first, "%Y-%m-%d")).days
        recency = min((t["recency_days"] for t in matched), default=10 ** 6)
        durability = {
            "n_convs": len(conv_ids),
            "active_months": len(months),
            "span_days": span,
            "recency_days": recency,
            "depth_max": max(depths),
            "class": classify_durability(span, len(months)),
        }
        # Newest first, but at most one quote per conversation: four quotes
        # from four different conversations show the owner a theme actually
        # recurring, while four from one conversation just show that
        # conversation four times. Provenance is the whole point of quoting.
        seen_convs, picked = set(), []
        for e in sorted(evidence, key=lambda e: (e["date"] or ""), reverse=True):
            conv = e.get("conversation_id")
            if conv and conv in seen_convs:
                continue
            seen_convs.add(conv)
            picked.append(e)
            if len(picked) >= MAX_EVIDENCE_QUOTES:
                break

        cand = dict(cand)
        cand["durability"] = durability
        cand["evidence"] = picked
        cand["source_themes"] = [t["key"] for t in matched]
        cand["source_conversations"] = sorted(conv_ids)
        enriched.append(cand)
    return enriched


def rank_candidates(candidates, *, max_offers=MAX_OFFERS_PER_RUN):
    """Score, floor, and pick what to actually offer.

    Two floors, both from §5.2: the durability gate (measured, local) and the
    composite score floor. A candidate that fails either is still shipped in
    the artifact -- the consumer decides what to do with it -- but is not
    marked as an offer, so the owner's inbox stays a five-row decision.

    The serendipity slot is a lane, not a weight: one exploratory candidate
    that cleared the floors is promoted even if it did not make the top five,
    so the inbox is never purely the safest picks.
    """
    scored = []
    for cand in candidates:
        theme_like = dict(cand["durability"])
        max_sim = max((s["sim"] for s in cand["similarity_to_existing"]), default=0.0)
        score, terms = score_candidate(theme_like, expected_yield=cand["expected_yield"],
                                        max_similarity=max_sim)
        cand = dict(cand)
        cand["score"] = score
        cand["score_terms"] = terms
        cand["max_similarity"] = round(max_sim, 4)
        cand["passes_durability_gate"] = passes_durability_gate({
            "n_convs": theme_like["n_convs"], "active_months": theme_like["active_months"],
            "depth_max": theme_like["depth_max"],
        })
        cand["qualified"] = bool(cand["passes_durability_gate"] and score >= OFFER_SCORE_FLOOR)
        scored.append(cand)

    scored.sort(key=lambda c: (-c["score"], c["key"]))
    qualified = [c for c in scored if c["qualified"]]
    offered = qualified[:max_offers]
    if not any(c["exploratory"] for c in offered):
        for c in qualified[max_offers:]:
            if c["exploratory"]:
                offered = offered[: max(0, max_offers - 1)] + [c]
                break
    offered_keys = {c["key"] for c in offered}
    for c in scored:
        c["offered"] = c["key"] in offered_keys
    return scored


def corpus_stats(conn, digests):
    """Coverage the consumer needs to read a candidate honestly.

    The corpus is being backfilled continuously, so an artifact has to say
    what it was derived from -- otherwise a candidate generated from 15% of
    the history is indistinguishable from one generated from all of it.
    """
    total = conn.execute("SELECT COUNT(*) FROM raw_conversations").fetchone()[0]
    by_source = dict(conn.execute(
        "SELECT source, COUNT(*) FROM raw_conversations GROUP BY source").fetchall())
    rng = conn.execute("SELECT MIN(created_at), MAX(created_at) FROM raw_conversations").fetchone()
    return {
        "conversations_in_db": total,
        "conversations_digested": len(digests),
        "by_source": by_source,
        "created_at_range": [rng[0], rng[1]],
        "coverage": round(len(digests) / total, 4) if total else 0.0,
    }


def build_artifact(conn, digests, themes, candidates, *, window_days=180):
    """Contract v2: v1's topics[] plus candidates[]."""
    v1 = ps.derive(conn, window_days=window_days)
    return {
        "contract_version": CONTRACT_VERSION,
        "generated_at": _now_iso(),
        "generator": "interest_extractor.py",
        "window_days": window_days,
        "conversation_count": v1["conversation_count"],
        "sources": v1["sources"],
        # v1 consumers read this untouched. NOTE: both pre-registered evals
        # FALSIFIED on 2026-08-18, so these topics are descriptive only and
        # must not be used as a scoring input -- see the experiment docs.
        "topics": v1["topics"],
        "corpus": corpus_stats(conn, digests),
        "themes_considered": len(themes),
        "candidates": candidates,
    }


def run_reduce(conn, dconn, llm, *, interests_path=None, out_path=DEFAULT_OUT,
               max_candidates=30, max_themes=DEFAULT_MAX_THEMES, timeout=900, now=None):
    digests = load_digests(dconn)
    if not digests:
        raise SystemExit("reduce: no digests yet -- run `interest_extractor.py map` first")
    themes = aggregate_themes(digests, now=now)
    gated = [t for t in themes if passes_durability_gate(t)]
    interests, blocked = load_existing_interests(interests_path)
    log(f"reduce: {len(themes)} themes ({len(gated)} clear the durability gate), "
        f"{len(interests)} existing interests, {len(digests)} digests")

    prompt, context = build_reduce_prompt(gated or themes, interests, blocked,
                                           max_themes=max_themes)
    reply = llm.complete_json(prompt, REDUCE_SHAPE, timeout=timeout, context=context)
    candidates = parse_reduce_reply(reply)
    candidates = attach_evidence(candidates, themes)
    candidates = rank_candidates(candidates)[:max_candidates]

    artifact = build_artifact(conn, digests, themes, candidates)
    ps.write(out_path, artifact)
    offered = [c for c in candidates if c["offered"]]
    log(f"reduce: {len(candidates)} candidates ({len(offered)} offered) -> {out_path}")
    return artifact


# --- CLI ---------------------------------------------------------------------

def _open_corpus(path):
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _default_interests_path():
    """The consumer's interests.json, if this machine has the sibling repo.
    An env override exists because the two repos are only conventionally
    siblings, and nothing here should hard-depend on that."""
    env = os.environ.get("DISCOVERY_INTERESTS_JSON")
    if env:
        return env
    sibling = REPO_ROOT.parent / "internet" / "interests.json"
    return str(sibling) if sibling.exists() else None


def cmd_status(args):
    conn = _open_corpus(args.db)
    dconn = open_digests(args.digests)
    try:
        digests = load_digests(dconn)
        stats = corpus_stats(conn, digests)
        pending = stats["conversations_in_db"] - stats["conversations_digested"]
        failures = dconn.execute(
            "SELECT COUNT(*), COALESCE(MAX(attempts), 0) FROM digest_failures WHERE digest_version = ?",
            (DIGEST_VERSION,)).fetchone()
        themes = aggregate_themes(digests)
        gated = [t for t in themes if passes_durability_gate(t)]
        print(json.dumps({
            "corpus": stats,
            "pending_conversations": pending,
            "failed_conversations": failures[0],
            "max_attempts_seen": failures[1],
            "themes": len(themes),
            "themes_passing_gate": len(gated),
            "durability_mix": {
                k: sum(1 for t in themes if t["durability"] == k)
                for k in ("durable", "emerging", "transient")
            },
        }, indent=2, ensure_ascii=False))
    finally:
        conn.close()
        dconn.close()


def cmd_map(args):
    conn = _open_corpus(args.db)
    dconn = open_digests(args.digests)
    llm = BrowserClaude(port=args.port, model=args.model, log=log)
    ok, why = llm.preflight()
    if not ok:
        raise SystemExit(f"map: claude.ai is not reachable -- {why}")
    started = time.monotonic()
    try:
        stats = run_map(conn, dconn, llm, batch_size=args.batch_size, max_chars=args.max_chars,
                        limit=args.limit, max_attempts=args.max_attempts, model=args.model,
                        timeout=args.timeout)
    finally:
        llm.close()
        conn.close()
        dconn.close()
    log(f"map done in {(time.monotonic() - started) / 60:.1f}m: {stats}")


def cmd_reduce(args):
    conn = _open_corpus(args.db)
    dconn = open_digests(args.digests)
    llm = BrowserClaude(port=args.port, model=args.model, log=log)
    ok, why = llm.preflight()
    if not ok:
        raise SystemExit(f"reduce: claude.ai is not reachable -- {why}")
    try:
        run_reduce(conn, dconn, llm, interests_path=args.interests, out_path=args.out,
                   max_candidates=args.max_candidates, timeout=args.timeout)
    finally:
        llm.close()
        conn.close()
        dconn.close()


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=DEFAULT_DB, help="conversations.db (opened read-only)")
    parser.add_argument("--digests", default=DEFAULT_DIGESTS, help="this module's own state db")
    parser.add_argument("--port", type=int, default=9222, help="Chrome --remote-debugging-port")
    parser.add_argument("--model", default="claude-opus-5")
    sub = parser.add_subparsers(dest="command", required=True)

    p_status = sub.add_parser("status", help="offline: coverage, themes, durability mix")
    p_status.set_defaults(func=cmd_status)

    p_map = sub.add_parser("map", help="digest conversations (live; resumable)")
    p_map.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    p_map.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS)
    p_map.add_argument("--limit", type=int, default=None)
    p_map.add_argument("--max-attempts", type=int, default=DEFAULT_MAX_ATTEMPTS)
    p_map.add_argument("--timeout", type=int, default=420)
    p_map.set_defaults(func=cmd_map)

    p_red = sub.add_parser("reduce", help="aggregate digests into candidates (live)")
    p_red.add_argument("--out", default=DEFAULT_OUT)
    p_red.add_argument("--interests", default=_default_interests_path())
    p_red.add_argument("--max-candidates", type=int, default=30)
    p_red.add_argument("--timeout", type=int, default=900)
    p_red.set_defaults(func=cmd_reduce)

    return parser


def main():
    sys.stdout.reconfigure(errors="replace")
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
