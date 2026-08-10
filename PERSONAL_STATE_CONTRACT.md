# Personal State Contract

**Current version: 1**

This document is the authoritative schema for the `personal_state.json`
artifact produced by `personal_state.py` in this repo (`ai`). It is derived
from `conversations.db` and is the only thing about the owner's
conversations this repo publishes for consumption by other repos
(specifically `internet`, via `discovery/personal_state.py`).

## Schema (v1)

Top-level object:

| Field               | Type   | Required | Meaning |
|---------------------|--------|----------|---------|
| `contract_version`  | int    | yes      | Always `1` for this version of the contract. |
| `generated_at`       | string | yes      | UTC ISO-8601 timestamp (`YYYY-MM-DDTHH:MM:SSZ`) of when the artifact was produced. |
| `window_days`        | int    | yes      | The `--window-days` value used to derive this artifact. `0` means no window (all conversations considered). |
| `conversation_count`  | int    | yes      | Number of conversations considered (i.e. that passed the window filter). |
| `sources`             | object | yes      | `{"claude": <int>, "chatgpt": <int>}` — count of considered conversations per source. |
| `topics`              | array  | yes      | List of topic objects (see below), sorted by `conversations` desc then `key` asc, truncated to `--max-topics`. |

Each entry in `topics`:

| Field           | Type   | Required | Meaning |
|-----------------|--------|----------|---------|
| `key`           | string | yes      | A lowercase token derived from conversation titles (see Derivation below). |
| `weight`        | float  | yes      | `round(conversations / max_conversations, 4)`, in `[0, 1]`, where `max_conversations` is the highest `conversations` count among the artifact's topics. |
| `conversations` | int    | yes      | Number of distinct conversations whose title contains this token. |
| `last_seen`     | string \| null | yes | The max `updated_at` (ISO-8601) among conversations containing this token, or `null` if none of those conversations have a parseable `updated_at` (e.g. rows with a NULL `updated_at`, such as `markdown_reconstructed` imports or Claude API responses that omitted it). Key is always present; consumers must handle a null value. |

## Derivation (informative — see `personal_state.py:derive()` for the
normative implementation)

- Reads only `source`, `title`, `updated_at` from `raw_conversations` —
  never `raw_json`.
- Lowercases each title, splits on non-alphanumeric characters.
- Drops tokens shorter than 3 chars, longer than 30 chars, purely numeric,
  or in the built-in `STOPWORDS` set (English function words plus generic
  chat/export noise like `chat`, `help`, `code`, `error`, `untitled`).
- Counts each token once per conversation (distinct conversations, not
  occurrences).
- Keeps tokens with `conversations >= min_conversations`.
- Sorts by `conversations` desc, then `key` asc, and takes the first
  `max_topics`.

## Forward compatibility

- Consumers **MUST** ignore unknown top-level keys and unknown per-topic
  keys.
- Producers **MAY** add new optional fields without bumping
  `CONTRACT_VERSION`. Only removing/renaming a required field, or changing
  what an existing field means, is a breaking change.

## Version-bump procedure

1. Bump `CONTRACT_VERSION` in `personal_state.py`.
2. Add a new dated section to this document describing the new version;
   keep the old section(s) for reference — do not delete them.
3. Extend `SUPPORTED_VERSIONS` in the `internet` repo's
   `discovery/personal_state.py` to include the new version.
4. Only after both repos are updated should producers start emitting the
   new version by default.

## Privacy invariant

The artifact carries **only** aggregate tokens and counts. It never
contains full titles, message bodies, `raw_json`, or `conversation_id`s.
This is what makes it safe to publish outside this repo — no consumer ever
needs to (or should) open `conversations.db`.

## Publication

This repo (`ai`) is the sole producer: running `personal_state.py` writes
`personal_state.json` (gitignored — it's derived data, regenerated on
demand, never committed). Consumers (e.g. `internet`) are pointed at the
file by path or env var (see `internet`'s `DISCOVERY_PERSONAL_STATE`); they
never open `conversations.db` directly.

## Verifying the contract

Build a throwaway SQLite DB with the `raw_conversations` schema and two
rows, then run the script against it:

```bash
python - <<'PY'
import sqlite3
from export_to_sqlite import SCHEMA

conn = sqlite3.connect("tmp_verify.db")
conn.executescript(SCHEMA)
conn.execute(
    "INSERT INTO raw_conversations (source, conversation_id, title, model, "
    "created_at, updated_at, raw_json, content_hash) VALUES (?,?,?,?,?,?,?,?)",
    ("claude", "c1", "Debugging the SQLite export script", "claude-3",
     "2026-01-01T00:00:00+00:00", "2026-01-02T00:00:00+00:00", "{}", "h1"),
)
conn.execute(
    "INSERT INTO raw_conversations (source, conversation_id, title, model, "
    "created_at, updated_at, raw_json, content_hash) VALUES (?,?,?,?,?,?,?,?)",
    ("chatgpt", "c2", "SQLite export script questions", "gpt-4",
     "2026-01-01T00:00:00+00:00", "2026-01-03T00:00:00+00:00", "{}", "h2"),
)
conn.commit()
conn.close()
PY

python personal_state.py --db tmp_verify.db --out tmp_verify.json --window-days 0 --min-conversations 2
cat tmp_verify.json
```

You should see a `topics` entry for `sqlite` and `export` (each appears in
both titles), with `conversations: 2` and `weight: 1.0`.

## Knowledge-state fields (additive, optional, v1)

These fields are produced by `knowledge_state.py` and are **not** part of
the default `personal_state.py` output. They are merged into each topic
record only when `personal_state.py` is run with `--with-knowledge-state`
(default off); with the flag off, output is byte-identical to the schema
above. Per "Forward compatibility" above, these are optional additive
per-topic fields, so `CONTRACT_VERSION` stays `1` and `internet`'s
`SUPPORTED_VERSIONS` requires no change.

| Field           | Type            | Nullable | Meaning |
|-----------------|-----------------|----------|---------|
| `first_seen`    | string          | yes      | Min `updated_at` (ISO-8601) among conversations containing this token, or `null` if none of those conversations have a parseable `updated_at`. |
| `last_seen`     | string          | yes      | Max `updated_at` (ISO-8601) among conversations containing this token, or `null` under the same condition (same nullability rule as v1's `last_seen`). |
| `span_days`     | int             | no       | Whole days between `first_seen` and `last_seen`; `0` if either is `null`. |
| `recency_days`  | int             | yes      | Whole days from `last_seen` to `now`; `null` if `last_seen` is `null`. |
| `active_months` | int             | no       | Count of distinct `(year, month)` pairs in which the token appeared. |
| `familiarity`   | float           | no       | `[0, 1]`, the frozen formula below. |

**Frozen familiarity formula** (pre-registered in
`KNOWLEDGE_STATE_EXPERIMENT.md`; not to be tuned post-hoc; see
`knowledge_state.py` for the normative implementation):

```
exposure = min(1.0, log(1 + conversations) / log(1 + SATURATION))     # SATURATION = 8
spread   = min(1.0, active_months / SPREAD_MONTHS)                    # SPREAD_MONTHS = 6
decay    = 0.5 ** (recency_days / HALF_LIFE_DAYS)  if recency_days is not None else 0.0   # HALF_LIFE_DAYS = 120
familiarity = round((0.5 * exposure + 0.5 * spread) * decay, 4)
```

Conceptually: interest (`weight`, above) measures how OFTEN a topic comes
up; `familiarity` measures accumulated exposure SPREAD OVER TIME and
decayed by how long ago it was last touched. Two topics with identical
`conversations` counts get different `familiarity` if one was a single-day
burst and the other recurred across months.

**Status:** INCONCLUSIVE -- NO CORPUS AVAILABLE on replay 2026-08-10 -- the
pre-registered replay evaluation could not be run (no `conversations.db`
reachable from the automation clone; see `KNOWLEDGE_STATE_EXPERIMENT.md`).
These fields therefore have **no measured predictive validity yet** and
MUST NOT be used as a novelty-scoring input pending a real evaluation run;
they are descriptive/opt-in only. See `KNOWLEDGE_STATE_EXPERIMENT.md` for
the full record and the command to produce a real result later.

## Loop closure and leakage (step-08)

The artifact flows **one direction only**: `conversations.db` →
`personal_state.json` → consumers. Nothing a consumer produces or does with
the artifact is ever allowed to flow back into the input side of that
arrow.

- No consumer output — discovery items, digests, council/bot replies, or
  any other derived text — may be written into `raw_conversations`, and
  none may be fed back into `personal_state.py`'s or `knowledge_state.py`'s
  derivation. Derivation reads only real owner conversations.
- The existing `is_council_bot_scratch_conversation()` exclusion in
  `export_to_sqlite.py` (see its docstring) is this repo's concrete
  instance of that guard for `council_bot.py`'s own scratch conversations:
  they never reach `raw_conversations`, so they can never contribute a
  token to a future artifact. Covered by
  `test_export_to_sqlite.py::TestCouncilBotScratchConversationFilter`,
  including a case asserting `personal_state.derive()` output is
  byte-identical whether or not an excluded scratch conversation was ever
  present in the upstream batch — exclusion has zero downstream side
  effect, not just "the row is missing."
- Consumers that seed their own state from this artifact should record the
  artifact's identity in their own provenance, not just the values they
  read from it: sha256 of the artifact file's bytes, its `generated_at`,
  and its `contract_version`. As of step-08, `internet`'s `interest_events`
  does this for its `personal_state`-seeded rows.
- The step-05 adoption gate remains in force, unchanged by this step: per
  `FUTURE_SELF_EXPERIMENT.md`'s INCONCLUSIVE-NO-CORPUS branch, neither
  `weight` (interest) nor `familiarity` (knowledge-state) may be used as a
  scoring input anywhere until a real-corpus replay eval has actually run.
  This section adds documentation and test coverage only — it does not
  change `CONTRACT_VERSION` (still `1`), and `personal_state.py`,
  `knowledge_state.py`, `eval_knowledge_state.py`, and `eval_future_self.py`
  are unmodified by step-08.
