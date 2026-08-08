#!/usr/bin/env python3
"""Minimal read-only web viewer for conversations.db's raw_conversations table.

Intended to run locally and be exposed via an ngrok tunnel so it's reachable
from a phone browser away from home. Two safety measures given that:

  1. The DB connection is opened in SQLite's own read-only URI mode
     (mode=ro), so even a bug here can't write to conversations.db.
  2. Every request requires HTTP Basic Auth (credentials printed on start).

No third-party JS, no CDN assets, no arbitrary SQL execution exposed --
fixed views only: a conversation list, a per-conversation transcript, a
table list, and a raw per-table row browser (schema + every column value,
straight from sqlite_master/PRAGMA/SELECT *, no query box).
"""
import argparse
import base64
import html
import json
import secrets
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, unquote

REPO_ROOT = Path(__file__).resolve().parent
DB_PATH = REPO_ROOT / "conversations.db"

USERNAME = "eran"
_CREDS_FILE = REPO_ROOT / ".viewer_auth"
if _CREDS_FILE.exists():
    PASSWORD = _CREDS_FILE.read_text(encoding="utf-8").strip()
else:
    PASSWORD = secrets.token_urlsafe(12)
    _CREDS_FILE.write_text(PASSWORD, encoding="utf-8")


def db():
    return sqlite3.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True)


PAGE_CSS = """
body { font-family: -apple-system, system-ui, sans-serif; max-width: 700px;
       margin: 0 auto; padding: 16px; background: #111; color: #eee; }
a { color: #6cf; text-decoration: none; }
.conv { display: block; padding: 10px 0; border-bottom: 1px solid #333; }
.meta { color: #999; font-size: 0.85em; }
.msg { margin: 14px 0; padding: 10px 12px; border-radius: 10px; }
.msg.user { background: #1d3a5f; }
.msg.assistant { background: #262626; }
.role { font-size: 0.75em; color: #999; margin-bottom: 4px; text-transform: uppercase; }
.text { white-space: pre-wrap; word-wrap: break-word; }
h1 { font-size: 1.2em; }
nav { margin-bottom: 12px; font-size: 0.9em; }
table { border-collapse: collapse; width: 100%; margin-top: 10px; }
th, td { border: 1px solid #333; padding: 6px 8px; text-align: left; vertical-align: top; font-size: 0.85em; }
th { background: #1a1a1a; position: sticky; top: 0; }
td.cell { max-width: 320px; overflow-x: auto; white-space: pre-wrap; word-break: break-word; }
.wrap { overflow-x: auto; }
"""

NAV = '<nav><a href="/">conversations</a> &middot; <a href="/tables">raw tables</a></nav>'


def render_list():
    conn = db()
    rows = conn.execute(
        "SELECT source, conversation_id, title, created_at "
        "FROM raw_conversations ORDER BY created_at DESC"
    ).fetchall()
    conn.close()
    items = "".join(
        f'<a class="conv" href="/c/{html.escape(source)}/{html.escape(conversation_id, quote=True)}">'
        f'<b>[{html.escape(source)}]</b> {html.escape(title or "(untitled)")}'
        f'<div class="meta">{html.escape(created_at or "")}</div></a>'
        for source, conversation_id, title, created_at in rows
    )
    return f"<html><head><title>Conversations</title><style>{PAGE_CSS}</style></head>" \
           f"<body>{NAV}<h1>Conversations ({len(rows)})</h1>{items}</body></html>"


def render_tables_list():
    conn = db()
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    counts = {}
    for (name,) in tables:
        counts[name] = conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
    conn.close()
    items = "".join(
        f'<a class="conv" href="/table/{html.escape(name, quote=True)}">'
        f'<b>{html.escape(name)}</b><div class="meta">{counts[name]} rows</div></a>'
        for (name,) in tables
    )
    return f"<html><head><title>Tables</title><style>{PAGE_CSS}</style></head>" \
           f"<body>{NAV}<h1>Tables ({len(tables)})</h1>{items}</body></html>"


def render_table(name):
    conn = db()
    valid = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    if not valid:
        conn.close()
        return None
    columns = [row[1] for row in conn.execute(f'PRAGMA table_info("{name}")').fetchall()]
    rows = conn.execute(f'SELECT * FROM "{name}"').fetchall()
    conn.close()

    header = "".join(f"<th>{html.escape(c)}</th>" for c in columns)
    body_rows = "".join(
        "<tr>" + "".join(f'<td class="cell">{html.escape("" if v is None else str(v))}</td>' for v in row) + "</tr>"
        for row in rows
    )
    back = '<a href="/tables">&larr; all tables</a>'
    return f"<html><head><title>{html.escape(name)}</title><style>{PAGE_CSS}</style></head>" \
           f"<body>{NAV}{back}<h1>{html.escape(name)} ({len(rows)} rows)</h1>" \
           f'<div class="wrap"><table><thead><tr>{header}</tr></thead><tbody>{body_rows}</tbody></table></div>' \
           f"</body></html>"


def render_conversation(source, conversation_id):
    conn = db()
    row = conn.execute(
        "SELECT title, raw_json FROM raw_conversations WHERE source = ? AND conversation_id = ?",
        (source, conversation_id),
    ).fetchone()
    conn.close()
    if not row:
        return None
    title, raw_json = row
    data = json.loads(raw_json)
    msgs = "".join(
        f'<div class="msg {html.escape(m.get("role", ""))}">'
        f'<div class="role">{html.escape(m.get("role", ""))}</div>'
        f'<div class="text">{html.escape(m.get("text", ""))}</div></div>'
        for m in data.get("messages", [])
    )
    back = '<a href="/">&larr; back</a>'
    return f"<html><head><title>{html.escape(title or 'Conversation')}</title>" \
           f"<style>{PAGE_CSS}</style></head><body>{NAV}{back}<h1>{html.escape(title or '')}</h1>{msgs}</body></html>"


class Handler(BaseHTTPRequestHandler):
    def _unauthorized(self):
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="conversations"')
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Auth required")

    def _check_auth(self):
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(auth[6:]).decode("utf-8")
        except Exception:
            return False
        user, _, pwd = decoded.partition(":")
        return secrets.compare_digest(user, USERNAME) and secrets.compare_digest(pwd, PASSWORD)

    def _send_html(self, body, status=200):
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self):
        if not self._check_auth():
            self._unauthorized()
            return
        parsed = urlparse(self.path)
        parts = [unquote(p) for p in parsed.path.strip("/").split("/")]
        if parsed.path == "/":
            self._send_html(render_list())
            return
        if len(parts) == 3 and parts[0] == "c":
            _, source, conversation_id = parts
            body = render_conversation(source, conversation_id)
            if body is None:
                self._send_html("<h1>404 Not Found</h1>", status=404)
            else:
                self._send_html(body)
            return
        if parsed.path == "/tables":
            self._send_html(render_tables_list())
            return
        if len(parts) == 2 and parts[0] == "table":
            body = render_table(parts[1])
            if body is None:
                self._send_html("<h1>404 Not Found</h1>", status=404)
            else:
                self._send_html(body)
            return
        self._send_html("<h1>404 Not Found</h1>", status=404)

    def log_message(self, fmt, *args):
        pass  # keep stdout clean; nothing sensitive logged either way


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    if not DB_PATH.exists():
        raise SystemExit(f"conversations.db not found at {DB_PATH}")

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Serving on http://127.0.0.1:{args.port}")
    print(f"Username: {USERNAME}")
    print(f"Password: {PASSWORD}")
    server.serve_forever()


if __name__ == "__main__":
    main()
