# PROJECT_STATE.md — `ai`

Updated 2026-08-07. Imported by `CLAUDE.md`; maintained under its startup and
token-efficiency rules. Current state only — not a log, not an architecture doc.

## Implemented
Claude + ChatGPT export via CDP → SQLite; continuous poller; Telegram council bot; read-only web viewer (ngrok-exposed); one-off markdown→SQLite migration (done, no longer needed); resilient chunked-import supervisor.

## Non-obvious decisions
- No intermediate markdown/json export files any more — CDP goes straight to `raw_conversations`. Old markdown-derived rows are marked `markdown_reconstructed` and superseded when the real API row arrives.
- `--after/--before` filter on `updated_at`, not `created_at`, so edited old conversations get re-imported.
- `council_bot`'s own scratch conversations are excluded at import time.
- Long-lived background runs on this machine get reaped silently (no traceback) — hence `resilient_import.py` + `--max-runtime-minutes`. Don't replace it with one long run.

## Key files
`cdp.py` CDP client · `export_to_sqlite.py` fetch + `upsert()` (only DB writer) · `poll_conversations.py` poller · `resilient_import.py` supervisor · `council_bot.py` + `e2e_verify_bot.py` · `view_conversations_server.py` · `common.py` env/date/filename helpers.

## Adding a CLI flag (recurring task — no file reads needed)
Each script builds its own `argparse.ArgumentParser` inside `main()`. Flags that
`poll_conversations.py` must forward are read off `args` with
`getattr(args, "name", default)` in `export_to_sqlite.py`, because the poller
passes a `types.SimpleNamespace` rather than a parsed argparse result.

## Known issues
- ChatGPT backfill incomplete: ~242 of ~1630 conversations in DB (Claude: 21).
- Under chatgpt.com throttling, CDP runs die with `WinError 10053` / "WebSocket connection closed". Stop `poll_conversations.py` before a big backfill — its polling compounds the rate limit.

## Uncommitted (branch `main`, dirty)
Modified: `chatgpt_export_cdp.py` (retry every empty page; raise at offset 0), `export_to_sqlite.py` (`--max-runtime-minutes`, resume), `test_chatgpt_export_cdp.py`.
Untracked: `resilient_import.py`, `view_conversations_server.py`, `migrate_md_to_sqlite.py`, `_check_live.py`, `_check_recent.py`, plus many `*.log` files (should be gitignored, aren't).

## Next task
1. Gitignore the stray `*.log` / `*.pre-move` files, branch off `main`, commit the pending work, open a PR.
2. Then finish the ChatGPT backfill: poller stopped, Chrome on port 9222, `python resilient_import.py chatgpt`.

## Commands to continue
```bash
chrome --remote-debugging-port=9222   # required by every CDP script
python resilient_import.py chatgpt
python council_bot.py                 # Telegram bridge; confirm council_bot.log is idle first
python view_conversations_server.py   # read-only local viewer
python -m unittest discover -p "test_*.py"
python -c "import sqlite3;print(sqlite3.connect('file:conversations.db?mode=ro',uri=True).execute('select source,count(*) from raw_conversations group by source').fetchall())"
```
