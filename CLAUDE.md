# CLAUDE.md — `ai`

Conversation export + council bot.

@PROJECT_STATE.md

## Work from maintained context

CLAUDE.md + PROJECT_STATE.md are the authoritative starting context and are already loaded.

- Do not rediscover, map, summarize, or broadly scan the repo.
- Trust PROJECT_STATE.md by default; verify only code relevant to the current task.
- Read the smallest possible set of files/regions needed to implement the task.
- Prefer targeted Grep / Read; no tree, recursive globs, explorer agents, or orientation sweeps.
- Do not open files just to learn project structure already documented in maintained context.
- Widen exploration only when required information is genuinely missing or touched code contradicts maintained state.
- Implement directly, run only relevant tests, and stop. No summary unless asked.
- Update PROJECT_STATE.md after meaningful implementation or architecture changes; keep it concise (<500 words).

## Core constraints

- Python 3.14 on Windows / PowerShell.
- Stdlib except `anthropic`, used by `council_bot.py`.
- SQLite (`conversations.db`, `raw_conversations`) is the single store.
- Browser automation uses our own `cdp.py`; no Selenium, Playwright, or HTTP library for browser-backed APIs.
- Secrets live in `.env.local`, loaded by `common.load_env_local()`; never hardcode or default them.

## Architecture

- Browser auth: claude.ai and chatgpt.com requests must run via `fetch()` inside an already authenticated Chrome tab through `cdp.py` / `Runtime.evaluate`. Do not replace this with direct HTTP.
- DB writes: all writes to `raw_conversations` go through `export_to_sqlite.py:upsert()`. Importers reuse it; do not create parallel write paths.
- Empty responses: treat unexpected empty API responses/pages as failures and retry with backoff before interpreting them as end-of-data.
- Put non-obvious implementation rationale next to the relevant code, not in CLAUDE.md or PROJECT_STATE.md.

## Testing

Default offline suite:

```bash
python -m unittest discover -p "test_*.py"
```

While iterating, run only the relevant test module.

Tests stay offline: stub CDP/Chrome, Telegram, Anthropic, and network. Live verification never runs in CI.

For `council_bot.py` changes, `python e2e_verify_bot.py` is the mock E2E check.

## Keep it simple

- No ORM, migration framework, new dependencies, frameworks, async rewrite, or packaging unless clearly required.
- Reuse `common.py`, `cdp.py`, and `export_to_sqlite.py` helpers rather than creating parallel mechanisms.
- No abstractions for single call sites.
- Scripts remain flat files with `__main__` + argparse.
- Keep logging simple.

README is user-facing documentation. PROJECT_STATE.md stores only concise implementation state needed by future sessions.
