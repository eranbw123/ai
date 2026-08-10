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
