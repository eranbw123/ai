# PROJECT_STATE.md — `ai`

Updated 2026-08-07. Imported by `CLAUDE.md`; maintained under its startup and
token-efficiency rules. Current state only — not a log, not an architecture doc.

## Implemented
Claude + ChatGPT export via CDP → SQLite; continuous poller; Telegram council bot; read-only web viewer (ngrok-exposed); one-off markdown→SQLite migration (done, no longer needed); resilient chunked-import supervisor; privacy-safe `personal_state.py` (interest = normalized frequency, v1 contract); `knowledge_state.py` (familiarity = exposure × temporal spread × recency decay, structurally distinct from interest) plus `eval_knowledge_state.py`, a frozen replay-eval harness comparing the two.

## Non-obvious decisions
- No intermediate markdown/json export files any more — CDP goes straight to `raw_conversations`. Old markdown-derived rows are marked `markdown_reconstructed` and superseded when the real API row arrives.
- `--after/--before` filter on `updated_at`, not `created_at`, so edited old conversations get re-imported.
- `council_bot`'s own scratch conversations are excluded at import time.
- Long-lived background runs on this machine get reaped silently (no traceback) — hence `resilient_import.py` + `--max-runtime-minutes`. Don't replace it with one long run.
- `knowledge_state.py`'s familiarity formula (saturation/spread-months/half-life constants) is pre-registered and frozen in `KNOWLEDGE_STATE_EXPERIMENT.md`'s append-only "Pre-registration" section, written before any evaluation number was observed. Do not tune it based on `eval_knowledge_state.py` output.
- `personal_state.py --with-knowledge-state` (opt-in, default off) merges `knowledge_state.py`'s additive fields into personal_state's topic records by token key; default output (`contract_version` 1) is unchanged.

## Key files
`cdp.py` CDP client · `export_to_sqlite.py` fetch + `upsert()` (only DB writer) · `poll_conversations.py` poller · `resilient_import.py` supervisor · `council_bot.py` + `e2e_verify_bot.py` · `view_conversations_server.py` · `common.py` env/date/filename helpers · `personal_state.py` derives the versioned, privacy-safe interest-state artifact for other repos · `knowledge_state.py` derives the (not yet published/contracted) familiarity artifact, reusing `personal_state`'s tokenizer and writer · `eval_knowledge_state.py` the frozen replay-eval harness for the latter (run once — see the doc below).

Personal-state contract (schema + version-bump procedure): `PERSONAL_STATE_CONTRACT.md`, currently v1. Knowledge-state pre-registration + (once run) results: `KNOWLEDGE_STATE_EXPERIMENT.md`.

## Adding a CLI flag (recurring task — no file reads needed)
Each script builds its own `argparse.ArgumentParser` inside `main()`. Flags that
`poll_conversations.py` must forward are read off `args` with
`getattr(args, "name", default)` in `export_to_sqlite.py`, because the poller
passes a `types.SimpleNamespace` rather than a parsed argparse result.

## Known issues
- ChatGPT backfill incomplete: ~242 of ~1630 conversations in DB (Claude: 21).
- Under chatgpt.com throttling, CDP runs die with `WinError 10053` / "WebSocket connection closed". Stop `poll_conversations.py` before a big backfill — its polling compounds the rate limit.

## Next task
Run `eval_knowledge_state.py` once against a real corpus (if one is present in this clone or via `AI_CONVERSATIONS_DB`) and append an honest results section to `KNOWLEDGE_STATE_EXPERIMENT.md` after its frozen pre-registration section — do not edit that section, do not re-run with different settings. Then finish the ChatGPT backfill: poller stopped, Chrome on port 9222, `python resilient_import.py chatgpt`.

## Commands to continue
```bash
chrome --remote-debugging-port=9222   # required by every CDP script
python resilient_import.py chatgpt
python council_bot.py                 # Telegram bridge; confirm council_bot.log is idle first
python view_conversations_server.py   # read-only local viewer
python -m unittest discover -p "test_*.py"
python -c "import sqlite3;print(sqlite3.connect('file:conversations.db?mode=ro',uri=True).execute('select source,count(*) from raw_conversations group by source').fetchall())"
```
