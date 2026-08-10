"""Frozen replay-evaluation harness for knowledge_state.py's `familiarity`
score against step-02's interest weight (personal_state.py).

This implements EXACTLY the protocol pre-registered in
KNOWLEDGE_STATE_EXPERIMENT.md's "Pre-registration (frozen)" section, written
and committed before any evaluation number was observed. Per that section's
stopping condition: run this ONCE against the real corpus on the frozen
split with the frozen formula. Do not re-run with a changed formula, split,
candidate rule, or metric after seeing a result -- if you want to explore
variants, split TRAIN internally (see the doc) and label any such number
explicitly as dev-split exploration that does not touch this result.

Protocol:
  1. Load all raw_conversations rows (window_days=0, i.e. all history). Drop
     rows with a missing/unparseable updated_at; count and report them.
  2. Sort remaining conversations by updated_at ascending. TRAIN = first 70%,
     TEST = last 30%. cutoff = updated_at of the last TRAIN conversation.
  3. CANDIDATE SET = every token appearing in >= 2 distinct TRAIN
     conversations -- computed from TRAIN only, never filtered using TEST.
  4. LABEL: y=1 if the token appears in >= 1 TEST conversation ("still live"),
     else y=0 ("settled/dropped"). This is a PROXY for "already known" -- a
     topic can drop out of TEST because it was learned, or simply because
     interest moved on; the metric cannot tell those apart.
  5. Three rankings over the same candidate set, computed from TRAIN only
     with now=cutoff:
       B0 (baseline, == step-02 interest): TRAIN conversation count.
       B1 (ablation baseline): -recency_days (recency alone).
       K1 (candidate): knowledge_state familiarity.
  6. PRIMARY METRIC: tie-corrected Mann-Whitney ROC-AUC for each ranking.
  7. SECONDARY: precision@20 for B0 and K1; dAUC = AUC(K1) - AUC(B0)
     (AUC(K1) - AUC(B1) is derivable from the three reported AUCs).
  8. UNCERTAINTY: paired bootstrap over candidate topics, 2000 resamples,
     random.Random(seed), reporting the 95% percentile CI for dAUC and the
     count of skipped (degenerate, all-one-label) resamples.
  9. Emit a JSON report with ONLY aggregate numbers -- no token strings.
"""
import argparse
import math
import random
import sqlite3
from datetime import timezone

import export_to_sqlite as ets
import knowledge_state as ks
import personal_state as ps

TRAIN_FRACTION = 0.7
N_RESAMPLES = 2000
PRECISION_K = 20

# Effectively "no truncation" when calling knowledge_state.derive() for the
# candidate set -- the frozen protocol requires EVERY token with >= 2 TRAIN
# conversations, not just the top max_topics.
_NO_TRUNCATION = 10 ** 9


def _auc(scores, labels):
    """Tie-corrected Mann-Whitney AUC:
    AUC = (sum of average-ranks of positives - n_pos*(n_pos+1)/2) / (n_pos*n_neg)
    Returns None if either class is empty (AUC undefined).
    """
    n = len(scores)
    order = sorted(range(n), key=lambda i: scores[i])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and scores[order[j + 1]] == scores[order[i]]:
            j += 1
        avg_rank = (i + 1 + j + 1) / 2.0  # 1-based rank, averaged over the tie block
        for k in range(i, j + 1):
            ranks[order[k]] = avg_rank
        i = j + 1

    n_pos = sum(1 for y in labels if y == 1)
    n_neg = n - n_pos
    if n_pos == 0 or n_neg == 0:
        return None
    rank_sum_pos = sum(r for r, y in zip(ranks, labels) if y == 1)
    return (rank_sum_pos - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg)


def _precision_at_k(scores, labels, k):
    n = len(scores)
    k = min(k, n)
    if k == 0:
        return 0.0
    order = sorted(range(n), key=lambda i: -scores[i])
    top = order[:k]
    return sum(1 for i in top if labels[i] == 1) / k


def _percentile(sorted_vals, p):
    if not sorted_vals:
        return None
    n = len(sorted_vals)
    idx = p / 100.0 * (n - 1)
    lo, hi = math.floor(idx), math.ceil(idx)
    if lo == hi:
        return sorted_vals[int(idx)]
    frac = idx - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


def _load_and_split(conn):
    """Load all rows, drop unparseable updated_at, sort by updated_at
    ascending, split 70/30. Returns (train_rows, test_rows, n_dropped) where
    each row is (dt, title)."""
    rows = conn.execute("SELECT title, updated_at FROM raw_conversations").fetchall()
    parsed = []
    n_dropped = 0
    for title, updated_at in rows:
        dt = ps._parse_ts(updated_at)
        if dt is None:
            n_dropped += 1
            continue
        parsed.append((dt, title))
    parsed.sort(key=lambda r: r[0])

    train_n = int(len(parsed) * TRAIN_FRACTION)
    train_rows = parsed[:train_n]
    test_rows = parsed[train_n:]
    return train_rows, test_rows, n_dropped


def _build_train_conn(train_rows):
    """An in-memory raw_conversations DB containing only TRAIN rows, so
    knowledge_state.derive() (which reads via SQL) can be reused verbatim to
    compute the candidate set and its familiarity/recency/count fields,
    instead of duplicating its tokenization/aggregation logic here."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(ets.SCHEMA)
    for i, (dt, title) in enumerate(train_rows):
        iso = dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn.execute(
            "INSERT INTO raw_conversations "
            "(source, conversation_id, title, model, created_at, updated_at, "
            "raw_json, content_hash) VALUES (?,?,?,?,?,?,?,?)",
            ("claude", f"train-{i}", title, "eval", iso, iso, "{}", f"h{i}"),
        )
    conn.commit()
    return conn


def run_eval(conn, *, seed=1234):
    train_rows, test_rows, n_dropped = _load_and_split(conn)
    n_conversations = len(train_rows) + len(test_rows)

    cutoff_dt = train_rows[-1][0] if train_rows else None
    cutoff_iso = (
        cutoff_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if cutoff_dt is not None else None
    )

    test_token_set = set()
    for _dt, title in test_rows:
        test_token_set |= ps._tokenize(title)

    if train_rows:
        train_conn = _build_train_conn(train_rows)
        try:
            candidates = ks.derive(
                train_conn, now=cutoff_dt, window_days=0,
                max_topics=_NO_TRUNCATION, min_conversations=2,
            )
        finally:
            train_conn.close()
    else:
        candidates = []

    n_candidates = len(candidates)
    labels = [1 if c["key"] in test_token_set else 0 for c in candidates]
    n_pos = sum(labels)
    n_neg = n_candidates - n_pos

    scores_b0 = [c["conversations"] for c in candidates]
    scores_b1 = [
        -c["recency_days"] if c["recency_days"] is not None else float("-inf")
        for c in candidates
    ]
    scores_k1 = [c["familiarity"] for c in candidates]

    auc_b0 = _auc(scores_b0, labels)
    auc_b1 = _auc(scores_b1, labels)
    auc_k1 = _auc(scores_k1, labels)

    precision_b0 = _precision_at_k(scores_b0, labels, PRECISION_K)
    precision_k1 = _precision_at_k(scores_k1, labels, PRECISION_K)

    dauc = (auc_k1 - auc_b0) if (auc_k1 is not None and auc_b0 is not None) else None

    rng = random.Random(seed)
    dauc_samples = []
    n_skipped = 0
    for _ in range(N_RESAMPLES):
        idx = [rng.randrange(n_candidates) for _ in range(n_candidates)] if n_candidates else []
        rl = [labels[i] for i in idx]
        if not rl or sum(rl) == 0 or sum(rl) == len(rl):
            n_skipped += 1
            continue
        r_b0 = [scores_b0[i] for i in idx]
        r_b1 = [scores_b1[i] for i in idx]
        r_k1 = [scores_k1[i] for i in idx]
        a_b0 = _auc(r_b0, rl)
        _a_b1 = _auc(r_b1, rl)  # recomputed per spec step 8; not itself reported
        a_k1 = _auc(r_k1, rl)
        if a_b0 is None or a_k1 is None:
            n_skipped += 1
            continue
        dauc_samples.append(a_k1 - a_b0)

    dauc_samples.sort()
    ci_lo = _percentile(dauc_samples, 2.5)
    ci_hi = _percentile(dauc_samples, 97.5)

    return {
        "n_conversations": n_conversations,
        "n_dropped_rows": n_dropped,
        "cutoff": cutoff_iso,
        "n_candidates": n_candidates,
        "n_pos": n_pos,
        "n_neg": n_neg,
        "auc_b0": auc_b0,
        "auc_b1": auc_b1,
        "auc_k1": auc_k1,
        "precision_at_20_b0": precision_b0,
        "precision_at_20_k1": precision_k1,
        "dauc": dauc,
        "dauc_ci95": [ci_lo, ci_hi],
        "n_skipped_resamples": n_skipped,
        "seed": seed,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="conversations.db")
    parser.add_argument("--out", default="knowledge_state_eval.json")
    parser.add_argument("--seed", type=int, default=1234)
    args = parser.parse_args()

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    try:
        report = run_eval(conn, seed=args.seed)
    finally:
        conn.close()

    ps.write(args.out, report)
    print(
        f"knowledge_state eval: n={report['n_conversations']} "
        f"candidates={report['n_candidates']} dAUC={report['dauc']} -> {args.out}"
    )


if __name__ == "__main__":
    main()
