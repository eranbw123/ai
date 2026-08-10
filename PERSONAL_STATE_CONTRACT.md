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
