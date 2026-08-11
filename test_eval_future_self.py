#!/usr/bin/env python3
"""Offline tests for eval_future_self.py -- no network, no CDP, no real
conversations.db. Builds throwaway SQLite DBs reusing the real
raw_conversations schema from export_to_sqlite.SCHEMA.
"""
import json
import random
import shutil
import sys
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import eval_future_self as efs  # noqa: E402
import eval_knowledge_state as eks  # noqa: E402
import export_to_sqlite as ets  # noqa: E402
import personal_state as ps  # noqa: E402


class EvalFutureSelfTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.conn = sqlite3.connect(str(self.tmp / "conversations.db"))
        self.conn.executescript(ets.SCHEMA)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _insert(self, source, conversation_id, title, updated_at, conn=None):
        conn = conn or self.conn
        conn.execute(
            "INSERT INTO raw_conversations "
            "(source, conversation_id, title, model, created_at, updated_at, "
            "raw_json, content_hash) VALUES (?,?,?,?,?,?,?,?)",
            (
                source, conversation_id, title, "test-model", updated_at, updated_at,
                json.dumps({"marker": "RAWJSONMARKER", "conversation_id": conversation_id}),
                f"hash-{conversation_id}",
            ),
        )
        conn.commit()

    # -- determinism ----------------------------------------------------------

    def test_determinism(self):
        conn = self._build_planted_corpus()
        try:
            report1 = efs.run_eval(conn, seed=1234)
            report2 = efs.run_eval(conn, seed=1234)
        finally:
            conn.close()

        out1, out2 = self.tmp / "out1.json", self.tmp / "out2.json"
        ps.write(out1, report1)
        ps.write(out2, report2)
        self.assertEqual(out1.read_bytes(), out2.read_bytes())

    # -- leakage guard ----------------------------------------------------------

    def test_leakage_guard_post_t_only_token_excluded(self):
        # A token that appears ONLY in TEST (post-T) rows must never reach
        # the frozen artifact or the candidate pool -- both are built from
        # a TRAIN-only in-memory DB. Verified two ways: (1) directly against
        # the reconstructed candidate pool, using the same internal helpers
        # run_eval uses, and (2) against the serialized report, which must
        # never carry token strings at all regardless.
        conn = sqlite3.connect(":memory:")
        conn.executescript(ets.SCHEMA)
        base = datetime(2025, 1, 1, tzinfo=timezone.utc)
        # 20 TRAIN rows (70%), rest TEST -- round counts so the 70/30 split
        # lands cleanly.
        for i in range(20):
            dt = base + timedelta(days=i)
            self._insert("claude", f"c{i}", f"traintopic{i % 3} alpha",
                         dt.strftime("%Y-%m-%dT%H:%M:%SZ"), conn=conn)
        leak_dt = base + timedelta(days=25)
        self._insert("claude", "leak1", "leakonlytoken here", leak_dt.strftime("%Y-%m-%dT%H:%M:%SZ"), conn=conn)
        self._insert("claude", "leak2", "leakonlytoken there", (leak_dt + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"), conn=conn)

        train_rows, test_rows, _ = eks._load_and_split(conn)
        cutoff_dt = train_rows[-1][0]
        frozen_rows = [(dt, title) for dt, title in train_rows if dt >= cutoff_dt - timedelta(days=180)]
        train_conn = eks._build_train_conn(frozen_rows)
        try:
            candidates = ps.derive(train_conn, window_days=0, max_topics=efs._NO_TRUNCATION, min_conversations=2)["topics"]
        finally:
            train_conn.close()
        candidate_tokens = {c["key"] for c in candidates}
        self.assertNotIn("leakonlytoken", candidate_tokens)
        # Sanity: the leak token really is present in TEST, confirming this
        # is a meaningful negative (not just an absent-by-construction one).
        test_tokens = set()
        for _dt, title in test_rows:
            test_tokens |= ps._tokenize(title)
        self.assertIn("leakonlytoken", test_tokens)

        report = efs.run_eval(conn, seed=1234)
        conn.close()

        blob = json.dumps(report)
        self.assertNotIn("leakonlytoken", blob)

    # -- window anchoring -------------------------------------------------------

    def test_window_anchoring_excludes_pre_train_row_older_than_180d(self):
        conn = sqlite3.connect(":memory:")
        conn.executescript(ets.SCHEMA)
        base = datetime(2025, 6, 1, tzinfo=timezone.utc)
        # 20 recent TRAIN rows within 180 days of T, then TEST rows after.
        for i in range(20):
            dt = base + timedelta(days=i)
            self._insert("claude", f"recent{i}", f"recenttopic{i % 3} word",
                         dt.strftime("%Y-%m-%dT%H:%M:%SZ"), conn=conn)
        for i in range(9):
            dt = base + timedelta(days=25 + i)
            self._insert("claude", f"test{i}", f"testtopic{i % 3} word",
                         dt.strftime("%Y-%m-%dT%H:%M:%SZ"), conn=conn)
        # An old TRAIN row, far more than 180 days before T, carrying a
        # token that appears nowhere else -- must be excluded from the
        # frozen candidate pool even though it is chronologically pre-T.
        old_dt = base - timedelta(days=400)
        self._insert("claude", "old1", "ancienttopic here", old_dt.strftime("%Y-%m-%dT%H:%M:%SZ"), conn=conn)
        self._insert("claude", "old2", "ancienttopic there", (old_dt + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"), conn=conn)

        train_rows, _test_rows, _ = eks._load_and_split(conn)
        cutoff_dt = train_rows[-1][0]
        # "ancienttopic"'s rows are chronologically TRAIN (pre-T) but more
        # than 180 days before T -- confirm they are indeed in train_rows
        # (so this is a real pre-T-but-out-of-window case, not an
        # already-TEST one) before checking they're windowed out.
        train_titles = " ".join(title for _dt, title in train_rows)
        self.assertIn("ancienttopic", train_titles)

        frozen_rows = [(dt, title) for dt, title in train_rows if dt >= cutoff_dt - timedelta(days=180)]
        train_conn = eks._build_train_conn(frozen_rows)
        try:
            candidates = ps.derive(train_conn, window_days=0, max_topics=efs._NO_TRUNCATION, min_conversations=2)["topics"]
        finally:
            train_conn.close()
        candidate_tokens = {c["key"] for c in candidates}
        self.assertNotIn("ancienttopic", candidate_tokens)

        report = efs.run_eval(conn, seed=1234)
        conn.close()
        blob = json.dumps(report)
        self.assertNotIn("ancienttopic", blob)

    # -- privacy ------------------------------------------------------------

    def test_privacy_no_titles_or_ids_leak(self):
        conn = self._build_planted_corpus()
        try:
            report = efs.run_eval(conn, seed=1234)
        finally:
            conn.close()
        blob = json.dumps(report)
        self.assertNotIn("keepertopic", blob)
        self.assertNotIn("droptopic", blob)
        self.assertNotIn("train-", blob)
        self.assertNotIn("RAWJSONMARKER", blob)

    # -- planted-signal harness self-test ------------------------------------
    #
    # These tests validate the HARNESS ONLY (split/candidate/permutation/
    # decision machinery). The corpora below are synthetic and constructed
    # so recurrence is predictable (or destroyed) by design -- they are NOT
    # evidence about H1 or about the real owner's data. The real, single,
    # pre-registered run happens separately per FUTURE_SELF_EXPERIMENT.md.

    def _build_planted_corpus(self):
        conn = sqlite3.connect(":memory:")
        conn.executescript(ets.SCHEMA)
        train_start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        test_start = datetime(2024, 7, 1, tzinfo=timezone.utc)

        cid = 0
        # Exactly 10 "keeper" tokens, one word per title (no shared/generic
        # second word -- that would itself out-count the keeper tokens and
        # crowd them out of the top-10). 5 TRAIN conversations each, all
        # well within 150 days of the eventual cutoff so none get windowed
        # out; count=5 beats every noise token's count=2, so all 10 fill
        # the frozen top-10 exactly.
        for i in range(10):
            for j in range(5):
                cid += 1
                dt = train_start + timedelta(days=j * 15 + i)
                self._insert("claude", f"train-{cid}", f"keepertopic{i}",
                             dt.strftime("%Y-%m-%dT%H:%M:%SZ"), conn=conn)
        # 25 "noise" tokens: 2 TRAIN conversations each, never recurring,
        # also within the window, count=2 so they never outrank a keeper.
        for i in range(25):
            for j in range(2):
                cid += 1
                dt = train_start + timedelta(days=80 + j * 3 + (i % 10))
                self._insert("claude", f"train-{cid}", f"noisetopic{i}",
                             dt.strftime("%Y-%m-%dT%H:%M:%SZ"), conn=conn)
        # 100 TRAIN conversations total (10*5 + 25*2), 43 TEST conversations
        # -- chosen so int(143 * 0.7) == 100, i.e. the 70/30 split boundary
        # lands exactly on the train/test grouping used above. Each TEST
        # conversation's title is exactly one keeper token (all 10 keepers
        # are in the frozen top-10), so every TEST row is a hit by
        # construction.
        for i in range(43):
            cid += 1
            dt = test_start + timedelta(days=i)
            self._insert("claude", f"test-{cid}", f"keepertopic{i % 10}",
                         dt.strftime("%Y-%m-%dT%H:%M:%SZ"), conn=conn)
        return conn

    def test_planted_signal_predictive(self):
        conn = self._build_planted_corpus()
        try:
            report = efs.run_eval(conn, seed=1234)
        finally:
            conn.close()

        self.assertGreaterEqual(report["n_test"], 30)
        self.assertGreaterEqual(report["n_candidates"], 30)
        self.assertIsNotNone(report["p_perm"])
        # The frozen top-10 is built from the 15 keeper topics (each with 3
        # TRAIN conversations vs. noise topics' 2), and keepers recur in
        # every TEST conversation by construction -- INTEREST hit@10 should
        # clearly beat the chance permutation mean.
        self.assertGreater(report["interest_hit_at_10"], report["mean_permuted_hit_at_10"])
        self.assertLess(report["p_perm"], 0.05)

    def test_shuffled_titles_near_chance(self):
        # Same conversation COUNT/shape as the planted corpus, but titles
        # are reassigned so pre-T topic identity carries no relationship to
        # what shows up post-T -- p_perm should NOT clear the significance
        # threshold.
        conn = sqlite3.connect(":memory:")
        conn.executescript(ets.SCHEMA)
        train_start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        test_start = datetime(2024, 6, 1, tzinfo=timezone.utc)
        rng = random.Random(42)

        pool = [f"unrelatedword{i}" for i in range(80)]
        cid = 0
        for i in range(35):
            for j in range(2):
                cid += 1
                dt = train_start + timedelta(days=i + j * 30)
                title = f"{rng.choice(pool)} {rng.choice(pool)}"
                self._insert("claude", f"train-{cid}", title,
                             dt.strftime("%Y-%m-%dT%H:%M:%SZ"), conn=conn)
        for i in range(40):
            cid += 1
            dt = test_start + timedelta(days=i)
            title = f"{rng.choice(pool)} {rng.choice(pool)}"
            self._insert("claude", f"test-{cid}", title,
                         dt.strftime("%Y-%m-%dT%H:%M:%SZ"), conn=conn)

        report = efs.run_eval(conn, seed=1234)
        conn.close()

        self.assertIsNotNone(report["p_perm"])
        self.assertGreaterEqual(report["p_perm"], 0.05)

    # -- mechanical verdict rule ----------------------------------------------

    def test_verdict_inconclusive_small_test_set(self):
        conn = sqlite3.connect(":memory:")
        conn.executescript(ets.SCHEMA)
        base = datetime(2025, 1, 1, tzinfo=timezone.utc)
        for i in range(9):  # tiny corpus -> n_test well under 30
            dt = base + timedelta(days=i)
            self._insert("claude", f"c{i}", f"topic{i % 3} word",
                         dt.strftime("%Y-%m-%dT%H:%M:%SZ"), conn=conn)
        report = efs.run_eval(conn, seed=1234)
        conn.close()
        self.assertEqual(report["verdict"], "INCONCLUSIVE")
        self.assertLess(report["n_test"], 30)

    def test_verdict_supported_reachable(self):
        conn = self._build_planted_corpus()
        try:
            report = efs.run_eval(conn, seed=1234)
        finally:
            conn.close()
        self.assertEqual(report["verdict"], "SUPPORTED")

    def test_verdict_falsified_reachable(self):
        conn = sqlite3.connect(":memory:")
        conn.executescript(ets.SCHEMA)
        train_start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        test_start = datetime(2024, 6, 1, tzinfo=timezone.utc)
        rng = random.Random(7)
        pool = [f"unrelatedword{i}" for i in range(80)]
        cid = 0
        for i in range(35):
            for j in range(2):
                cid += 1
                dt = train_start + timedelta(days=i + j * 30)
                title = f"{rng.choice(pool)} {rng.choice(pool)}"
                self._insert("claude", f"train-{cid}", title,
                             dt.strftime("%Y-%m-%dT%H:%M:%SZ"), conn=conn)
        for i in range(40):
            cid += 1
            dt = test_start + timedelta(days=i)
            title = f"{rng.choice(pool)} {rng.choice(pool)}"
            self._insert("claude", f"test-{cid}", title,
                         dt.strftime("%Y-%m-%dT%H:%M:%SZ"), conn=conn)
        report = efs.run_eval(conn, seed=1234)
        conn.close()
        self.assertEqual(report["verdict"], "FALSIFIED")

    def test_verdict_inconclusive_no_corpus(self):
        missing_db = str(self.tmp / "does-not-exist.db")
        db_path, checked = efs._locate_db(missing_db)
        self.assertIsNone(db_path)
        self.assertIn(missing_db, checked)
        report = efs._no_corpus_report(checked, seed=1234)
        self.assertEqual(report["verdict"], "INCONCLUSIVE — NO CORPUS AVAILABLE")


if __name__ == "__main__":
    unittest.main()
