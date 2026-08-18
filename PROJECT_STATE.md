# PROJECT_STATE.md — `ai`

Updated 2026-08-11. Imported by `CLAUDE.md`; maintained under its startup and
token-efficiency rules. Current state only — not a log, not an architecture doc.

## Implemented
Claude + ChatGPT export via CDP → SQLite; continuous poller; Telegram council bot; read-only web viewer (ngrok-exposed); one-off markdown→SQLite migration (done, no longer needed); resilient chunked-import supervisor; privacy-safe `personal_state.py` (interest = normalized frequency, v1 contract); `knowledge_state.py` (familiarity = exposure × temporal spread × recency decay, structurally distinct from interest) plus `eval_knowledge_state.py`, a frozen TOKEN-level replay-eval harness (does familiarity rank future token recurrence better than interest weight?). `eval_future_self.py` is a second, separate frozen ARTIFACT-level replay-eval harness: does the frozen personal-state top-10 topic set at a historical T predict post-T conversations' titles (hit@10 vs. permutation-chance and recency baselines)? See `FUTURE_SELF_EXPERIMENT.md`.

`interest_extractor.py` — the interest-intelligence producer (design doc 2026-08-17, PR G): a two-stage LLM pass over conversation BODIES (`map` per conversation -> `digests.db`, `reduce` over the aggregate) that emits evidence-bearing interest candidates as a **contract v2** artifact (`interest_candidates.json`) for `internet`'s `discovery/offers.py`. All LLM traffic goes through `claude_browser.py` (claude.ai in a logged-in Chrome tab over CDP) — **no `anthropic` import, no API key, ever** on this path.

`weak_labels.py` — read-only, stdlib-only extractor of 5 weak behavioral labels (depth, sustained_followup, rapid_abandonment, response_rejection, recurrence) per conversation, with provenance/confidence/degraded_reasons on every record. `weak_labels.json` is **local-only, gitignored, never-gold supervision data** — not part of the personal-state contract, not consumed by any other module (guard-tested). See `WEAK_LABELS.md` for label definitions, thresholds, and the recorded corpus distribution.
## Non-obvious decisions
- No intermediate markdown/json export files any more — CDP goes straight to `raw_conversations`. Old markdown-derived rows are marked `markdown_reconstructed` and superseded when the real API row arrives.
- `--after/--before` filter on `updated_at`, not `created_at`, so edited old conversations get re-imported.
- `council_bot`'s own scratch conversations are excluded at import time.
- Long-lived background runs on this machine get reaped silently (no traceback) — hence `resilient_import.py` + `--max-runtime-minutes`. Don't replace it with one long run.
- `knowledge_state.py`'s familiarity formula (saturation/spread-months/half-life constants) is pre-registered and frozen in `KNOWLEDGE_STATE_EXPERIMENT.md`'s append-only "Pre-registration" section, written before any evaluation number was observed. Do not tune it based on `eval_knowledge_state.py` output.
- `cdp.evaluate()` checked for `exceptionDetails` at the wrong nesting level until 2026-08-18, so **every JS exception was silently swallowed** and returned as the thrown value (`{}` for a rejected async IIFE). A logged-out claude.ai tab therefore read as "0 conversations" and advanced `poll_state.json`'s watermark past unimported history. Fixed + regression-tested (`test_cdp.py`); the failure mode is worth remembering because an auth error imitates "no new data" exactly.
- The claude.ai browser round trip lives in `claude_browser.py`, not `council_bot.py`. It moved so `interest_extractor.py` can drive claude.ai without importing `council_bot` (and therefore `anthropic`); `council_bot.py` re-exports the names, so existing imports/patches are unchanged.
- `interest_extractor.read_corpus()` excludes `council_bot` scratch conversations via the import path's own guard — the machine's deliberations must never shape the interests it proposes (same step-08 loop-closure reasoning).
- Extractor ranking is code-side and pure (`aggregate_themes`/`score_candidate`/`rank_candidates`); the model only rates `expected_yield` and candidate<->interest similarity. The durability separator (durable = span >=30d AND >=2 calendar months; transient = a single <7d burst) is the design's **measured** one and must not be re-derived.
- `personal_state.py --with-knowledge-state` (opt-in, default off) merges `knowledge_state.py`'s additive fields into personal_state's topic records by token key; default output (`contract_version` 1) is unchanged.
- Both pre-registered evals **have now been run** (2026-08-18; corpus 263 conversations, 2023-08-17 → 2026-08-06) and both recorded **FALSIFIED** — results appended to their docs (`eval_knowledge_state` dAUC +0.015, CI [-0.174, +0.186]; `eval_future_self` hit@10 0.0127 vs 0.0243 chance, p_perm 0.806). Both are **spent**: neither may be re-run and neither formula may be retuned in response. The step-05 adoption gate is now closed **on evidence** — `personal_state.weight` and `knowledge_state.familiarity` stay barred as scoring inputs. `interest_extractor`'s candidates are a different signal and are human-approved offers, so the gate does not cover them (see the contract's v2 section).

## Key files
`cdp.py` CDP client · `export_to_sqlite.py` fetch + `upsert()` (only DB writer) · `poll_conversations.py` poller · `resilient_import.py` supervisor · `council_bot.py` + `e2e_verify_bot.py` · `view_conversations_server.py` · `common.py` env/date/filename helpers · `personal_state.py` derives the versioned, privacy-safe interest-state artifact for other repos · `knowledge_state.py` derives the (not yet published/contracted) familiarity artifact, reusing `personal_state`'s tokenizer and writer · `eval_knowledge_state.py` frozen token-level replay-eval (run once) · `eval_future_self.py` frozen artifact-level replay-eval for personal_state's top-10 (run once), reusing `eval_knowledge_state._load_and_split`/`_build_train_conn`.

`interest_extractor.py` producer (map/reduce/status) · `claude_browser.py` the repo's only LLM transport (claude.ai over CDP) · `digests.db` its own checkpoint store (never inside `conversations.db`).

Personal-state contract (schema + version-bump procedure): `PERSONAL_STATE_CONTRACT.md`, v1 for `personal_state.json` plus a **v2** section for the extractor's `interest_candidates.json` (adds evidence quotes + `conversation_id`s; v1's tokens-only privacy invariant is deliberately relaxed there, per the owner). Knowledge-state pre-registration + results: `KNOWLEDGE_STATE_EXPERIMENT.md`. Future-self pre-registration + results: `FUTURE_SELF_EXPERIMENT.md`.

Weak-label definitions/thresholds/record schema/regenerate command: `WEAK_LABELS.md`.
## Adding a CLI flag (recurring task — no file reads needed)
Each script builds its own `argparse.ArgumentParser` inside `main()`. Flags that
`poll_conversations.py` must forward are read off `args` with
`getattr(args, "name", default)` in `export_to_sqlite.py`, because the poller
passes a `types.SimpleNamespace` rather than a parsed argparse result.

## Known issues
- The corpus is incomplete and is being backfilled by a **separate import agent** (worktree `C:/github/ai-wt-import`, branch `corpus/import-backfill`) which is now the sole writer of `conversations.db`. At 2026-08-18: 263 conversations (chatgpt 242 of ~1,630; claude 21), newest 2026-08-06, Claude June+July 2026 entirely absent.
- ChatGPT **Projects** conversations are missing from the corpus: 57 projects exist in the account, and 13 of the 14 conversations in the 5 newest projects are absent from `/backend-api/conversations` (the flat history the importer walks) despite falling inside its window. They are reachable at `/backend-api/gizmos/{gizmo_id}/conversations?limit=&cursor=`, and the project list at `/backend-api/gizmos/snorlax/sidebar` (cursor-paginated). Owned by the import agent, not by this repo's extractor.
- Anything the extractor derives is only as complete as the corpus; `interest_candidates.json` therefore carries a `corpus` block (counts, date range, coverage) so a candidate generated from a partial history is not mistaken for one generated from all of it.
- Under chatgpt.com throttling, CDP runs die with `WinError 10053` / "WebSocket connection closed". Stop `poll_conversations.py` before a big backfill — its polling compounds the rate limit.

## Next task
Both evals are done. The extractor's `map` backfill has been run against the
current corpus; re-run it whenever the import agent lands new conversations
(it is incremental on `content_hash`, so it only digests what it has not
seen), then re-run `reduce` to refresh the artifact:

```
python interest_extractor.py status                      # coverage, themes, durability mix
python interest_extractor.py map                         # resumable; skips finished work
python interest_extractor.py reduce --out interest_candidates.json
```

Then hand `interest_candidates.json` to `internet`'s `discovery/offers.py`
(`import_artifact`, PR H) — its reader already accepts `contract_version` 2.

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
`export_to_sqlite.py`'s existing `is_council_bot_scratch_conversation()`
exclusion is the anti-leakage back-channel guard; its test now also proves
`personal_state.derive()` output is byte-identical with/without an excluded
scratch row upstream (zero downstream side effect, not just a missing row).
No production code changed; `CONTRACT_VERSION` stays `1`. See
`PERSONAL_STATE_CONTRACT.md`'s "Loop closure and leakage (step-08)" section
for the one-directional flow invariant, consumer provenance expectation, and
the (unchanged) step-05 no-scoring-input gate.
