#!/usr/bin/env python3
"""Trigger the extension's export flow via a real, already-logged-in Chrome tab.

Instead of a bare HTTP client (which claude.ai's edge rejects with 403 for
non-browser traffic) or reading Chrome's encrypted cookie store off disk,
this connects to an actual running Chrome via the DevTools Protocol and runs
the exact same fetch() calls content.js uses, inside that real page's JS
context. The browser handles auth/cookies/anti-bot exactly as it would for
any normal page script -- this script just tells it what to run and reads
back the result.

Setup:
    1. Launch Chrome with remote debugging enabled, e.g. a throwaway profile
       so it doesn't disturb your normal browsing:
         chrome.exe --remote-debugging-port=9222 --user-data-dir="%TEMP%\\chrome-cdp-profile"
    2. In that window, log into claude.ai normally (once).
    3. Make sure CLAUDE_ORG_ID is set in .env.local (CLAUDE_COOKIE is not
       needed for this script -- the real browser supplies auth itself).
    4. python claude_export_cdp.py list --after 2026-05-05 --before 2026-05-10
       python claude_export_cdp.py export-all --format markdown --after 2026-05-05 --before 2026-05-10

Uses chat_conversations_v2 (offset/limit/has_more), not the unpaginated
chat_conversations the browser extension itself uses -- the plain endpoint
happens to return everything in one shot for small accounts, but that's a
ticking bottleneck for larger ones (verified live: chatgpt.com's equivalent
list, on an account with 1621 conversations, took several minutes to walk
page by page before an early-stop fix cut it to ~9s). Fixing this before it
becomes a problem here too: v2's pagination was verified live (small --limit
forced multiple real pages against this account's 25 conversations, all
25 came back unique, no gaps/dupes) and, like ChatGPT's list, is sorted by
updated_at descending -- so the same sound early-stop applies: since
created_at <= updated_at always, once a page's oldest item has
updated_at < --after, every remaining item is too old on both fields.
"""
import argparse
import os
import re
import sys
import time

import cdp
from claude_export import filter_conversations, parse_api_timestamp, write_export
from common import load_env_local, parse_date

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def js_fetch_conversations_page(org_id, offset, limit):
    return f"""
(async () => {{
  const res = await fetch('https://claude.ai/api/organizations/{org_id}/chat_conversations_v2?limit={limit}&offset={offset}&consistency=eventual', {{
    credentials: 'include',
    headers: {{ Accept: 'application/json' }}
  }});
  if (!res.ok) throw new Error('HTTP ' + res.status);
  return await res.json();
}})()
"""


def js_fetch_conversation(org_id, conversation_id):
    return f"""
(async () => {{
  const res = await fetch('https://claude.ai/api/organizations/{org_id}/chat_conversations/{conversation_id}?tree=True&rendering_mode=messages&render_all_tools=true', {{
    credentials: 'include',
    headers: {{ Accept: 'application/json' }}
  }});
  if (!res.ok) throw new Error('HTTP ' + res.status);
  return await res.json();
}})()
"""


def connect(port):
    tab = cdp.find_claude_tab(port)
    if not tab:
        sys.exit(
            f"No open claude.ai tab found on the Chrome instance at port {port}.\n"
            "Open claude.ai and log in in that Chrome window, then retry."
        )
    print(f"Using tab: {tab['url']}")
    return cdp.CDPConnection(tab["webSocketDebuggerUrl"])


def require_org_id():
    load_env_local()
    org_id = os.environ.get("CLAUDE_ORG_ID")
    if not org_id or not UUID_RE.match(org_id):
        sys.exit("Missing or invalid CLAUDE_ORG_ID in .env.local")
    return org_id


def fetch_all_conversation_summaries(conn, org_id, after=None, limit=100, max_conversations=5000):
    """Pages chat_conversations_v2 via offset/limit until has_more is false.

    If `after` is given, stops early once a page's oldest item has
    updated_at < after -- sound (not a heuristic) because the list is sorted
    by updated_at descending and created_at <= updated_at always holds, so
    every remaining item is then too old on both fields regardless of which
    one you're actually filtering by. See module docstring for how this was
    verified live before relying on it.
    """
    conversations = []
    offset = 0
    while True:
        page = conn.evaluate(js_fetch_conversations_page(org_id, offset, limit))
        items = page.get("data") or []
        if not items:
            break
        conversations.extend(items)
        if after is not None:
            oldest_updated = parse_api_timestamp(items[-1].get("updated_at"))
            if oldest_updated is not None and oldest_updated < after:
                break
        if not page.get("has_more"):
            break
        if len(conversations) >= max_conversations:
            break
        offset += limit
    return conversations[:max_conversations]


def cmd_list(args):
    org_id = require_org_id()
    conn = connect(args.port)
    try:
        conversations = fetch_all_conversation_summaries(conn, org_id, after=args.after)
    finally:
        conn.close()
    filtered = filter_conversations(conversations, args.after, args.before, args.date_field, args.model)
    for c in filtered:
        print(f"{c['uuid']}  {c.get('name') or '(untitled)'}  {args.date_field}={c.get(f'{args.date_field}_at')}")
    print(f"\n{len(filtered)} of {len(conversations)} conversations match filters")


def cmd_export_all(args):
    org_id = require_org_id()
    conn = connect(args.port)
    try:
        conversations = fetch_all_conversation_summaries(conn, org_id, after=args.after)
        filtered = filter_conversations(conversations, args.after, args.before, args.date_field, args.model)
        print(f"{len(filtered)} of {len(conversations)} conversations match filters, exporting as {args.format}...")

        count = 0
        for conv in filtered:
            if not UUID_RE.match(conv["uuid"]):
                continue
            try:
                data = conn.evaluate(js_fetch_conversation(org_id, conv["uuid"]))
                path = write_export(data, args.format, not args.no_metadata, args.out)
                count += 1
                print(f"[{count}/{len(filtered)}] {path}")
                time.sleep(0.5)  # match extension's own rate limiting
            except Exception as e:  # noqa: BLE001
                print(f"Failed {conv['uuid']}: {e}", file=sys.stderr)
        print(f"Done. Exported {count}/{len(filtered)}.")
    finally:
        conn.close()


def build_parser():
    parser = argparse.ArgumentParser(description="Trigger the Claude exporter's flow via a real Chrome tab (CDP)")
    parser.add_argument("--port", type=int, default=9222, help="Chrome --remote-debugging-port")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_date_args(p):
        p.add_argument("--after", type=parse_date)
        p.add_argument("--before", type=parse_date)
        p.add_argument("--date-field", choices=["created", "updated"], default="created")
        p.add_argument("--model")

    p_list = sub.add_parser("list")
    add_date_args(p_list)
    p_list.set_defaults(func=cmd_list)

    p_all = sub.add_parser("export-all")
    p_all.add_argument("--format", choices=["json", "markdown", "text"], default="markdown")
    p_all.add_argument("--out", default="./exports")
    p_all.add_argument("--no-metadata", action="store_true")
    add_date_args(p_all)
    p_all.set_defaults(func=cmd_export_all)

    return parser


def main():
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
