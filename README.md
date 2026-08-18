# ai

Personal conversation export + council bot. Pulls your own Claude.ai and
ChatGPT conversation history into a local SQLite database via browser
automation (Chrome DevTools Protocol), keeps it continuously up to date, and
exposes it to a few small tools: a Telegram bridge that runs a 5-advisor +
chairman "council" deliberation against a random past conversation for
context, a read-only local web viewer, and a versioned "personal state"
artifact (topic summary) for consumption by other repos.

## What it does

- **Export**: `claude_export_cdp.py` / `chatgpt_export_cdp.py` drive an
  already-logged-in Chrome tab over CDP (see `cdp.py`) to pull conversations
  from claude.ai and chatgpt.com — no cookies or scraping libraries, the
  browser supplies its own auth. `export_to_sqlite.py` fetches and writes
  conversations straight into `conversations.db`'s `raw_conversations` table
  through a single `upsert()` path (with retry/backoff and payload
  validation to avoid silent data loss).
- **Continuous sync**: `poll_conversations.py` runs `export_to_sqlite.py` on
  a loop, bootstrapping a lookback window and then polling on
  `updated_at` (so edited old conversations get re-imported, not just new
  ones). `resilient_import.py` is a supervisor for large backfills: it runs
  the importer in bounded time chunks with cooldowns and crash/failure
  limits, since long-lived background runs on the host get silently reaped.
- **Council bot** (`council_bot.py`): a Telegram long-poller, restricted to
  one authorized chat, that answers each message by running a 5-advisor +
  chairman deliberation with a random past conversation as context, and
  replies with only the chairman's final call. Two backends: `api` (default,
  calls the Anthropic API) or `browser` (drives claude.ai over CDP instead of
  an API key; quick-POC only).
- **Viewer** (`view_conversations_server.py`): a minimal, read-only local web
  server over `conversations.db` (opened in SQLite read-only URI mode) with
  HTTP Basic Auth, meant to be exposed via an ngrok tunnel for phone access.
  Fixed views only (conversation list, transcript, table list, raw row
  browser) — no arbitrary SQL.
- **Personal state artifact** (`personal_state.py`): derives a small,
  privacy-safe JSON summary of conversation topics (token frequency by
  title, windowed by recency) from `conversations.db` and writes it out for
  other repos to consume, per the schema in `PERSONAL_STATE_CONTRACT.md`
  (current version: 1). Read-only — never writes to `conversations.db`.
  Opt-in `--with-knowledge-state` merges `knowledge_state.py`'s additive
  fields into the topic records by token key; the default (contract
  version 1) output is unchanged.
- **Knowledge state** (`knowledge_state.py`): a separate, not-yet-published
  experiment that derives per-topic `familiarity` — exposure depth × temporal
  spread × recency decay — as opposed to `personal_state.py`'s plain
  frequency-based `weight`. Reuses `personal_state`'s tokenizer/writer rather
  than duplicating them; same read-only, no-conversation-id privacy
  invariant. `eval_knowledge_state.py` is a frozen, pre-registered replay-eval
  harness that compares `familiarity` against frequency and recency
  baselines on a train/test split of the real corpus; see
  `KNOWLEDGE_STATE_EXPERIMENT.md` for the pre-registration and results (run
  once so far — no real corpus was reachable in that environment, so the
  recorded verdict is "inconclusive, no corpus available"; the harness
  self-test suite passes). `familiarity` is descriptive-only and not used by
  anything unless `personal_state.py` is run with `--with-knowledge-state`.
- **Future-self experiment** (`eval_future_self.py`): a second, separate
  frozen replay-eval harness, this one at the ARTIFACT level rather than the
  token level — does the frozen `personal_state.py` top-10 topic set at a
  historical time T predict the titles of conversations that happen after
  T (hit@10 rate vs. a permutation-chance baseline and a recency-ranking
  baseline)? Reuses `eval_knowledge_state.py`'s train/test split and
  `personal_state.py`'s tokenizer/`derive()` rather than duplicating them.
  See `FUTURE_SELF_EXPERIMENT.md` for the pre-registration and results (run
  once so far — no real corpus was reachable in that environment, so the
  recorded verdict is "inconclusive, no corpus available"; the harness
  self-test suite passes). Per its pre-registered implication mapping, until
  a real-corpus run happens, no consumer may adopt either `personal_state.py`
  interest or `knowledge_state.py` familiarity topics as a scoring input.
- **Weak labels** (`weak_labels.py`): a read-only, deterministic, no-LLM
  extractor of five weak per-conversation behavioral labels (`depth`,
  `sustained_followup`, `rapid_abandonment`, `response_rejection`,
  `recurrence`), each with a `confidence`, `degraded_reasons`, and
  `provenance` record. Its output, `weak_labels.json`, is local-only
  (gitignored), never treated as ground truth, and not consumed by
  `personal_state.py`/`knowledge_state.py` or any other module (a dedicated
  test guards against that). See `WEAK_LABELS.md` for label definitions,
  thresholds, the recorded real-corpus distribution, and how to regenerate.
- `migrate_md_to_sqlite.py` is a one-off migration from an older
  markdown-export layout into SQLite; no longer needed for normal use.

## Setup

1. Copy `.env.local.example` to `.env.local` and fill in what you need:
   - `CLAUDE_ORG_ID` for Claude export (Chrome supplies auth).
   - `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` for `council_bot.py`.
   - `ANTHROPIC_API_KEY` for the council bot's `api` backend.
   - `NTFY_TOPIC` / `NTFY_BASE` (optional) for push notifications.
2. `pip install -r requirements.txt` (only third-party dependency is
   `anthropic`, needed by `council_bot.py`'s `api` backend).
3. Launch Chrome with remote debugging and a logged-in tab, e.g.:
   ```
   chrome --remote-debugging-port=9222
   ```
4. Run an export, e.g. `python export_to_sqlite.py claude` /
   `python export_to_sqlite.py chatgpt`, or `python resilient_import.py
   chatgpt` for a chunked backfill.

## Backfilling the full corpus

`export_to_sqlite.py` keeps the corpus current; `corpus_backfill.py` is for
making it *complete*, which is a different job. It runs in two phases so the
expensive one always works off a fixed, inspectable plan:

```
python corpus_backfill.py --db conversations.db manifest   # cheap: list everything the servers have
python corpus_backfill.py --db conversations.db run        # slow: fetch what is missing
python corpus_backfill.py --db conversations.db status     # offline completeness report
```

`run` is resumable and idempotent — rerun the same command to continue. It
processes newest-first, commits after every single conversation, skips anything
already up to date without issuing a request, and appends one line per outcome
to `backfill_progress.jsonl`.

It is deliberately slow and polite: jittered delays, a longer pause every 25
conversations, and an exponential back-off that treats any rate-limit signal as
a reason to get much slower rather than to retry. It opens its **own** Chrome
tab and closes only that tab, so it can share a browser with other tooling.

Two things it handles that a flat-list walk does not:

- **ChatGPT Projects.** Conversations inside a project are not reliably in
  `/backend-api/conversations`, so projects are enumerated separately and
  merged. Each conversation's project is recorded in the `conversation_projects`
  table.
- **Empty responses that are not empty.** The project-conversations endpoint
  returns a `200` with `{"items": []}` for a page size above its cap, so page
  sizes are pinned and an unexpectedly empty first page is retried before being
  believed.

## Tests

Offline unit tests only (stub CDP/Chrome, Telegram, Anthropic, network):

```
python -m unittest discover -p "test_*.py"
```

Run automatically on every PR/push to `main` via GitHub Actions
(`.github/workflows`). `e2e_verify_bot.py` is a separate mock end-to-end
check for `council_bot.py`, run by hand — it's not part of the offline suite.

## Main files

- `cdp.py` — minimal stdlib CDP client (WebSocket + `Runtime.evaluate`).
- `common.py` — shared env/date/filename helpers, `load_env_local()`.
- `claude_export_cdp.py`, `chatgpt_export_cdp.py` — CDP-driven exporters.
- `export_to_sqlite.py` — fetch + `upsert()` into `conversations.db` (the
  only DB write path; reused by the poller and importers).
- `poll_conversations.py` — continuous polling wrapper around the exporters.
- `resilient_import.py` — chunked/supervised backfill runner.
- `corpus_backfill.py` — slow, resumable, project-aware corpus completeness
  backfill (see above).
- `council_bot.py` / `e2e_verify_bot.py` — Telegram council bridge and its
  mock E2E check.
- `view_conversations_server.py` — read-only local web viewer.
- `personal_state.py` / `PERSONAL_STATE_CONTRACT.md` — topic-state artifact
  and its versioned schema contract.
- `knowledge_state.py` / `eval_knowledge_state.py` /
  `KNOWLEDGE_STATE_EXPERIMENT.md` — experimental familiarity artifact and its
  frozen replay-eval harness/pre-registration.
- `eval_future_self.py` / `FUTURE_SELF_EXPERIMENT.md` — frozen artifact-level
  replay-eval harness (does frozen personal-state top-10 predict future
  conversation titles?) and its pre-registration/results.
- `weak_labels.py` / `WEAK_LABELS.md` — read-only weak behavioral label
  extractor and its label/threshold documentation.
- `migrate_md_to_sqlite.py` — legacy one-off markdown→SQLite migration.
