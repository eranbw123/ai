"""Frozen replay-evaluation harness testing whether personal_state.py's
frozen top-10 interest topic set at a historical time T predicts the topics
of conversations that happen AFTER T.

This implements EXACTLY the protocol pre-registered in
FUTURE_SELF_EXPERIMENT.md's "Pre-registration (frozen)" section, written and
committed before any evaluation number was observed. Per that section's
stopping condition: run this ONCE against the real corpus on the frozen
split with the frozen protocol. Do not re-run with a changed K, seed,
window, split fraction, or threshold after seeing a result.

Distinct from eval_knowledge_state.py: that harness ranks TOKENS by
familiarity-vs-interest at the token level (AUC over "does this token still
appear in TEST"). This harness evaluates the personal_state ARTIFACT's
top-10 interest set at the CONVERSATION level -- does each post-T
conversation's title intersect that frozen top-10.

Protocol:
  1. Load all raw_conversations rows and split 70/30 by updated_at, exactly
     as eval_knowledge_state._load_and_split (reused here, not duplicated
     -- both helpers are generic over title/updated_at, nothing
     knowledge-state-specific about them). T = updated_at of the last TRAIN
     row.
  2. Frozen-state construction: TRAIN rows are filtered to
     [T - 180 days, T] (the production 180-day window, anchored at T rather
     than at wall-clock "now"), inserted into an in-memory
     raw_conversations DB, and personal_state.derive() is called on that DB
     alone with window_days=0 (the window filter was already applied by
     the pre-filter step) and max_topics large enough to disable
     truncation -- that full, untruncated topic list IS the candidate
     pool; its first 10 entries (already weight-desc, token-asc, per
     derive()'s own sort) are the frozen INTEREST top-10.
  3. B1 (recency baseline): top-10 candidate-pool tokens ranked by most
     recent last_seen (ties broken token-ascending).
  4. B0 (chance baseline): 2000 permuted resamples (random.Random(seed)),
     each drawing 10 tokens uniformly without replacement from the
     candidate pool; hit@10 computed against the same TEST conversations.
  5. PRIMARY METRIC: hit@10 rate over TEST conversations for INTEREST and
     RECENCY; p_perm = (# permuted resamples with rate >= observed INTEREST
     rate, + 1) / (2000 + 1); effect size = INTEREST rate - mean permuted
     rate.
  6. Emit a JSON report with ONLY aggregate numbers -- no token strings, no
     conversation_ids, no titles, no raw_json content.
"""
import argparse
import os
import random
import sqlite3
from datetime import timedelta, timezone
from pathlib import Path

import eval_knowledge_state as eks  # reuse _load_and_split / _build_train_conn -- see module docstring
import personal_state as ps

N_RESAMPLES = 2000
TOP_K = 10
FROZEN_WINDOW_DAYS = 180  # matches personal_state.derive()'s production default

# Effectively "no truncation" -- the frozen protocol requires the FULL
# candidate pool (every token with >= 2 TRAIN conversations in the window),
# not just personal_state's default top-50.
_NO_TRUNCATION = 10 ** 9


def _last_seen_sort_key(topic):
    """Most-recent-first, ties broken token-ascending. Topics with no
    last_seen (nullable -- see personal_state.py) sort last."""
    dt = ps._parse_ts(topic["last_seen"]) if topic["last_seen"] else None
    return (-dt.timestamp() if dt is not None else float("inf"), topic["key"])


def _decide(n_test, n_candidates, n_frozen_topics, p_perm, interest_rate, recency_rate):
    """Mechanical application of the pre-registered decision rule -- see
    FUTURE_SELF_EXPERIMENT.md. n_candidates and n_frozen_topics are the same
    quantity in this implementation (the candidate pool IS the untruncated
    frozen artifact), but both conditions are checked explicitly to mirror
    the pre-registered text verbatim."""
    if n_test < 30 or n_candidates < 30 or n_frozen_topics < 10:
        return "INCONCLUSIVE"
    if p_perm is not None and p_perm < 0.05 and interest_rate >= recency_rate:
        return "SUPPORTED"
    return "FALSIFIED"


def run_eval(conn, *, seed=1234):
    train_rows, test_rows, n_dropped = eks._load_and_split(conn)
    n_train = len(train_rows)
    n_test = len(test_rows)

    cutoff_dt = train_rows[-1][0] if train_rows else None
    cutoff_iso = (
        cutoff_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if cutoff_dt is not None else None
    )

    frozen_rows = []
    if cutoff_dt is not None:
        window_start = cutoff_dt - timedelta(days=FROZEN_WINDOW_DAYS)
        frozen_rows = [(dt, title) for dt, title in train_rows if dt >= window_start]

    if frozen_rows:
        train_conn = eks._build_train_conn(frozen_rows)
        try:
            candidates = ps.derive(
                train_conn, window_days=0,
                max_topics=_NO_TRUNCATION, min_conversations=2,
            )["topics"]
        finally:
            train_conn.close()
    else:
        candidates = []

    candidate_tokens = [c["key"] for c in candidates]
    n_candidates = len(candidate_tokens)
    n_frozen_topics = len(candidates)

    interest_top10_set = set(c["key"] for c in candidates[:TOP_K])
    recency_top10_set = set(
        c["key"] for c in sorted(candidates, key=_last_seen_sort_key)[:TOP_K]
    )

    test_token_sets = [ps._tokenize(title) for _dt, title in test_rows]

    def _hit_rate(topic_set):
        if not test_token_sets:
            return None
        hits = sum(1 for s in test_token_sets if s & topic_set)
        return hits / len(test_token_sets)

    interest_hit_rate = _hit_rate(interest_top10_set)
    recency_hit_rate = _hit_rate(recency_top10_set)

    p_perm = None
    mean_permuted_hit_rate = None
    effect_size = None
    if test_token_sets and n_candidates >= TOP_K:
        rng = random.Random(seed)
        permuted_rates = []
        n_ge = 0
        for _ in range(N_RESAMPLES):
            sample_set = set(rng.sample(candidate_tokens, TOP_K))
            hits = sum(1 for s in test_token_sets if s & sample_set)
            rate = hits / len(test_token_sets)
            permuted_rates.append(rate)
            if rate >= interest_hit_rate:
                n_ge += 1
        p_perm = (n_ge + 1) / (N_RESAMPLES + 1)
        mean_permuted_hit_rate = sum(permuted_rates) / len(permuted_rates)
        effect_size = interest_hit_rate - mean_permuted_hit_rate

    verdict = _decide(
        n_test, n_candidates, n_frozen_topics, p_perm, interest_hit_rate, recency_hit_rate,
    )

    return {
        "n_train": n_train,
        "n_test": n_test,
        "n_dropped_rows": n_dropped,
        "cutoff": cutoff_iso,
        "n_candidates": n_candidates,
        "interest_hit_at_10": interest_hit_rate,
        "recency_hit_at_10": recency_hit_rate,
        "mean_permuted_hit_at_10": mean_permuted_hit_rate,
        "p_perm": p_perm,
        "effect_size": effect_size,
        "n_resamples": N_RESAMPLES,
        "seed": seed,
        "verdict": verdict,
    }


def _locate_db(cli_db):
    """Returns (path, checked_paths) for the first of --db / AI_CONVERSATIONS_DB
    that exists on disk, or (None, checked_paths) if neither does."""
    checked = [str(Path(cli_db))]
    if Path(cli_db).exists():
        return cli_db, checked
    env_db = os.environ.get("AI_CONVERSATIONS_DB")
    if env_db:
        checked.append(env_db)
        if Path(env_db).exists():
            return env_db, checked
    return None, checked


def _no_corpus_report(checked_paths, seed):
    return {
        "n_train": None,
        "n_test": None,
        "n_dropped_rows": None,
        "cutoff": None,
        "n_candidates": None,
        "interest_hit_at_10": None,
        "recency_hit_at_10": None,
        "mean_permuted_hit_at_10": None,
        "p_perm": None,
        "effect_size": None,
        "n_resamples": N_RESAMPLES,
        "seed": seed,
        "verdict": "INCONCLUSIVE — NO CORPUS AVAILABLE",
        "checked_paths": checked_paths,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="conversations.db")
    parser.add_argument("--out", default="future_self_eval.json")
    parser.add_argument("--seed", type=int, default=1234)
    args = parser.parse_args()

    db_path, checked = _locate_db(args.db)
    if db_path is None:
        report = _no_corpus_report(checked, args.seed)
        ps.write(args.out, report)
        print(f"future_self eval: NO CORPUS AVAILABLE (checked {checked}) -> {args.out}")
        return

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        report = run_eval(conn, seed=args.seed)
    finally:
        conn.close()

    ps.write(args.out, report)
    print(
        f"future_self eval: n_test={report['n_test']} "
        f"interest_hit@10={report['interest_hit_at_10']} "
        f"verdict={report['verdict']} -> {args.out}"
    )


if __name__ == "__main__":
    main()
