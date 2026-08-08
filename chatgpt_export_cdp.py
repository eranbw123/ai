#!/usr/bin/env python3
"""Trigger a ChatGPT export flow via a real, already-logged-in Chrome tab.

Mirrors claude_export_cdp.py's approach, retargeted at chatgpt.com's actual
API surface as used by pionxzh/chatgpt-exporter (github.com/pionxzh/chatgpt-exporter):

  GET  {origin}/api/auth/session            -> { accessToken, ... }
  GET  {origin}/backend-api/conversations?offset=&limit=  -> { items, total }
  GET  {origin}/backend-api/conversation/{id}             -> { mapping, current_node, ... }

Auth is a bearer token (Authorization + X-Authorization headers), obtained
from the session endpoint -- not a cookie header -- but it still only works
from inside a real browser tab: chatgpt.com's edge protection rejects bare
HTTP clients the same way claude.ai's does, so this runs the fetch() calls
inside the actual page via Chrome DevTools Protocol Runtime.evaluate, exactly
like the Claude flow.

Setup:
    1. Launch Chrome with remote debugging enabled (a throwaway profile is
       fine, see claude_export_cdp.py's docstring for the exact command).
    2. In that window, log into https://chatgpt.com normally (once).
    3. python chatgpt_export_cdp.py list --after 2026-05-05 --before 2026-05-10
       python chatgpt_export_cdp.py export-all --format markdown --after 2026-05-05 --before 2026-05-10
"""
import argparse
import re
import sys
import time

import cdp
from chatgpt_export import filter_conversations, parse_api_timestamp, write_export
from common import parse_date

UUID_RE = re.compile(r"^[0-9a-f-]{20,40}$", re.I)


def js_get_access_token():
    return """
(async () => {
  const res = await fetch('/api/auth/session', { headers: { Accept: 'application/json' } });
  if (!res.ok) throw new Error('HTTP ' + res.status + ' fetching session');
  const data = await res.json();
  if (!data.accessToken) throw new Error('No accessToken in session response (are you logged in?)');
  return data.accessToken;
})()
"""


def js_fetch_conversations_page(token, offset, limit):
    return f"""
(async () => {{
  const res = await fetch('/backend-api/conversations?offset={offset}&limit={limit}', {{
    headers: {{
      Accept: 'application/json',
      Authorization: 'Bearer {token}',
      'X-Authorization': 'Bearer {token}'
    }}
  }});
  if (!res.ok) throw new Error('HTTP ' + res.status);
  return await res.json();
}})()
"""


def js_fetch_conversation(token, conversation_id):
    return f"""
(async () => {{
  const res = await fetch('/backend-api/conversation/{conversation_id}', {{
    headers: {{
      Accept: 'application/json',
      Authorization: 'Bearer {token}',
      'X-Authorization': 'Bearer {token}'
    }}
  }});
  if (!res.ok) throw new Error('HTTP ' + res.status);
  return await res.json();
}})()
"""


def connect(port):
    tab = cdp.find_tab("https://chatgpt.com", port) or cdp.find_tab("https://chat.openai.com", port)
    if not tab:
        sys.exit(
            f"No open chatgpt.com tab found on the Chrome instance at port {port}.\n"
            "Open https://chatgpt.com and log in in that Chrome window, then retry."
        )
    print(f"Using tab: {tab['url']}")
    return cdp.CDPConnection(tab["webSocketDebuggerUrl"])


def fetch_all_conversation_summaries(conn, token, after=None, limit=100, max_conversations=2000):
    """Mirrors fetchAllConversations() in the reference project: page via
    offset/limit until a short page signals the end.

    Deliberately does NOT trust `offset + limit >= total` as the stop
    condition (that's what the reference project does) -- observed in
    testing that some accounts return a `total` that keeps climbing by
    exactly one page's worth on every request, which would make that
    condition never fire and walk pages all the way to max_conversations.
    A page shorter than the requested limit is a reliable end-of-data
    signal regardless of what `total` reports.

    If `after` is given, stops paging early once it hits an item whose
    update_time is older than `after`. This is sound, not a heuristic:
    empirically (checked live) the list is sorted by update_time descending,
    and update_time >= create_time always holds for any item -- so once
    update_time drops below `after`, every remaining (older) item is too old
    on *both* fields, regardless of which one you're actually filtering by.

    Every empty page gets retried (same 10/20/30s backoff shape as
    fetch_conversation_with_retry in export_to_sqlite.py) before being
    accepted as real end-of-data, not just the offset-0 page -- and offset 0
    specifically raises instead of returning [] if it's still empty after
    retrying. Real incident, 2026-08-06: with chatgpt.com already heavily
    rate-limited from repeated same-day full-history passes (compounded, it
    turned out, by poll_conversations.py's own background polling adding to
    the same request volume the whole time), the very first page came back
    `{"items": []}` -- indistinguishable, before this fix, from a real
    zero-conversation account. run_chatgpt() (export_to_sqlite.py) took the
    resulting empty `conversations` list at face value, logged "0 of 0
    conversations to import", and exited with status 0 ("nothing to do"),
    which fooled resilient_import.py's supervisor loop into declaring the
    whole backlog complete and stopping, having imported nothing. Retrying
    fixed offset 0 -- but the very next live run then truncated to exactly
    one page (100 of ~1630 real conversations) because *page 2* came back
    empty and, at the time, an empty page at offset > 0 was accepted
    immediately as the legitimate end-of-data signal with no retry at all --
    which is right when chatgpt.com is healthy (a page short of `limit`
    items is still the reliable, unretried signal for that: see the
    `len(items) < limit` check below), but wrong under the same throttling
    that hit offset 0. Retrying before accepting *any* empty page costs
    nothing extra in the healthy case (a real final page is almost always
    short-but-non-empty, so pagination stops via the `len(items) < limit`
    check one page earlier and never even requests the trailing empty one);
    the only added cost is up to one ~60s retry cycle in the rarer case
    where the true total happens to be an exact multiple of `limit`.
    """
    conversations = []
    offset = 0
    max_attempts = 4
    while True:
        items = []
        for attempt in range(1, max_attempts + 1):
            page = conn.evaluate(js_fetch_conversations_page(token, offset, limit))
            items = page.get("items") or []
            if items:
                break
            if attempt < max_attempts:
                wait = 10 * attempt
                print(f"  empty page at offset {offset} of chatgpt conversation list "
                      f"(attempt {attempt}/{max_attempts}), retrying after {wait}s -- "
                      f"likely rate-limited, not necessarily end of data", file=sys.stderr)
                time.sleep(wait)
        if not items:
            if offset == 0:
                raise RuntimeError(
                    f"chatgpt conversation list came back empty after {max_attempts} attempts at "
                    "offset 0 -- treating as a fetch failure (rate limit / stale session), not "
                    "truly zero conversations"
                )
            break  # retried and still empty at offset > 0 -- accept as genuine end-of-data
        conversations.extend(items)
        if after is not None:
            oldest_update = parse_api_timestamp(items[-1].get("update_time"))
            if oldest_update is not None and oldest_update < after:
                break
        if len(items) < limit:
            break
        if len(conversations) >= max_conversations:
            break
        offset += limit
    return conversations[:max_conversations]


def cmd_list(args):
    conn = connect(args.port)
    try:
        token = conn.evaluate(js_get_access_token())
        conversations = fetch_all_conversation_summaries(conn, token, after=args.after)
    finally:
        conn.close()
    filtered = filter_conversations(conversations, args.after, args.before, args.date_field)
    field = "create_time" if args.date_field == "created" else "update_time"
    for c in filtered:
        print(f"{c['id']}  {c.get('title') or '(untitled)'}  {args.date_field}={c.get(field)}")
    print(f"\n{len(filtered)} of {len(conversations)} conversations match filters")


def cmd_export_all(args):
    conn = connect(args.port)
    try:
        token = conn.evaluate(js_get_access_token())
        conversations = fetch_all_conversation_summaries(conn, token, after=args.after)
        filtered = filter_conversations(conversations, args.after, args.before, args.date_field)
        print(f"{len(filtered)} of {len(conversations)} conversations match filters, exporting as {args.format}...")

        count = 0
        for conv in filtered:
            if not UUID_RE.match(conv["id"]):
                continue
            try:
                data = conn.evaluate(js_fetch_conversation(token, conv["id"]))
                path = write_export(data, args.format, not args.no_metadata, args.out)
                count += 1
                print(f"[{count}/{len(filtered)}] {path}")
                time.sleep(0.5)
            except Exception as e:  # noqa: BLE001
                print(f"Failed {conv['id']}: {e}", file=sys.stderr)
        print(f"Done. Exported {count}/{len(filtered)}.")
    finally:
        conn.close()


def build_parser():
    parser = argparse.ArgumentParser(description="Trigger a ChatGPT export flow via a real Chrome tab (CDP)")
    parser.add_argument("--port", type=int, default=9222, help="Chrome --remote-debugging-port")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_date_args(p):
        p.add_argument("--after", type=parse_date)
        p.add_argument("--before", type=parse_date)
        p.add_argument("--date-field", choices=["created", "updated"], default="created")

    p_list = sub.add_parser("list")
    add_date_args(p_list)
    p_list.set_defaults(func=cmd_list)

    p_all = sub.add_parser("export-all")
    p_all.add_argument("--format", choices=["json", "markdown", "text"], default="markdown")
    p_all.add_argument("--out", default="./exports_chatgpt")
    p_all.add_argument("--no-metadata", action="store_true")
    add_date_args(p_all)
    p_all.set_defaults(func=cmd_export_all)

    return parser


def main():
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
