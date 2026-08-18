# Knowledge State Experiment

This document pre-registers the hypothesis, metric, and decision rule for
evaluating `knowledge_state.py`'s `familiarity` score, BEFORE any evaluation
number was observed. The section below is **append-only**: once Task 2
(execution) starts, it must never be edited. Results go in a new section
appended after it.

## Background

`personal_state.py` (step-02) publishes `weight`, a pure normalized
FREQUENCY of a title token: how OFTEN a topic comes up. That's an INTEREST
signal. `knowledge_state.py` (this step) claims something different:
KNOWLEDGE is accumulated exposure SPREAD OVER TIME and decayed by how long
ago it was last touched. This experiment asks whether that composite signal
actually predicts something interest-frequency alone doesn't: whether the
owner keeps returning to a topic.

## Pre-registration (frozen)

**HYPOTHESIS H1:** A knowledge-state familiarity score that combines
exposure depth, temporal spread, and recency decay ranks a topic's future
recurrence better than step-02's interest weight (normalized frequency)
alone.

**BASELINES:**
- B0 = train conversation count (== step-02 interest weight ranking)
- B1 = recency alone

**PRIMARY METRIC:** ROC-AUC over the candidate topic set; headline effect
size dAUC = AUC(K1) - AUC(B0).

**DECISION RULE (fixed in advance):**
- **SUPPORTED** iff dAUC >= +0.05 AND the 95% bootstrap CI for dAUC excludes
  0 AND AUC(K1) > AUC(B1).
- **FALSIFIED** if dAUC <= 0, or if the 95% CI for dAUC includes 0, or if B1
  (recency alone) matches/beats K1 -- in the last case the composite has not
  earned its complexity.
- **INCONCLUSIVE** if the candidate set has n < 50 topics, or n_pos == 0, or
  n_neg == 0.

**STOPPING CONDITION:** the harness is run ONCE against the real corpus on
the frozen split with the frozen formula. No re-running with a changed
formula, changed split, changed candidate rule, or changed metric. If the
implementer wants to explore variants, they must split TRAIN internally
(first 70% of TRAIN as dev) and label any such number explicitly as a
dev-split exploration that does not touch the pre-registered result.

**FALSIFICATION CONDITION:** as in the decision rule. A falsified
hypothesis, properly measured and honestly recorded, is a successful outcome
for this step. The formula, the metric, and the decision rule must not be
changed after seeing the result.

**LABEL PROXY CAVEAT:** the label (`y=1` iff the token appears in >= 1 TEST
conversation) is a PROXY for "already known," not a direct measurement of
it. A topic can be absent from TEST because the owner already learned it
and moved on, OR simply because interest moved on for unrelated reasons
(the topic went stale, got superseded, was seasonal, etc.). This metric
cannot separate those two cases -- a result here says "the score predicts
whether the owner keeps returning to it," not "the score measures how much
the owner has learned."

## Protocol (implemented in `eval_knowledge_state.py`, do not restate here
divergently)

1. Load all `raw_conversations` rows (window_days = 0, i.e. ALL history).
   Drop rows whose `updated_at` is missing/unparseable; count and report how
   many were dropped.
2. Sort remaining conversations by `updated_at` ascending. TRAIN = first 70%
   of conversations, TEST = last 30%. `cutoff` = `updated_at` of the last
   TRAIN conversation.
3. CANDIDATE SET = every token appearing in >= 2 distinct TRAIN
   conversations, computed from TRAIN only -- never filtered using TEST.
4. LABEL for each candidate token: y = 1 if the token appears in >= 1 TEST
   conversation ("still live"), else y = 0 ("settled/dropped").
5. Three rankings scored over the same candidate set, all computed from
   TRAIN only with `now = cutoff`:
   - B0: TRAIN conversation count for the token.
   - B1: `-recency_days`.
   - K1: `knowledge_state` `familiarity`.
6. Tie-corrected Mann-Whitney AUC: `AUC = (sum of average-ranks of
   positives - n_pos*(n_pos+1)/2) / (n_pos*n_neg)`.
7. Secondary: precision@20 for B0 and K1; dAUC = AUC(K1) - AUC(B0)
   (AUC(K1) - AUC(B1) is derivable from the three reported AUCs).
8. Paired bootstrap over candidate topics, 2000 resamples,
   `random.Random(seed)` (seed default 1234), resampling topics with
   replacement and recomputing AUCs per resample; 95% percentile CI for
   dAUC; degenerate (all-one-label) resamples are skipped and counted.
9. JSON report with ONLY aggregate numbers -- no token strings.

## Results (2026-08-10)

**Corpus search (per pre-registered Step 1, in order):**
1. `conversations.db` in this clone's repo root -- **not present** (it is
   gitignored derived data; this automation worktree clone does not carry
   it).
2. `AI_CONVERSATIONS_DB` environment variable -- **not set** in this
   session's environment.

No real corpus was reachable from this worktree. Per the pre-registration's
own instruction ("do NOT go looking outside the workspace clone, and do NOT
copy the owner's DB from anywhere"), the replay evaluation was **not run**
against real data, and no fabricated corpus was substituted.

**VERDICT: INCONCLUSIVE -- NO CORPUS AVAILABLE.**

This is not one of the three outcomes the decision rule adjudicates
(SUPPORTED / FALSIFIED / INCONCLUSIVE-by-n); it is a precondition failure --
the evaluation could not be executed at all in this environment. It is
recorded honestly rather than manufactured.

### Harness validation only, not evidence about the owner

To confirm the harness itself is sound and ready to run against a real
corpus, its planted-signal self-test suite was executed:

```
python -m unittest test_knowledge_state -v
```

Result: all 10 tests pass, including the two planted-signal harness
self-tests. Actual figures observed (harness sanity only -- the corpus is
synthetic and constructed so recurrence is predictable by design; these
numbers say nothing about the real owner's data):

- `test_planted_signal_predictive`: on a synthetic 100-conversation corpus
  with 30 planted "keeper" tokens (spread across months, recurring into
  TEST) and 5 planted "dropped" tokens (single-day burst, TRAIN-only),
  n_candidates=35, n_pos=30, n_neg=5 -- `AUC(K1) = 1.0000`,
  `AUC(B0) = 0.5000` (B0 is tied at exactly 2 TRAIN conversations per
  planted token by construction, so it carries no signal; its tie-corrected
  AUC lands at exactly the chance value, which also sanity-checks the
  tie-correction logic), `AUC(B1) = 1.0000`.
- `test_planted_signal_shuffled_labels_near_chance`: same candidates/scores,
  labels shuffled (`random.Random(42)`) to destroy the planted relationship
  -- `AUC(K1) = 0.4833`, within the test's `|AUC - 0.5| < 0.2` tolerance of
  chance, as expected once the signal is destroyed.

These confirm the AUC computation, the split/candidate/label logic, and the
bootstrap machinery behave correctly on data where the ground-truth
relationship is known by construction. They are **not** a measurement of
whether `familiarity` predicts recurrence in the owner's real conversation
history -- that requires a real corpus, and per the pre-registration's
stopping condition, running the harness now on a substitute would not count
as (and must not be reported as) the pre-registered result.

### How to produce the real result later

Run once, on a real `conversations.db`, and append a fresh dated `##
Results` section (do not edit this one):

```
python eval_knowledge_state.py --db conversations.db --out knowledge_state_eval.json
```

(or point `--db` at the path held in `AI_CONVERSATIONS_DB` / wherever the
owner's real export lives). Per the pre-registration, run it exactly once
and record whatever verdict falls out mechanically.

## Results (2026-08-18) -- the pre-registered run, on the real corpus

The corpus became reachable (`conversations.db` in this clone's repo root,
263 conversations / 3,858 messages). Per the pre-registration's stopping
condition the harness was run exactly ONCE, with the frozen formula, the
frozen 70/30 split, the frozen candidate rule, and seed 1234:

```
python eval_knowledge_state.py --db conversations.db --out knowledge_state_eval.json
```

**Observed (full report: `knowledge_state_eval.json`, gitignored):**

| Quantity | Value |
| --- | --- |
| n_conversations | 263 (0 dropped for missing/unparseable `updated_at`) |
| cutoff T | 2026-05-20T18:27:57Z |
| n_candidates | 84 (tokens in >= 2 TRAIN conversations) |
| n_pos / n_neg | 12 / 72 |
| AUC(B0) interest weight | 0.5411 |
| AUC(B1) recency alone | 0.5237 |
| AUC(K1) familiarity | 0.5561 |
| **dAUC = AUC(K1) - AUC(B0)** | **+0.0150** |
| 95% bootstrap CI for dAUC | **[-0.1741, +0.1861]** (2000 resamples, 0 skipped) |
| precision@20 B0 / K1 | 0.15 / 0.10 |

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

Applied mechanically, no softening. The candidate set is large enough
(84 >= 50) and both classes are populated (12 / 72), so this is not the
INCONCLUSIVE branch. It is not SUPPORTED: dAUC is +0.0150, far below the
pre-registered +0.05 threshold. It IS FALSIFIED on the CI clause -- the 95%
bootstrap CI for dAUC spans zero by a wide margin ([-0.17, +0.19], i.e. an
interval roughly 24x the size of the point estimate). `AUC(K1) > AUC(B1)`
does hold (0.5561 vs 0.5237), so the composite is not beaten by recency
alone, but the decision rule is a conjunction and one satisfied clause does
not rescue it.

Read plainly: all three rankings sit within ~0.06 of chance (0.50). On this
corpus, at this split, familiarity does not rank future token recurrence
meaningfully better than a raw TRAIN conversation count, and neither of them
ranks it much better than a coin. The formula stands as pre-registered and
is NOT to be retuned in response to this number (pre-registration, stopping
condition).

**Caveats that are part of the result, not excuses for it:**
- The corpus is ~15% of the owner's real ChatGPT history (242 of ~1,630;
  backfill stalled 2026-08-06). A larger corpus is a different measurement,
  not a re-run of this one -- if the backfill completes, that would be a new
  pre-registration, separately dated.
- Only 12 of 84 candidate tokens are positive, so the AUC is estimated
  against a small positive class; the wide CI is exactly that showing up.
- The LABEL PROXY CAVEAT above applies unchanged.
- Both signals are TITLE-token level. This result is evidence about title
  tokens, not about the owner's interests as such -- which is the specific
  motivation for the content-level extractor added in the same step
  (`interest_extractor.py`), whose candidates are owner-approved offers
  rather than an automatic scoring input.

### Implication mapping now in force

The FALSIFIED branch replaces the 2026-08-10 INCONCLUSIVE -- NO CORPUS
branch. `knowledge_state`'s familiarity has now been measured against its
own pre-registered baseline and did not clear it, so it must not be adopted
as a scoring input by any consumer. Combined with the parallel FALSIFIED
verdict in `FUTURE_SELF_EXPERIMENT.md` (2026-08-18), the standing adoption
gate is not lifted -- it is now closed on measurement rather than open on a
missing corpus. See that doc's implication mapping for the interest-weight
side.
