# Future-Self Experiment

This document pre-registers the hypothesis, metric, and decision rule for
evaluating whether `personal_state.py`'s frozen top-10 interest topic set,
built with data available only up to a historical time T, predicts the
topics of conversations that happen AFTER T. Written and committed BEFORE
any evaluation number was observed. The section below is **append-only**:
once execution starts, it must never be edited. Results go in a new section
appended after it.

## Background

`personal_state.py` (step-02) publishes `weight`, a pure normalized
FREQUENCY of a title token, and calls it an INTEREST signal. This
experiment is distinct from `KNOWLEDGE_STATE_EXPERIMENT.md`'s: that one
asks a TOKEN-level question (does `knowledge_state`'s familiarity rank
future token recurrence better than interest weight?). This one asks an
ARTIFACT-level question: if you had frozen the interest artifact's top-10
topics at some past moment T, using only what was knowable at T, would that
frozen set actually have told you anything about what you'd go on to talk
about? Nothing here folds knowledge-state familiarity in; step-03's H1 has
its own frozen protocol and remains separately queued.

## Pre-registration (frozen)

**HYPOTHESIS H1:** The frozen personal-state top-10 topic set at time T
(produced by `personal_state.derive()` over conversations with
`updated_at <= T` only, using the production 180-day window anchored at T)
predicts post-T conversations: the fraction of post-T conversations whose
title tokens intersect the frozen top-10 ("hit@10 rate") exceeds both (a) a
random-topic chance baseline and (b) a recency-ranking baseline.

**DEFINITION OF T:** Identical mechanics to
`eval_knowledge_state._load_and_split`: chronologically sort all
`raw_conversations` rows on parsed `updated_at`; drop rows whose
`updated_at` is missing/unparseable and report the drop count; split 70/30
(TRAIN = first 70%, TEST = last 30%); T = `updated_at` of the last TRAIN
row.

**FROZEN-STATE CONSTRUCTION:** TRAIN rows are filtered to
`[T - 180 days, T]` (the production 180-day window, anchored at T rather
than at wall-clock "now") and inserted into an in-memory
`raw_conversations` DB containing ONLY those rows. `personal_state.derive()`
is called on that DB alone with `window_days=0` (the window filter has
already been applied by the pre-filter step) and `max_topics` large enough
to disable truncation. That full, untruncated topic list IS the candidate
pool (every token with >= 2 TRAIN conversations inside the window — exactly
`derive()`'s kept set before `max_topics` truncation); its first 10 entries
(already weight-desc, token-asc, per `derive()`'s own sort) are the frozen
INTEREST top-10.

**BASELINES:**
- B0 (chance) = permutation baseline: 2000 resamples, `random.Random(1234)`,
  each drawing 10 tokens uniformly without replacement from the candidate
  pool; hit@10 rate computed per resample against the same post-T (TEST)
  conversations.
- B1 (recency) = top-10 tokens from the same candidate pool ranked by most
  recent pre-T `last_seen` (ties broken alphabetically ascending).

**PRIMARY METRIC:** hit@10 rate over post-T (TEST) conversations — a
post-T conversation is a "hit" if `personal_state._tokenize(title)`
intersects the top-10 set. `p_perm = (count of permuted resamples with rate
>= observed INTEREST rate + 1) / (2000 + 1)`. Effect size = observed
INTEREST rate minus mean permuted rate. Also reported: hit@10 for B1,
n_test, n_train, n_dropped_rows, cutoff T, candidate pool size, and seed.

**DECISION RULE (mechanical, no softening):**
- **INCONCLUSIVE** if no corpus reachable, or n_test < 30, or candidate
  pool < 30 tokens, or the frozen artifact has fewer than 10 topics.
- **SUPPORTED** iff p_perm < 0.05 AND INTEREST hit@10 >= RECENCY hit@10.
- **FALSIFIED** otherwise (p_perm >= 0.05 OR INTEREST hit@10 < RECENCY
  hit@10).

**STOPPING CONDITION:** exactly one evaluation run on the frozen split with
seed 1234. No re-runs with different K, seed, window, split fraction, or
thresholds.

**FALSIFICATION CONDITION:** as stated in the decision rule above. A
falsified hypothesis, properly measured and honestly recorded, is a
successful outcome for this step — it is not a reason to retune the split,
the window, K, or the baselines and try again.

**LEAKAGE PROTECTIONS:**
1. The frozen state is built from an in-memory DB containing ONLY rows
   whose parsed `updated_at` falls in `[T - 180d, T]` — TEST rows are never
   inserted into it, so `personal_state.derive()` never sees a single
   post-T row.
2. The 180-day window is anchored at T, not at wall-clock "now" — no
   information from after T ever enters the window filter (a TRAIN row
   older than `T - 180d` is excluded from the frozen state even though it
   is itself pre-T; see the window-anchoring test).
3. The candidate pool (and therefore the permutation universe and the
   recency ranking) is computed entirely from the TRAIN-only frozen DB,
   before any TEST label is consulted for anything.
4. The on-disk `conversations.db` is opened strictly read-only
   (`file:...?mode=ro`, `uri=True`) and this script never issues an
   INSERT/UPDATE/DELETE against it.
5. The emitted report contains only aggregate counts/rates — no token
   strings, no conversation_ids, no verbatim titles, no `raw_json` content.

**IMPLICATION MAPPING (pre-registered before any outcome is seen):**
- **SUPPORTED** -> later steps may treat frozen personal-state top topics
  as a validated short-horizon predictive prior.
- **FALSIFIED** -> later steps must treat personal-state topics as
  descriptive-only and must not give them predictive weight in any
  scoring/recommendation consumer (including `internet`'s
  `personal_state_top_terms` augmentation) without a different validated
  signal.
- **INCONCLUSIVE — NO CORPUS AVAILABLE** -> later steps must assume
  NEITHER interest nor knowledge-state has predictive validation; both
  this eval and `eval_knowledge_state.py` stay queued for the first session
  with a real `conversations.db`; no consumer may adopt either as a
  scoring input until then.

## Protocol (implemented in `eval_future_self.py`, do not restate here
divergently)

1. Load all `raw_conversations` rows and split 70/30 by `updated_at`
   exactly as `eval_knowledge_state._load_and_split` (reused, not
   duplicated). T = `updated_at` of the last TRAIN row.
2. Filter TRAIN rows to `[T - 180d, T]`, insert into an in-memory
   `raw_conversations` DB, call `personal_state.derive(window_days=0,
   max_topics=<huge>, min_conversations=2)` on it. The returned topic list
   is the candidate pool; its first 10 entries are the frozen INTEREST
   top-10.
3. B1: sort the candidate pool by most-recent `last_seen` descending
   (ties: token ascending); take the first 10.
4. B0: `random.Random(seed)`, 2000 resamples, each `rng.sample(pool, 10)`
   without replacement; hit@10 computed against TEST conversations for
   each resample.
5. Compute hit@10 for INTEREST and RECENCY over TEST conversations; compute
   `p_perm` and effect size as defined above.
6. Apply the decision rule mechanically and emit the JSON report.

## Results (2026-08-10)

**Corpus search (per pre-registered posture, in order):**
1. `conversations.db` in this clone's repo root — **not present** (it is
   gitignored derived data; this automation worktree clone does not carry
   it).
2. `AI_CONVERSATIONS_DB` environment variable — **not set** in this
   session's environment.

No real corpus was reachable from this worktree. Command run (exactly
once, per the stopping condition):

```
python eval_future_self.py --db conversations.db --out future_self_eval.json
```

Output (`future_self_eval.json`, gitignored):

```json
{
  "checked_paths": ["conversations.db"],
  "cutoff": null,
  "effect_size": null,
  "interest_hit_at_10": null,
  "mean_permuted_hit_at_10": null,
  "n_candidates": null,
  "n_dropped_rows": null,
  "n_resamples": 2000,
  "n_test": null,
  "n_train": null,
  "p_perm": null,
  "recency_hit_at_10": null,
  "seed": 1234,
  "verdict": "INCONCLUSIVE — NO CORPUS AVAILABLE"
}
```

**VERDICT: INCONCLUSIVE — NO CORPUS AVAILABLE.**

This is not one of the three outcomes the decision rule adjudicates
(SUPPORTED / FALSIFIED / INCONCLUSIVE-by-n); it is a precondition failure —
the evaluation could not be executed at all in this environment. It is
recorded honestly rather than manufactured, matching
`KNOWLEDGE_STATE_EXPERIMENT.md`'s prior corpus-search outcome in this same
clone.

### Harness validation only, not evidence about the owner

To confirm the harness itself is sound and ready to run against a real
corpus, its offline test suite was executed:

```
python -m unittest test_eval_future_self -v
```

Result: all 10 tests pass, including the planted-signal and
shuffled-labels self-tests. Actual figures observed (harness sanity only —
both corpora below are synthetic and constructed so the outcome is
predictable/unpredictable by design; these numbers say nothing about the
real owner's data):

- **Planted-signal corpus** (10 keeper tokens with 5 TRAIN conversations
  each, spread across the window and each recurring in TEST by
  construction, vs. 25 noise tokens with 2 TRAIN conversations each that
  never recur): n_train=100, n_test=43, n_candidates=35,
  `interest_hit@10 = 1.0000`, `recency_hit@10 = 0.0000` (the noise tokens
  happen to be more recent than the keepers in this synthetic layout, so
  B1 picks zero-signal tokens by construction), `mean_permuted_hit@10 =
  0.2835`, `p_perm = 0.0005`, `effect_size = 0.7165` → **SUPPORTED**.
- **Shuffled/unrelated-titles corpus** (same conversation counts and
  shape, titles reassigned from a random word pool so pre-T topic
  identity carries no relationship to post-T topics): n_train=77,
  n_test=33, n_candidates=43, `interest_hit@10 = 0.2727`,
  `recency_hit@10 = 0.1515`, `mean_permuted_hit@10 = 0.2248`,
  `p_perm = 0.2989`, `effect_size = 0.0479` → **FALSIFIED** (p_perm did
  not clear 0.05).

These confirm the split/frozen-state/candidate-pool/permutation/decision
machinery behaves correctly on data where the ground-truth relationship is
known by construction. They are **not** a measurement of whether frozen
personal-state topics predict the owner's real future conversations — that
requires a real corpus, and per the pre-registration's stopping condition,
running the harness now on a substitute would not count as (and must not
be reported as) the pre-registered result.

### Implication mapping now in force

Per the pre-registered IMPLICATION MAPPING above: **INCONCLUSIVE — NO
CORPUS AVAILABLE** is in force. Later steps must assume NEITHER interest
nor knowledge-state has predictive validation; both this eval and
`eval_knowledge_state.py` stay queued for the first session with a real
`conversations.db`; no consumer (including `internet`'s
`personal_state_top_terms` augmentation) may adopt either signal as a
scoring input until then.

### How to produce the real result later

Run once, on a real `conversations.db`, and append a fresh dated `##
Results` section (do not edit this one):

```
python eval_future_self.py --db conversations.db --out future_self_eval.json
```

(or point `--db` at the path held in `AI_CONVERSATIONS_DB` / wherever the
owner's real export lives). Per the pre-registration, run it exactly once
and record whatever verdict falls out mechanically.

## Results (2026-08-18) -- the pre-registered run, on the real corpus

The corpus became reachable (`conversations.db` in this clone's repo root,
263 conversations). Per the stopping condition the harness was run exactly
ONCE, frozen split, K=10, 180-day window anchored at T, seed 1234:

```
python eval_future_self.py --db conversations.db --out future_self_eval.json
```

**Observed (full report: `future_self_eval.json`, gitignored):**

| Quantity | Value |
| --- | --- |
| n_train / n_test | 184 / 79 (0 dropped rows) |
| cutoff T | 2026-05-20T18:27:57Z |
| candidate pool | 79 tokens |
| INTEREST hit@10 | **0.0127** (1 of 79 post-T conversations) |
| RECENCY hit@10 (B1) | 0.0253 |
| chance mean permuted hit@10 (B0) | 0.0243 |
| p_perm | **0.8061** |
| effect size (INTEREST - chance) | **-0.0116** |
| verdict emitted by the harness | **FALSIFIED** |

**CORPUS SNAPSHOT THIS RESULT IS MEASURED AGAINST** (state it whenever these
numbers are quoted -- the corpus is actively being backfilled, so a later,
larger corpus is a different measurement, not a correction of this one):

- 263 conversations: chatgpt 242, claude 21.
- `created_at` range 2023-08-17 -> 2026-08-06; the newest row is ~12 days
  behind wall clock at run time (2026-08-18), i.e. a head gap on both sources.
- Known holes at run time: ChatGPT backfill ~15% complete (242 of ~1,630),
  Claude June+July 2026 entirely absent (zero rows), and ChatGPT *Projects*
  conversations never imported at all (57 projects exist in the account; a
  flat-history importer does not see their conversations).

**VERDICT: FALSIFIED.**

Not INCONCLUSIVE: n_test = 79 >= 30, candidate pool = 79 >= 30, and the
frozen artifact carried a full 10 topics. Both FALSIFIED clauses fire
independently: p_perm = 0.806 is nowhere near < 0.05, and INTEREST hit@10
(0.0127) is below RECENCY hit@10 (0.0253).

The frozen top-10 interest set did **worse than drawing 10 tokens at
random** from the same pool (0.0127 vs a 0.0243 permuted mean), and worse
than simply taking the 10 most recently seen tokens. The most-frequent title
tokens as of 2026-05-20 predicted essentially nothing about which
conversations the owner would have next. This is a clean falsification, and
per the pre-registration that is a successful outcome for the step -- the
split, window, K, and baselines are not to be retuned in response to it.

Worth stating because it is the mechanism, not a caveat: the corpus turns
over faster than a frequency ranking tracks. Title tokens that dominated a
180-day window are largely spent by the time the next 79 conversations
happen; the owner moves onto new themes (the gaming cluster, supplements)
that a backward-looking frequency count cannot anticipate. The same 15%
backfill limitation noted in `KNOWLEDGE_STATE_EXPERIMENT.md`'s 2026-08-18
results applies here too.

### Implication mapping now in force

Per the pre-registered IMPLICATION MAPPING: the **FALSIFIED** branch is in
force, replacing the 2026-08-10 INCONCLUSIVE -- NO CORPUS branch.

> later steps must treat personal-state topics as descriptive-only and must
> not give them predictive weight in any scoring/recommendation consumer
> (including `internet`'s `personal_state_top_terms` augmentation) without a
> different validated signal.

Concretely, and for consumers to rely on:
- `personal_state.py`'s `weight` and `knowledge_state.py`'s `familiarity`
  remain barred as scoring inputs. The adoption gate is now closed on
  evidence rather than pending on a missing corpus, and both evals are
  spent -- neither may be re-run to try for a different answer.
- The token ladder in `internet`'s `interest_state.py` should stay dormant
  (which matches the owner's 2026-08-17 decision to leave it dormant once
  offers ship).
- `interest_extractor.py` (added in the same step) is NOT covered by this
  bar and does not evade it: it is a *different signal* (LLM extraction over
  conversation bodies, not title-token frequency), and its output is a
  human-approved offer -- the owner accepts or rejects each candidate before
  anything reaches the scorer. Nothing it derives is used as an automatic
  scoring weight. If a future step wants to make extractor confidence itself
  a scoring input, that needs its own pre-registered eval.
