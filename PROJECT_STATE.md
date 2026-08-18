# PROJECT_STATE.md — `ai`

Updated 2026-08-11. Imported by `CLAUDE.md`; maintained under its startup and
token-efficiency rules. Current state only — not a log, not an architecture doc.

## Implemented
Claude + ChatGPT export via CDP → SQLite; continuous poller; Telegram council bot; read-only web viewer (ngrok-exposed); one-off markdown→SQLite migration (done, no longer needed); resilient chunked-import supervisor; `corpus_backfill.py`, the slow/resumable/project-aware completeness backfill (own section below); privacy-safe `personal_state.py` (interest = normalized frequency, v1 contract); `knowledge_state.py` (familiarity = exposure × temporal spread × recency decay, structurally distinct from interest) plus `eval_knowledge_state.py`, a frozen TOKEN-level replay-eval harness (does familiarity rank future token recurrence better than interest weight?). `eval_future_self.py` is a second, separate frozen ARTIFACT-level replay-eval harness: does the frozen personal-state top-10 topic set at a historical T predict post-T conversations' titles (hit@10 vs. permutation-chance and recency baselines)? See `FUTURE_SELF_EXPERIMENT.md`.

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
## Corpus backfill (`corpus_backfill.py`)
Two phases, so the expensive phase always works off a fixed, inspectable plan:
`manifest` (cheap listing) then `run` (one detail fetch per missing
conversation, resumable, commits after every single one). `status` reports
completeness with no browser.

Measured live 2026-08-18, and the reason this exists separately from
`export_to_sqlite.py`:
- **ChatGPT Projects are invisible to the flat list.** 57 projects exist on this
  account. `/backend-api/conversations` carries `gizmo_id` for only some of
  their conversations; others are absent from it entirely (checked against a
  flat page spanning the same dates, so it is not a paging artifact). Projects
  are walked separately via `gizmos/snorlax/sidebar` +
  `gizmos/{id}/conversations` and merged into one manifest. `?gizmo_id=` on
  the flat endpoint is silently ignored, so it is not an alternative.
- **That project endpoint returns HTTP 200 `{"items": []}` for any `limit`
  above its cap** — 100 gives 0 items where 50/28/20 all give the same 6. A
  first pass at limit=100 therefore reported all 57 projects as empty.
  `PROJECT_CONVERSATIONS_PAGE_LIMIT` pins it to 50, and an empty *first* page is
  retried before being believed.
- **The Claude June/July 2026 gap is real, not an import bug.** claude.ai's own
  `chat_conversations_v2` returns 157 conversations total: 9 in April, 6 in May,
  142 in August, zero in June/July — and the DB already matched April/May
  exactly. Claude Code sessions never create claude.ai conversations, so a
  stretch of Claude-heavy work legitimately leaves no rows here. Do not "fix" it.
- Project attribution lands in `conversation_projects`, a purely additive side
  table rather than a new column on `raw_conversations`, so nothing already
  reading that table can be affected.

## Adding a CLI flag (recurring task — no file reads needed)
Each script builds its own `argparse.ArgumentParser` inside `main()`. Flags that
`poll_conversations.py` must forward are read off `args` with
`getattr(args, "name", default)` in `export_to_sqlite.py`, because the poller
passes a `types.SimpleNamespace` rather than a parsed argparse result.

## Known issues
- The corpus is being completed by `corpus_backfill.py` (see its section below), the sole writer of `conversations.db`. Measured on the servers 2026-08-18: **1898 conversations exist, of which 132 are machine scratch** (116 `discovery scratch` from the `internet` appliance, plus council_bot/extractor scratch), leaving **1766 real** — 1740 chatgpt, **26 claude**. **The last five months (updated >= 2026-03-18) are complete: 224/224 chatgpt, 26/26 claude, 63/63 project conversations.** Claude is complete outright. What remains is the pre-2026-03 chatgpt tail. Resume with `python corpus_backfill.py --db conversations.db run`.
- ChatGPT **Projects** conversations are partly missing from `/backend-api/conversations`; `corpus_backfill.py` enumerates them separately and records each conversation's project in `conversation_projects`. See its section below.
- **The Claude June/July 2026 gap is real, not an import defect** — claude.ai's own list returns 9 April, 6 May, 142 August and zero June/July, and the DB already matched April and May exactly. Claude Code sessions never create claude.ai conversations. Do not "fix" it.
- Anything the extractor derives is only as complete as the corpus; `interest_candidates.json` therefore carries a `corpus` block (counts, date range, coverage) so a candidate generated from a partial history is not mistaken for one generated from all of it.
- Under chatgpt.com throttling, CDP runs die with `WinError 10053` / "WebSocket connection closed". Stop `poll_conversations.py` before a big backfill — its polling compounds the rate limit. `corpus_backfill.py` reopens its own tab and retries the item instead of dying.

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
python corpus_backfill.py --db conversations.db manifest   # refresh what the servers have
python corpus_backfill.py --db conversations.db run        # slow, resumable; rerun to continue
python corpus_backfill.py --db conversations.db status     # offline completeness report
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
