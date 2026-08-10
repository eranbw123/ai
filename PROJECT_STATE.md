# PROJECT_STATE.md — `ai`

Updated 2026-08-11. Imported by `CLAUDE.md`; maintained under its startup and
token-efficiency rules. Current state only — not a log, not an architecture doc.

## Implemented
Claude + ChatGPT export via CDP → SQLite; continuous poller; Telegram council bot; read-only web viewer (ngrok-exposed); one-off markdown→SQLite migration (done, no longer needed); resilient chunked-import supervisor; privacy-safe `personal_state.py` (interest = normalized frequency, v1 contract); `knowledge_state.py` (familiarity = exposure × temporal spread × recency decay, structurally distinct from interest) plus `eval_knowledge_state.py`, a frozen TOKEN-level replay-eval harness (does familiarity rank future token recurrence better than interest weight?). `eval_future_self.py` is a second, separate frozen ARTIFACT-level replay-eval harness: does the frozen personal-state top-10 topic set at a historical T predict post-T conversations' titles (hit@10 vs. permutation-chance and recency baselines)? See `FUTURE_SELF_EXPERIMENT.md`.

`weak_labels.py` — read-only, stdlib-only extractor of 5 weak behavioral labels (depth, sustained_followup, rapid_abandonment, response_rejection, recurrence) per conversation, with provenance/confidence/degraded_reasons on every record. `weak_labels.json` is **local-only, gitignored, never-gold supervision data** — not part of the personal-state contract, not consumed by any other module (guard-tested). See `WEAK_LABELS.md` for label definitions, thresholds, and the recorded corpus distribution.
## Non-obvious decisions
- No intermediate markdown/json export files any more — CDP goes straight to `raw_conversations`. Old markdown-derived rows are marked `markdown_reconstructed` and superseded when the real API row arrives.
- `--after/--before` filter on `updated_at`, not `created_at`, so edited old conversations get re-imported.
- `council_bot`'s own scratch conversations are excluded at import time.
- Long-lived background runs on this machine get reaped silently (no traceback) — hence `resilient_import.py` + `--max-runtime-minutes`. Don't replace it with one long run.
- `knowledge_state.py`'s familiarity formula (saturation/spread-months/half-life constants) is pre-registered and frozen in `KNOWLEDGE_STATE_EXPERIMENT.md`'s append-only "Pre-registration" section, written before any evaluation number was observed. Do not tune it based on `eval_knowledge_state.py` output.
- `personal_state.py --with-knowledge-state` (opt-in, default off) merges `knowledge_state.py`'s additive fields into personal_state's topic records by token key; default output (`contract_version` 1) is unchanged.
- Both `eval_knowledge_state.py` and `eval_future_self.py` were run 2026-08-10: no `conversations.db` in this automation clone, `AI_CONVERSATIONS_DB` unset — both recorded INCONCLUSIVE — NO CORPUS AVAILABLE (harness self-tests green) in their respective docs. Per `FUTURE_SELF_EXPERIMENT.md`'s pre-registered implication mapping, the INCONCLUSIVE-NO-CORPUS branch is now in force: **neither** interest (`personal_state`) nor knowledge-state topics have predictive validation; no consumer (including `internet`'s `personal_state_top_terms` augmentation) may adopt either as a scoring input until a real-corpus run happens.

## Key files
`cdp.py` CDP client · `export_to_sqlite.py` fetch + `upsert()` (only DB writer) · `poll_conversations.py` poller · `resilient_import.py` supervisor · `council_bot.py` + `e2e_verify_bot.py` · `view_conversations_server.py` · `common.py` env/date/filename helpers · `personal_state.py` derives the versioned, privacy-safe interest-state artifact for other repos · `knowledge_state.py` derives the (not yet published/contracted) familiarity artifact, reusing `personal_state`'s tokenizer and writer · `eval_knowledge_state.py` frozen token-level replay-eval (run once) · `eval_future_self.py` frozen artifact-level replay-eval for personal_state's top-10 (run once), reusing `eval_knowledge_state._load_and_split`/`_build_train_conn`.

Personal-state contract (schema + version-bump procedure): `PERSONAL_STATE_CONTRACT.md`, currently v1. Knowledge-state pre-registration + results: `KNOWLEDGE_STATE_EXPERIMENT.md`. Future-self pre-registration + results: `FUTURE_SELF_EXPERIMENT.md`.

Weak-label definitions/thresholds/record schema/regenerate command: `WEAK_LABELS.md`.
## Adding a CLI flag (recurring task — no file reads needed)
Each script builds its own `argparse.ArgumentParser` inside `main()`. Flags that
`poll_conversations.py` must forward are read off `args` with
`getattr(args, "name", default)` in `export_to_sqlite.py`, because the poller
passes a `types.SimpleNamespace` rather than a parsed argparse result.

## Known issues
- ChatGPT backfill incomplete: ~242 of ~1630 conversations in DB (Claude: 21).
- Under chatgpt.com throttling, CDP runs die with `WinError 10053` / "WebSocket connection closed". Stop `poll_conversations.py` before a big backfill — its polling compounds the rate limit.

## Next task
When a real `conversations.db` (or `AI_CONVERSATIONS_DB`) is reachable, run BOTH pre-registered evals once each and append a fresh dated results section to each doc, after the existing sections — do not edit prior sections, do not re-run either with different settings:
```
python eval_knowledge_state.py --db conversations.db --out knowledge_state_eval.json   # -> KNOWLEDGE_STATE_EXPERIMENT.md
python eval_future_self.py --db conversations.db --out future_self_eval.json           # -> FUTURE_SELF_EXPERIMENT.md
```
Then finish the ChatGPT backfill: poller stopped, Chrome on port 9222, `python resilient_import.py chatgpt`.

## Commands to continue
```bash
chrome --remote-debugging-port=9222   # required by every CDP script
python resilient_import.py chatgpt
python council_bot.py                 # Telegram bridge; confirm council_bot.log is idle first
python view_conversations_server.py   # read-only local viewer
python -m unittest discover -p "test_*.py"
python -c "import sqlite3;print(sqlite3.connect('file:conversations.db?mode=ro',uri=True).execute('select source,count(*) from raw_conversations group by source').fetchall())"
```

## Loop closure guard (step-08)
Confirmed the existing `is_council_bot_scratch_conversation()` exclusion in
`export_to_sqlite.py` is this repo's anti-leakage back-channel guard, and it
was already covered by a test (`test_export_to_sqlite.py`,
`TestCouncilBotScratchConversationFilter`) asserting the scratch row never
reaches `raw_conversations`. Extended that test with one more case:
`personal_state.derive()` output is byte-identical whether or not an
excluded scratch conversation was present in the upstream batch, so the
exclusion has zero downstream side effect on the artifact, not just a
missing row. No production code changed; `personal_state.py`,
`knowledge_state.py`, and both eval scripts have zero diffs;
`CONTRACT_VERSION` stays `1`. Documented the one-directional data-flow
invariant (conversations → artifact → consumers, never back) and the
consumer-side provenance expectation (artifact sha256/`generated_at`/
`contract_version` recorded by any seeding consumer) in
`PERSONAL_STATE_CONTRACT.md`'s new "Loop closure and leakage (step-08)"
section, and reaffirmed the step-05 no-scoring-input gate is unchanged.
