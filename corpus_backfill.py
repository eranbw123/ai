#!/usr/bin/env python3
"""Slow, resumable, project-aware backfill of the whole conversation corpus.

`export_to_sqlite.py` is the right tool for an incremental poll: it walks the
flat conversation list newest-first and stops when it runs out of time budget.
This module exists for the different job of making the corpus *complete*:

  * ChatGPT **Projects** conversations are not reliably in the flat
    `/backend-api/conversations` list. Measured live 2026-08-18 on this
    account: of 14 conversations sitting inside 5 projects, only 1 appeared in
    the flat list -- the other 13 (e.g. "Live XYZ Data Verification",
    updated 2026-08-16T15:53Z) were absent from a flat page that covered
    2026-08-18 down to 2026-08-07, so it is not a paging artifact. Anything
    that only walks the flat list therefore silently misses most project work.
    Projects are enumerated separately here via
    `/backend-api/gizmos/snorlax/sidebar` + `/backend-api/gizmos/{id}/conversations`
    and merged into one manifest. (`?gizmo_id=` on the flat list endpoint is
    silently ignored -- verified live -- so it is not an alternative.)
  * A full pass is thousands of requests against an account that chatgpt.com
    has already throttled hard once (see `fetch_conversation_with_retry`'s and
    `chatgpt_export_cdp.fetch_all_conversation_summaries`'s docstrings for the
    2026-08-05/06 incidents). So the pace here is deliberately slow, jittered,
    self-penalising on any rate-limit signal, and resumable at
    single-conversation granularity -- there is no deadline, and a run that
    stops early must never cost re-work.

Two phases, so the expensive per-conversation phase always works off a fixed,
inspectable plan rather than re-listing:

    python corpus_backfill.py manifest          # cheap: ~20 list requests
    python corpus_backfill.py run               # slow: one detail fetch per missing conversation
    python corpus_backfill.py status            # read-only report, no browser

`run` is idempotent and resumable: re-running the exact same command picks up
where it left off. It skips anything whose stored `updated_at` already matches
the manifest (no network call at all -- the fix that made re-runs cheap, see
run_chatgpt() in export_to_sqlite.py), commits after *every* conversation, and
appends one line per outcome to `backfill_progress.jsonl`.

Chrome tab etiquette: this opens its **own** tab and closes only that tab. The
Chrome on port 9222 is shared with a live discovery appliance and other agents;
never close a tab you did not open, and never restart that browser.
"""
import argparse
import json
import os
import random
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import cdp
import chatgpt_export_cdp as gc
import claude_browser
import claude_export_cdp as cc
from chatgpt_export import parse_api_timestamp as gpt_ts
from claude_export import parse_api_timestamp as claude_ts
from export_to_sqlite import (
    SCHEMA,
    fetch_conversation_with_retry,
    find_chatgpt_model,
    import_lock,
    is_council_bot_scratch_conversation,
    norm_ts,
    upsert,
)

REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_DB = REPO_ROOT / "conversations.db"

# A project ("gizmo") name is a topic the owner declared out loud -- a much
# stronger interest signal than an inferred one -- so it is worth keeping even
# though raw_conversations has nowhere to put it. A side table rather than a
# new column on raw_conversations: purely additive, so no existing query,
# `SELECT *` consumer or positional INSERT anywhere else can be affected by it.
PROJECT_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversation_projects (
    source          TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    project_id      TEXT NOT NULL,
    project_name    TEXT,
    recorded_at     TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (source, conversation_id)
);
"""


# ---------------------------------------------------------------- tab etiquette

def _devtools(port, path, method="GET"):
    req = urllib.request.Request(f"http://localhost:{port}{path}", method=method)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8")


@contextmanager
def own_tab(port, url, *, load_timeout=90):
    """Open a tab we own, yield a CDPConnection to it, then close only that tab.

    Deliberately points at `<origin>/robots.txt` rather than the app itself:
    all we need is a JS context on the right origin so `fetch(..., credentials)`
    carries the real session, and loading the actual SPA would make the page
    fire its own background API calls -- extra request volume against exactly
    the endpoints we are trying not to get throttled on. A text/plain document
    still gets a normal Runtime.evaluate context (verified live).

    Opening our own tab (rather than reusing whatever claude.ai/chatgpt.com tab
    happens to be open) is not politeness for its own sake: this Chrome is
    shared with a live discovery appliance that collects every 60s, and
    navigating or closing its tab would break it.
    """
    quoted = urllib.parse.quote(url, safe=":/?=&")
    info = None
    for method in ("PUT", "GET"):  # Chrome flipped /json/new to PUT-only mid-versions
        try:
            info = json.loads(_devtools(port, f"/json/new?{quoted}", method))
            break
        except urllib.error.HTTPError:
            continue
    if info is None:
        raise RuntimeError(f"could not open a new tab on the Chrome at port {port}")

    conn = None
    try:
        deadline = time.monotonic() + load_timeout
        while True:
            try:
                conn = cdp.CDPConnection(info["webSocketDebuggerUrl"])
                if conn.evaluate("document.readyState") in ("interactive", "complete"):
                    break
                conn.close()
                conn = None
            except Exception:  # noqa: BLE001 -- a fresh tab is briefly unattachable
                if conn is not None:
                    conn.close()
                conn = None
            if time.monotonic() >= deadline:
                raise RuntimeError(f"tab for {url} never finished loading")
            time.sleep(1.0)
        yield conn
    finally:
        if conn is not None:
            conn.close()
        try:
            _devtools(port, f"/json/close/{info['id']}")
        except Exception:  # noqa: BLE001 -- best effort; never mask the real error
            pass


# ---------------------------------------------------------------- pacing

# Anything in this family means "the server is pushing back", and the only
# correct response is to get much slower, not to retry tightly.
RATE_LIMIT_RE = re.compile(r"HTTP (429|403|418|502|503|529)|rate.?limit|too many requests", re.I)
# The observed shape of chatgpt.com throttling killing the CDP socket itself
# (WinError 10053 in the 2026-08-06 run), which needs a reconnect, not a retry.
CONNECTION_LOST_RE = re.compile(
    r"10053|10054|WebSocket connection closed|connection was aborted|"
    r"forcibly closed|Broken pipe|timed out|handshake",
    re.I,
)


def is_rate_limited(exc):
    return bool(RATE_LIMIT_RE.search(str(exc)))


def is_connection_lost(exc):
    return isinstance(exc, (ConnectionError, TimeoutError)) or bool(
        CONNECTION_LOST_RE.search(str(exc))
    )


class Pacer:
    """Jittered per-item pacing with a multiplicative penalty.

    The jitter matters more than the mean: a fixed sleep produces a perfectly
    periodic request train, which is exactly the signature naive rate limiters
    key on. The penalty doubles on every rate-limit signal and decays only
    slowly on success, so a throttled run degrades to a crawl and *stays* there
    for a while instead of oscillating straight back into the limiter -- the
    failure mode of the 2026-08-06 run, which retried its way into a much
    deeper throttle.
    """

    def __init__(self, base=4.0, jitter=0.4, long_pause_every=25, long_pause=60.0,
                 max_penalty=32.0, rate_limit_pause=90.0, max_rate_limit_pause=1800.0):
        self.base = base
        self.jitter = jitter
        self.long_pause_every = long_pause_every
        self.long_pause = long_pause
        self.max_penalty = max_penalty
        self.rate_limit_pause = rate_limit_pause
        self.max_rate_limit_pause = max_rate_limit_pause
        self.penalty = 1.0
        self.count = 0

    def delay(self):
        span = self.base * self.penalty
        return max(0.5, span * (1.0 + random.uniform(-self.jitter, self.jitter)))

    def sleep(self, sleeper=time.sleep):
        self.count += 1
        sleeper(self.delay())
        if self.long_pause_every and self.count % self.long_pause_every == 0:
            sleeper(self.long_pause * self.penalty)

    def penalize(self):
        self.penalty = min(self.max_penalty, self.penalty * 2.0)
        return self.penalty

    def relax(self):
        if self.penalty > 1.0:
            self.penalty = max(1.0, self.penalty / 1.5)
        return self.penalty

    def rate_limit_backoff(self, sleeper=time.sleep):
        """Hard stop-and-back-off, returning how long it waited."""
        self.penalize()
        wait = min(self.max_rate_limit_pause, self.rate_limit_pause * self.penalty)
        sleeper(wait)
        return wait


# ---------------------------------------------------------------- manifest

def _js_get(path, token=None):
    if token:
        headers = ("{ Accept: 'application/json', Authorization: 'Bearer %s', "
                   "'X-Authorization': 'Bearer %s' }" % (token, token))
        creds = ""
    else:
        headers = "{ Accept: 'application/json' }"
        creds = "credentials: 'include', "
    return ("(async () => { const r = await fetch('%s', { %sheaders: %s });"
            " if (!r.ok) throw new Error('HTTP ' + r.status);"
            " return await r.json(); })()" % (path, creds, headers))


def _entry(source, conv, *, project_id=None, project_name=None):
    if source == "chatgpt":
        cid, title = conv.get("id"), conv.get("title")
        created, updated = gpt_ts(conv.get("create_time")), gpt_ts(conv.get("update_time"))
        project_id = project_id or conv.get("gizmo_id") or None
    else:
        cid, title = conv.get("uuid"), conv.get("name")
        created, updated = claude_ts(conv.get("created_at")), claude_ts(conv.get("updated_at"))
        project_id = project_id or conv.get("project_uuid") or None
        if project_name is None and isinstance(conv.get("project"), dict):
            project_name = conv["project"].get("name")
    return {
        "source": source,
        "conversation_id": cid,
        "title": title,
        "created_at": norm_ts(created),
        "updated_at": norm_ts(updated),
        "project_id": project_id,
        "project_name": project_name,
    }


# `/backend-api/gizmos/{id}/conversations` silently returns `{"items": []}` --
# a 200, not an error -- for any `limit` above its (undocumented) cap. Measured
# live 2026-08-18 against a project with 6 conversations: limit=100 -> 0 items,
# limit=50/28/20 -> all 6. A first pass at limit=100 therefore reported all 57
# projects as empty and would have silently skipped every project-only
# conversation in the corpus. Same family as the `total` and empty-page traps
# already documented in chatgpt_export_cdp.py: on this API an empty response is
# a failure signal, never a trustworthy "there is nothing here".
PROJECT_CONVERSATIONS_PAGE_LIMIT = 50


def fetch_chatgpt_projects(conn, token, pacer, *,
                           page_limit=PROJECT_CONVERSATIONS_PAGE_LIMIT,
                           max_pages=50, empty_retries=2):
    """Enumerate every ChatGPT project and every conversation inside each.

    Two levels of cursor pagination: the sidebar pages over projects, and each
    project pages over its own conversations. The sidebar returns a non-null
    `cursor` even when it has already handed back everything, so end-of-data is
    detected by "this page added no project we had not already seen" rather
    than by trusting the cursor -- the same "do not trust the server's own
    end-of-data signal" lesson that `total` taught us on the flat list.

    A project's *first* page coming back empty is retried before being believed
    (see PROJECT_CONVERSATIONS_PAGE_LIMIT above and CLAUDE.md's rule that an
    unexpected empty response is a failure to retry, not end-of-data). A later
    page is allowed to be empty -- that is ordinary end-of-data.
    """
    projects, seen, cursor = [], set(), ""
    for _ in range(max_pages):
        path = ("/backend-api/gizmos/snorlax/sidebar?conversations_per_gizmo=1"
                f"&cursor={urllib.parse.quote(cursor)}")
        page = conn.evaluate(_js_get(path, token))
        items = (page or {}).get("items") or []
        fresh = 0
        for item in items:
            giz = ((item or {}).get("gizmo") or {}).get("gizmo") or {}
            gid = giz.get("id")
            if not gid or gid in seen:
                continue
            seen.add(gid)
            fresh += 1
            projects.append({"id": gid, "name": (giz.get("display") or {}).get("name")})
        cursor = (page or {}).get("cursor") or ""
        if not fresh or not cursor:
            break
        pacer.sleep()

    for proj in projects:
        convs, pcursor = [], ""
        for page_no in range(max_pages):
            path = (f"/backend-api/gizmos/{proj['id']}/conversations"
                    f"?cursor={urllib.parse.quote(pcursor)}&limit={page_limit}")
            batch = []
            for attempt in range(empty_retries + 1):
                page = conn.evaluate(_js_get(path, token))
                batch = (page or {}).get("items") or []
                pcursor = (page or {}).get("cursor") or ""
                if batch or page_no > 0 or attempt == empty_retries:
                    break
                print(f"  empty first page for project {proj['name']!r} "
                      f"(attempt {attempt + 1}/{empty_retries + 1}), retrying",
                      file=sys.stderr)
                pacer.sleep()
            convs.extend(batch)
            if not batch or not pcursor:
                break
            pacer.sleep()
        proj["conversations"] = convs
        pacer.sleep()
    return projects


def build_manifest(port, sources, pacer):
    """One request-cheap pass recording everything the servers say exists."""
    manifest = {"built_at": datetime.now(timezone.utc).isoformat(), "entries": [], "projects": []}
    by_key = {}

    def add(entry):
        if not entry["conversation_id"]:
            return
        key = (entry["source"], entry["conversation_id"])
        prev = by_key.get(key)
        if prev is None:
            by_key[key] = entry
            manifest["entries"].append(entry)
            return
        # A conversation can legitimately appear twice (flat list AND its
        # project). Keep the newer updated_at, and never let a flat-list
        # sighting erase the project attribution the project walk found.
        if (entry["updated_at"] or "") > (prev["updated_at"] or ""):
            for field in ("title", "created_at", "updated_at"):
                prev[field] = entry[field]
        if entry.get("project_id") and not prev.get("project_id"):
            prev["project_id"] = entry["project_id"]
        # The id and the name are filled in independently, because the flat
        # list supplies a bare `gizmo_id` with no name while only the project
        # walk knows what that project is called. Gating the name on the id
        # still being unset meant the flat sighting (which always comes first)
        # permanently locked in project_name=None: 215 of 245 attributions lost
        # their name that way, i.e. nearly all of the signal worth having.
        if entry.get("project_name") and not prev.get("project_name"):
            prev["project_name"] = entry["project_name"]

    if "chatgpt" in sources:
        with own_tab(port, "https://chatgpt.com/robots.txt") as conn:
            token = conn.evaluate(gc.js_get_access_token())
            flat = gc.fetch_all_conversation_summaries(conn, token, max_conversations=100000)
            print(f"chatgpt: {len(flat)} conversations in the flat list")
            for conv in flat:
                add(_entry("chatgpt", conv))
            pacer.sleep()
            projects = fetch_chatgpt_projects(conn, token, pacer)
            in_projects = sum(len(p["conversations"]) for p in projects)
            print(f"chatgpt: {len(projects)} projects holding {in_projects} conversations")
            for proj in projects:
                manifest["projects"].append({
                    "source": "chatgpt", "id": proj["id"], "name": proj["name"],
                    "conversation_count": len(proj["conversations"]),
                })
                for conv in proj["conversations"]:
                    add(_entry("chatgpt", conv, project_id=proj["id"], project_name=proj["name"]))

    if "claude" in sources:
        with own_tab(port, "https://claude.ai/robots.txt") as conn:
            org_id = cc.require_org_id()
            convs = cc.fetch_all_conversation_summaries(conn, org_id, max_conversations=100000)
            if not convs:
                # An empty list here has always meant "logged out / throttled",
                # never "no conversations" -- see the incident recorded in
                # poll_state.json, where an empty list silently reset the
                # watermark and had to be restored by hand.
                raise RuntimeError(
                    "claude.ai returned an empty conversation list -- treating as a fetch "
                    "failure (logged out or throttled), not as an empty account"
                )
            print(f"claude: {len(convs)} conversations in the list")
            for conv in convs:
                add(_entry("claude", conv))

    # Last resort for a name: a conversation can carry a project id that the
    # project walk never emitted it under (it only lists a project's *current*
    # conversations), so resolve anything still unnamed against the project
    # roster we just built.
    names = {p["id"]: p["name"] for p in manifest["projects"] if p.get("name")}
    for entry in manifest["entries"]:
        if entry.get("project_id") and not entry.get("project_name"):
            entry["project_name"] = names.get(entry["project_id"])

    manifest["entries"].sort(key=lambda e: (e["updated_at"] or ""), reverse=True)
    return manifest


# ---------------------------------------------------------------- db helpers

def open_db(path):
    conn = sqlite3.connect(path, timeout=60)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError as exc:
        # Switching journal mode needs exclusive access and does not go through
        # busy_timeout; a reader holding the db is not a reason to abort a
        # multi-hour import (same call this makes in poll_conversations.py).
        print(f"could not switch to WAL journal mode ({exc}), continuing without it",
              file=sys.stderr)
    conn.execute("PRAGMA busy_timeout=60000")
    conn.executescript(SCHEMA)
    conn.executescript(PROJECT_SCHEMA)
    return conn


def record_project(conn, source, conversation_id, project_id, project_name):
    if not project_id:
        return
    conn.execute(
        """INSERT INTO conversation_projects (source, conversation_id, project_id, project_name)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(source, conversation_id) DO UPDATE SET
               project_id = excluded.project_id,
               project_name = excluded.project_name,
               recorded_at = datetime('now')""",
        (source, conversation_id, project_id, project_name),
    )


def sync_projects(conn, manifest):
    """Record every project attribution the manifest knows, up front.

    Project membership comes entirely from the listing phase, so there is no
    reason to make it wait on the slow per-conversation fetch loop: a run that
    is interrupted after ten conversations should still leave the full project
    map behind, and a conversation already imported by an earlier tool should
    still get attributed without being re-fetched. Costs no network at all.
    """
    recorded = 0
    for entry in manifest["entries"]:
        if entry.get("project_id"):
            record_project(conn, entry["source"], entry["conversation_id"],
                           entry["project_id"], entry.get("project_name"))
            recorded += 1
    conn.commit()
    return recorded


def stored_updated_at(conn):
    return {
        (src, cid): upd
        for src, cid, upd in conn.execute(
            "SELECT source, conversation_id, updated_at FROM raw_conversations"
        )
    }


def is_agent_scratch(title):
    """True if `title` belongs to one of our own agents' throwaway conversations.

    Two separate producers, both of which create real conversations on the live
    account that would otherwise import like any other: council_bot.py's
    per-question scratch conversations, and claude_browser.py's interest
    extractor. Importing either is a correctness bug, not just noise -- their
    prompts contain conversation bodies, so a scratch conversation carries the
    corpus back into the corpus and hands a later derivation its own earlier
    output as if it were the owner's material. Checked here at the point where
    work is queued, so such a conversation is never even fetched.

    Not merely theoretical: an 'interest-extractor scratch' conversation was
    the first row this backfill imported on 2026-08-18, before this guard
    existed, and had to be deleted.
    """
    return bool(title) and (
        is_council_bot_scratch_conversation(title)
        or claude_browser.is_scratch_conversation(title)
    )


def purge_agent_scratch(conn):
    """Delete any agent scratch conversation already in the corpus.

    The queue-time guard above stops new ones; this removes any that a run
    predating the guard already wrote. Returns what it deleted so the caller
    can report it rather than silently mutating the corpus.
    """
    doomed = [
        (source, cid, title)
        for source, cid, title in conn.execute(
            "SELECT source, conversation_id, title FROM raw_conversations"
        )
        if is_agent_scratch(title)
    ]
    for source, cid, _title in doomed:
        conn.execute(
            "DELETE FROM raw_conversations WHERE source = ? AND conversation_id = ?",
            (source, cid),
        )
        conn.execute(
            "DELETE FROM conversation_projects WHERE source = ? AND conversation_id = ?",
            (source, cid),
        )
    conn.commit()
    return doomed


def same_instant(stored_value, manifest_value):
    """True if two stored timestamps denote the same moment.

    Compared as instants rather than strings because the corpus contains two
    spellings of the same time: older Claude rows were written with a `Z`
    suffix while everything norm_ts() produces uses `+00:00`. 17 of the 21
    Claude rows differ only that way, and a string comparison would re-fetch
    every one of them for nothing.
    """
    if not stored_value or not manifest_value:
        return False
    if stored_value == manifest_value:
        return True
    try:
        return (datetime.fromisoformat(stored_value.replace("Z", "+00:00"))
                == datetime.fromisoformat(manifest_value.replace("Z", "+00:00")))
    except ValueError:
        return False


def pending_entries(manifest, stored, *, sources=None, since=None):
    """Manifest entries that still need a detail fetch, newest-first.

    An entry whose stored updated_at already equals the manifest's is skipped
    outright -- no request at all, not merely no write. That is what makes a
    resume nearly free, and it is the same trust in update_time the poller and
    the --after/--before filters already rely on.

    Newest-first ordering is the priority policy: the last few months matter
    most, so an interrupted run has always already covered them.
    """
    out = []
    for entry in manifest["entries"]:
        if sources and entry["source"] not in sources:
            continue
        if since and (entry["updated_at"] or "") < since:
            continue
        if is_agent_scratch(entry.get("title")):
            continue
        key = (entry["source"], entry["conversation_id"])
        if same_instant(stored.get(key), entry["updated_at"]):
            continue
        out.append(entry)
    out.sort(key=lambda e: (e["updated_at"] or ""), reverse=True)
    return out


# ---------------------------------------------------------------- the slow run

class SourceClient:
    """One authenticated tab per source, able to rebuild itself after a drop."""

    def __init__(self, source, port):
        self.source = source
        self.port = port
        self._ctx = None
        self.conn = None
        self.token = None
        self.org_id = cc.require_org_id() if source == "claude" else None

    @property
    def url(self):
        return ("https://chatgpt.com/robots.txt" if self.source == "chatgpt"
                else "https://claude.ai/robots.txt")

    def connect(self):
        self.close()
        self._ctx = own_tab(self.port, self.url)
        self.conn = self._ctx.__enter__()
        if self.source == "chatgpt":
            self.token = self.conn.evaluate(gc.js_get_access_token())
        return self

    def close(self):
        if self._ctx is not None:
            try:
                self._ctx.__exit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass
        self._ctx, self.conn, self.token = None, None, None

    def fetch(self, conversation_id):
        if self.source == "chatgpt":
            js = gc.js_fetch_conversation(self.token, conversation_id)
        else:
            js = cc.js_fetch_conversation(self.org_id, conversation_id)
        return self.conn.evaluate(js)


def _row_from(source, entry, data):
    if source == "chatgpt":
        return {
            "conversation_id": entry["conversation_id"],
            # The detail fetch returns title=null for a conversation ChatGPT
            # has not finished titling server-side yet, while the list already
            # has one -- prefer the detail, fall back rather than store
            # a needlessly wrong "(untitled)".
            "title": data.get("title") or entry.get("title"),
            "model": find_chatgpt_model(data),
            "created_at": norm_ts(gpt_ts(data.get("create_time"))) or entry.get("created_at"),
            # The manifest's (list endpoint's) update_time, not the detail
            # response's -- see the matching comment in run_chatgpt(). The two
            # differ by 1-2s, and storing the one the next run will compare
            # against is what lets a resume skip an unchanged conversation
            # without a request. raw_json still holds the detail verbatim.
            "updated_at": entry.get("updated_at") or norm_ts(gpt_ts(data.get("update_time"))),
        }
    return {
        "conversation_id": data.get("uuid") or entry["conversation_id"],
        "title": data.get("name") or entry.get("title"),
        "model": data.get("model"),
        "created_at": (norm_ts(claude_ts(data["created_at"]))
                       if data.get("created_at") else entry.get("created_at")),
        "updated_at": (norm_ts(claude_ts(data["updated_at"]))
                       if data.get("updated_at") else entry.get("updated_at")),
    }


def run(conn, manifest, *, port, sources, since, pacer, progress_path,
        max_items=None, max_runtime_minutes=None, max_attempts_per_item=2,
        sleeper=time.sleep, client_factory=SourceClient):
    for source, cid, title in purge_agent_scratch(conn):
        print(f"backfill: removed agent scratch conversation {source}/{cid} {title!r}")
    recorded = sync_projects(conn, manifest)
    print(f"backfill: recorded {recorded} project attributions from the manifest")
    stored = stored_updated_at(conn)
    todo = pending_entries(manifest, stored, sources=sources, since=since)
    if max_items:
        todo = todo[:max_items]
    stats = {"inserted": 0, "updated": 0, "unchanged": 0, "failed": 0,
             "rate_limited": 0, "reconnects": 0, "processed": 0, "stopped_early": False}
    print(f"backfill: {len(todo)} conversations need a fetch "
          f"({', '.join(sorted(sources))}; since={since or 'beginning'})")
    if not todo:
        return stats

    deadline = (time.monotonic() + max_runtime_minutes * 60) if max_runtime_minutes else None
    clients = {}
    progress = open(progress_path, "a", encoding="utf-8")
    try:
        for i, entry in enumerate(todo, 1):
            if deadline is not None and time.monotonic() >= deadline:
                stats["stopped_early"] = True
                print(f"backfill: hit the {max_runtime_minutes}m budget with "
                      f"{len(todo) - i + 1} left -- rerun the same command to resume")
                break
            source = entry["source"]
            client = clients.get(source)
            if client is None:
                client = clients[source] = client_factory(source, port).connect()

            outcome, error = None, None
            for _attempt in range(max_attempts_per_item):
                try:
                    data = fetch_conversation_with_retry(
                        lambda: client.fetch(entry["conversation_id"]),
                        source=source, label=entry["conversation_id"],
                    )
                    row = _row_from(source, entry, data)
                    outcome, _superseded = upsert(conn, source=source, raw_obj=data, **row)
                    record_project(conn, source, entry["conversation_id"],
                                   entry.get("project_id"), entry.get("project_name"))
                    conn.commit()  # checkpoint after EVERY conversation
                    stats[outcome] += 1
                    pacer.relax()
                    break
                except Exception as exc:  # noqa: BLE001
                    error = exc
                    if is_rate_limited(exc):
                        stats["rate_limited"] += 1
                        waited = pacer.rate_limit_backoff(sleeper)
                        print(f"  rate-limited on {entry['conversation_id']}: {exc} -- "
                              f"backed off {waited:.0f}s (penalty x{pacer.penalty:g})",
                              file=sys.stderr)
                    elif is_connection_lost(exc):
                        stats["reconnects"] += 1
                        print(f"  lost the CDP connection ({exc}) -- reopening our own tab",
                              file=sys.stderr)
                        pacer.penalize()
                        sleeper(pacer.delay())
                        try:
                            client = clients[source] = client_factory(source, port).connect()
                        except Exception as reconnect_exc:  # noqa: BLE001
                            error = reconnect_exc
                            break
                    else:
                        # An ordinary empty/invalid payload already survived
                        # fetch_conversation_with_retry's four attempts. Leave
                        # it for the next run rather than hammering it now.
                        break
            if outcome is None:
                stats["failed"] += 1
                print(f"[{i}/{len(todo)}] FAILED {entry['conversation_id']}: {error}",
                      file=sys.stderr)
            else:
                print(f"[{i}/{len(todo)}] {source} {outcome} "
                      f"[{entry.get('project_name') or '-'}] {entry.get('title')!r}")
            stats["processed"] += 1
            progress.write(json.dumps({
                "at": datetime.now(timezone.utc).isoformat(),
                "source": source,
                "conversation_id": entry["conversation_id"],
                "updated_at": entry["updated_at"],
                "project": entry.get("project_name"),
                "outcome": outcome or "failed",
                "error": None if outcome else str(error),
            }, ensure_ascii=False) + "\n")
            progress.flush()
            os.fsync(progress.fileno())
            pacer.sleep(sleeper)
    finally:
        progress.close()
        for client in clients.values():
            client.close()
    print(f"backfill done: {stats}")
    return stats


# ---------------------------------------------------------------- reporting

def status_report(conn, manifest=None):
    lines = []
    rows = conn.execute(
        "SELECT source, COUNT(*), MIN(created_at), MAX(created_at) "
        "FROM raw_conversations GROUP BY source ORDER BY source"
    ).fetchall()
    lines.append("rows per source: " + "; ".join(
        f"{s}={n} ({(lo or '?')[:10]} .. {(hi or '?')[:10]})" for s, n, lo, hi in rows))
    lines.append("")
    lines.append("month      chatgpt  claude")
    for month, gpt, cla in conn.execute(
        "SELECT substr(COALESCE(created_at,'?'),1,7) m, "
        "SUM(source='chatgpt'), SUM(source='claude') "
        "FROM raw_conversations GROUP BY m ORDER BY m"
    ):
        lines.append(f"{month:<10} {gpt:>7}  {cla:>6}")
    projects = conn.execute(
        "SELECT project_name, COUNT(*) FROM conversation_projects "
        "GROUP BY project_name ORDER BY 2 DESC"
    ).fetchall()
    if projects:
        lines.append("")
        lines.append("conversations per project:")
        for name, count in projects:
            lines.append(f"  {name or '(unnamed)'}: {count}")
    if manifest:
        remaining = pending_entries(manifest, stored_updated_at(conn))
        lines.append("")
        lines.append(f"manifest built {manifest['built_at']}: {len(manifest['entries'])} "
                     f"conversations on the servers, {len(remaining)} still to fetch")
    return "\n".join(lines)


# ---------------------------------------------------------------- cli

def build_parser():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=9222)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--manifest", default=None,
                        help="Manifest path (default: <db dir>/backfill_manifest.json)")
    parser.add_argument("--progress", default=None,
                        help="Append-only progress log (default: <db dir>/backfill_progress.jsonl)")
    parser.add_argument("--source", action="append", choices=["chatgpt", "claude"],
                        help="Restrict to one source (repeatable). Default: both.")
    parser.add_argument("--since", default=None,
                        help="Only conversations updated on/after this date (YYYY-MM-DD)")
    parser.add_argument("--base-delay", type=float, default=4.0,
                        help="Mean seconds between conversation fetches (jittered)")
    parser.add_argument("--long-pause-every", type=int, default=25)
    parser.add_argument("--long-pause", type=float, default=60.0)
    parser.add_argument("--max-items", type=int, default=None)
    parser.add_argument("--max-runtime-minutes", type=float, default=None)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("manifest", help="Refresh the list of everything the servers have")
    sub.add_parser("run", help="Fetch and import everything still missing (slow, resumable)")
    sub.add_parser("status", help="Report corpus completeness (no browser needed)")
    return parser


def main():
    args = build_parser().parse_args()

    # The Windows console is not UTF-8 and ~28% of these titles are Hebrew;
    # never let a print() take down a multi-hour import. Line buffering so a
    # redirected log reflects reality in real time (see export_to_sqlite.main).
    sys.stdout.reconfigure(errors="replace", line_buffering=True)
    sys.stderr.reconfigure(errors="replace", line_buffering=True)

    db_path = Path(args.db)
    manifest_path = (Path(args.manifest) if args.manifest
                     else db_path.parent / "backfill_manifest.json")
    progress_path = (Path(args.progress) if args.progress
                     else db_path.parent / "backfill_progress.jsonl")
    sources = set(args.source or ["chatgpt", "claude"])
    pacer = Pacer(base=args.base_delay, long_pause_every=args.long_pause_every,
                  long_pause=args.long_pause)

    if args.command == "manifest":
        manifest = build_manifest(args.port, sources, pacer)
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=1),
                                 encoding="utf-8")
        print(f"wrote {manifest_path} ({len(manifest['entries'])} conversations, "
              f"{len(manifest['projects'])} projects)")
        return

    if args.command == "status":
        conn = open_db(str(db_path))
        try:
            manifest = (json.loads(manifest_path.read_text(encoding="utf-8"))
                        if manifest_path.exists() else None)
            print(status_report(conn, manifest))
        finally:
            conn.close()
        return

    if not manifest_path.exists():
        raise SystemExit(f"no manifest at {manifest_path} -- run "
                         "`corpus_backfill.py manifest` first")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    since = None
    if args.since:
        since = norm_ts(datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc))

    with import_lock(Path(str(db_path) + ".import-lock")):
        conn = open_db(str(db_path))
        try:
            stats = run(conn, manifest, port=args.port, sources=sources, since=since,
                        pacer=pacer, progress_path=str(progress_path),
                        max_items=args.max_items,
                        max_runtime_minutes=args.max_runtime_minutes)
        finally:
            conn.close()
    if stats["stopped_early"]:
        sys.exit(3)  # the same contract resilient_import.py already understands


if __name__ == "__main__":
    main()
