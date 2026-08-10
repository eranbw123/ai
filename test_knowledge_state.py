#!/usr/bin/env python3
"""Offline tests for knowledge_state.py and eval_knowledge_state.py -- no
network, no CDP, no real conversations.db. Builds throwaway SQLite DBs
reusing the real raw_conversations schema from export_to_sqlite.SCHEMA.
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
import export_to_sqlite as ets  # noqa: E402
import eval_knowledge_state as eks  # noqa: E402
import knowledge_state as ks  # noqa: E402
import personal_state as ps  # noqa: E402


class KnowledgeStateTests(unittest.TestCase):
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

    # -- derive(): determinism ------------------------------------------------

    def test_determinism(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self._insert("claude", "c1", "Zeta apples bananas", "2025-06-01T00:00:00Z")
        self._insert("claude", "c2", "Alpha apples bananas", "2025-07-01T00:00:00Z")

        topics1 = ks.derive(self.conn, now=now, min_conversations=2)
        topics2 = ks.derive(self.conn, now=now, min_conversations=2)

        out1, out2 = self.tmp / "out1.json", self.tmp / "out2.json"
        ps.write(out1, topics1)
        ps.write(out2, topics2)
        self.assertEqual(out1.read_bytes(), out2.read_bytes())

    # -- derive(): privacy ------------------------------------------------------

    def test_privacy_no_raw_identifiers_leak(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        title = "My Private Medical Diagnosis Topic"
        self._insert("claude", "super-secret-conversation-id-42", title, "2025-06-01T00:00:00Z")
        self._insert("claude", "c2", title, "2025-06-02T00:00:00Z")
        topics = ks.derive(self.conn, now=now, min_conversations=2)
        blob = json.dumps(topics)
        self.assertNotIn("super-secret-conversation-id-42", blob)
        self.assertNotIn(title.lower(), blob)
        self.assertNotIn("private medical", blob)
        self.assertNotIn("RAWJSONMARKER", blob)

    # -- derive(): the burst-vs-spread discrimination is the whole point --------

    def test_burst_vs_spread_discrimination(self):
        # Same conversation count (2) and same last_seen (so exposure and
        # decay are identical) for both tokens -- the ONLY difference is
        # whether the two conversations landed in the same month (burst) or
        # different months (spread). If familiarity didn't distinguish
        # these, it would be redundant with personal_state's frequency-only
        # weight -- see knowledge_state.py's module docstring.
        self._insert("claude", "c1", "burstword alpha", "2025-01-05T00:00:00Z")
        self._insert("claude", "c2", "burstword beta", "2025-01-20T00:00:00Z")  # same month
        self._insert("claude", "c3", "spreadword alpha", "2024-08-05T00:00:00Z")
        self._insert("claude", "c4", "spreadword beta", "2025-01-20T00:00:00Z")  # different month, same last_seen

        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        topics = {t["key"]: t for t in ks.derive(self.conn, now=now, min_conversations=2)}

        burst, spread = topics["burstword"], topics["spreadword"]
        self.assertEqual(burst["conversations"], spread["conversations"])
        self.assertEqual(burst["last_seen"], spread["last_seen"])
        self.assertEqual(burst["active_months"], 1)
        self.assertEqual(spread["active_months"], 2)
        self.assertGreater(spread["familiarity"], burst["familiarity"])

    # -- derive(): null updated_at ------------------------------------------

    def test_null_updated_at_yields_null_fields(self):
        self._insert("claude", "c1", "Nullish gadgets alpha", None)
        self._insert("claude", "c2", "Nullish gadgets beta", None)

        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        topics = {t["key"]: t for t in ks.derive(self.conn, now=now, min_conversations=2)}
        topic = topics["nullish"]
        self.assertIsNone(topic["first_seen"])
        self.assertIsNone(topic["last_seen"])
        self.assertIsNone(topic["recency_days"])
        self.assertEqual(topic["span_days"], 0)
        self.assertEqual(topic["active_months"], 0)
        self.assertEqual(topic["familiarity"], 0.0)

        # Must round-trip through write() as JSON null without error.
        out = self.tmp / "null.json"
        ps.write(out, [topic])
        loaded = json.loads(out.read_text(encoding="utf-8"))[0]
        self.assertIsNone(loaded["last_seen"])
        self.assertIsNone(loaded["recency_days"])

    # -- eval_knowledge_state._auc(): hand-computed example with a tie ------

    def test_auc_hand_computed_with_tie(self):
        # scores [1, 2, 2, 3], labels [0, 1, 0, 1] (index-aligned).
        # Tie at score 2 (indices 1, 2) -> average rank (2+3)/2 = 2.5.
        # ranks: idx0=1, idx1=2.5, idx2=2.5, idx3=4
        # n_pos=2 (idx1, idx3), n_neg=2 (idx0, idx2)
        # rank_sum_pos = 2.5 + 4 = 6.5
        # AUC = (6.5 - 2*3/2) / (2*2) = 3.5/4 = 0.875
        # Cross-checked by hand via pairwise Mann-Whitney U:
        # pos=2 vs neg=1 -> win(1); pos=2 vs neg=2 -> tie(0.5);
        # pos=3 vs neg=1 -> win(1); pos=3 vs neg=2 -> win(1); U=3.5/4=0.875.
        scores = [1, 2, 2, 3]
        labels = [0, 1, 0, 1]
        self.assertAlmostEqual(eks._auc(scores, labels), 0.875)

    def test_auc_degenerate_returns_none(self):
        self.assertIsNone(eks._auc([1, 2, 3], [1, 1, 1]))
        self.assertIsNone(eks._auc([1, 2, 3], [0, 0, 0]))

    # -- eval_knowledge_state._load_and_split(): 70/30 boundary -------------

    def test_70_30_split_boundary(self):
        conn = sqlite3.connect(":memory:")
        conn.executescript(ets.SCHEMA)
        base = datetime(2025, 1, 1, tzinfo=timezone.utc)
        for i in range(10):
            dt = (base + timedelta(days=i)).strftime("%Y-%m-%dT%H:%M:%SZ")
            self._insert("claude", f"c{i}", f"topic{i} word", dt, conn=conn)

        train_rows, test_rows, n_dropped = eks._load_and_split(conn)
        conn.close()

        self.assertEqual(n_dropped, 0)
        self.assertEqual(len(train_rows), 7)  # int(10 * 0.7)
        self.assertEqual(len(test_rows), 3)
        self.assertEqual(train_rows[-1][0], base + timedelta(days=6))

    def test_split_drops_unparseable_updated_at(self):
        conn = sqlite3.connect(":memory:")
        conn.executescript(ets.SCHEMA)
        self._insert("claude", "c1", "topic alpha", "2025-01-01T00:00:00Z", conn=conn)
        self._insert("claude", "c2", "topic beta", None, conn=conn)
        self._insert("claude", "c3", "topic gamma", "not-a-timestamp", conn=conn)

        train_rows, test_rows, n_dropped = eks._load_and_split(conn)
        conn.close()

        self.assertEqual(n_dropped, 2)
        self.assertEqual(len(train_rows) + len(test_rows), 1)

    # -- eval_knowledge_state.run_eval(): planted-signal harness self-test --
    #
    # This test validates the HARNESS ONLY (the split/candidate/label/AUC/
    # bootstrap machinery). The corpus below is synthetic and constructed so
    # recurrence is predictable by design -- it is NOT evidence about H1 or
    # about the real owner's data. That real, single, pre-registered run
    # happens separately per KNOWLEDGE_STATE_EXPERIMENT.md.

    def _build_planted_corpus(self):
        conn = sqlite3.connect(":memory:")
        conn.executescript(ets.SCHEMA)

        train_start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        test_start = datetime(2024, 7, 1, tzinfo=timezone.utc)

        # 30 "keeper" tokens: 2 TRAIN conversations (~2 months apart, so
        # they get spread) + 1 TEST conversation each -> y=1, by
        # construction more familiar (spread + recent) than droppers.
        cid = 0
        for i in range(30):
            dt_a = train_start + timedelta(days=(i * 3) % 87)
            dt_b = dt_a + timedelta(days=60)
            dt_test = test_start + timedelta(days=i)
            for dt in (dt_a, dt_b):
                cid += 1
                self._insert("claude", f"c{cid}", f"keepertok{i}",
                             dt.strftime("%Y-%m-%dT%H:%M:%SZ"), conn=conn)
            cid += 1
            self._insert("claude", f"c{cid}", f"keepertok{i}",
                         dt_test.strftime("%Y-%m-%dT%H:%M:%SZ"), conn=conn)

        # 5 "dropped" tokens: 2 TRAIN conversations, same day (burst),
        # early in the train window -> y=0, never recur into TEST.
        for j in range(5):
            dt = train_start + timedelta(days=14 + j)
            for hour in (1, 13):
                cid += 1
                self._insert("claude", f"c{cid}", f"droptok{j}",
                             dt.replace(hour=hour).strftime("%Y-%m-%dT%H:%M:%SZ"), conn=conn)

        return conn

    def test_planted_signal_predictive(self):
        conn = self._build_planted_corpus()
        try:
            report = eks.run_eval(conn, seed=1234)
        finally:
            conn.close()

        self.assertEqual(report["n_conversations"], 100)
        self.assertEqual(report["n_dropped_rows"], 0)
        self.assertEqual(report["n_candidates"], 35)
        self.assertEqual(report["n_pos"], 30)
        self.assertEqual(report["n_neg"], 5)
        # K1 (familiarity) must clearly separate the two planted groups.
        self.assertGreater(report["auc_k1"], 0.75)
        # B0 (train count) is 2 for every planted token by construction, so
        # it carries no signal -- a fully-tied ranking's tie-corrected AUC
        # is exactly 0.5, which also doubles as a realistic-scale check on
        # the tie-correction logic beyond the tiny hand-computed example.
        self.assertEqual(report["auc_b0"], 0.5)

    def test_planted_signal_shuffled_labels_near_chance(self):
        # Same candidates/scores as the predictive test, but with labels
        # shuffled -- breaking the constructed relationship between
        # familiarity and recurrence. AUC should land near 0.5. Reuses the
        # harness's own internals rather than re-deriving candidates, since
        # this is still purely a harness self-test.
        conn = self._build_planted_corpus()
        try:
            train_rows, test_rows, _ = eks._load_and_split(conn)
            train_conn = eks._build_train_conn(train_rows)
            try:
                candidates = ks.derive(
                    train_conn, now=train_rows[-1][0], window_days=0,
                    max_topics=eks._NO_TRUNCATION, min_conversations=2,
                )
            finally:
                train_conn.close()
        finally:
            conn.close()

        test_token_set = set()
        for _dt, title in test_rows:
            test_token_set |= ps._tokenize(title)
        labels = [1 if c["key"] in test_token_set else 0 for c in candidates]
        scores_k1 = [c["familiarity"] for c in candidates]

        rng = random.Random(42)
        shuffled = labels[:]
        rng.shuffle(shuffled)
        auc_shuffled = eks._auc(scores_k1, shuffled)

        self.assertLess(abs(auc_shuffled - 0.5), 0.2)


if __name__ == "__main__":
    unittest.main()
