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

## Results

_(Not yet run. This section is appended by Task 2, after the section above,
without editing anything above this line.)_
