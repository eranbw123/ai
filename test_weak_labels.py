#!/usr/bin/env python3
"""Offline tests for weak_labels.py -- no network, no real conversations.db.
Builds the deterministic synthetic fixture corpus that weak_labels.py's
--synthetic-corpus CLI path also shares (build_synthetic_corpus), reusing
export_to_sqlite.SCHEMA for the table DDL.
"""
import json
import re
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import weak_labels as wl  # noqa: E402

# Message-body substrings from build_synthetic_corpus's fixtures. None of
# these -- nor any raw_json content -- may ever appear in the serialized
# artifact; only aggregate counts/ids are allowed (see WEAK_LABELS.md's
# not-gold rule).
FIXTURE_MESSAGE_TEXTS = [
    "deep question 0", "deep answer 0", "one question", "one answer",
    "day1 question", "day1 answer", "day3 followup", "day3 answer",
    "please fix my code", "here's a fix",
    "No, that's not what I need, try again", "here's another fix",
    "explain recursion", "explanation attempt 1",
    "explanation attempt 2 (regenerated)", "thanks, that makes sense",
    "widget pipeline notes", "reconstructed question", "reconstructed answer",
    "how do I fix this bug?", "here is the fix", "thanks, now I know what to do",
]


def _by_key(labels):
    return {(r["source"], r["conversation_id"], r["label"]): r for r in labels}


class WeakLabelsTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        wl.build_synthetic_corpus(self.conn)
        self.state = wl.derive(self.conn, db_desc="synthetic fixture")
        self.recs = _by_key(self.state["labels"])

    def tearDown(self):
        self.conn.close()

    # -- top-level shape --------------------------------------------------

    def test_top_level_keys_and_ground_truth(self):
        for key in ("schema_version", "ground_truth", "generated_at",
                    "extractor_version", "corpus", "labels"):
            self.assertIn(key, self.state)
        self.assertIs(self.state["ground_truth"], False)
        self.assertEqual(self.state["schema_version"], 1)

    def test_corpus_header_identifies_synthetic_fixture(self):
        self.assertEqual(self.state["corpus"]["db"], "synthetic fixture")
        self.assertEqual(
            self.state["corpus"]["conversation_count"], len(self.state["labels"]) // 5
        )

    def test_every_record_confidence_below_one(self):
        for rec in self.state["labels"]:
            self.assertLess(rec["confidence"], 1.0, rec)

    # -- depth --------------------------------------------------------------

    def test_depth_fires_on_deep_positive_not_on_quick_control(self):
        deep = self.recs[("claude", "deep-1", "depth")]
        self.assertTrue(deep["fired"])
        self.assertEqual(deep["evidence"]["user_turns"], 4)

        quick = self.recs[("claude", "quick-1", "depth")]
        self.assertFalse(quick["fired"])

    # -- sustained_followup ---------------------------------------------

    def test_sustained_followup_fires_on_days_apart_positive(self):
        rec = self.recs[("claude", "followup-1", "sustained_followup")]
        self.assertTrue(rec["fired"])
        self.assertAlmostEqual(rec["evidence"]["span_hours"], 48.0, places=1)

    def test_sustained_followup_not_fired_control_same_sitting(self):
        rec = self.recs[("claude", "deep-1", "sustained_followup")]
        self.assertIsNotNone(rec["value"])  # covered
        self.assertFalse(rec["fired"])

    def test_sustained_followup_not_covered_without_timestamps(self):
        rec = self.recs[("claude", "no-ts-1", "sustained_followup")]
        self.assertIsNone(rec["value"])
        self.assertFalse(rec["fired"])
        self.assertEqual(rec["confidence"], 0.0)
        self.assertIn("no_message_timestamps", rec["degraded_reasons"])

    # -- rapid_abandonment ------------------------------------------------

    def test_rapid_abandonment_fires_on_single_exchange_not_on_deep(self):
        quick = self.recs[("claude", "quick-1", "rapid_abandonment")]
        self.assertTrue(quick["fired"])

        deep = self.recs[("claude", "deep-1", "rapid_abandonment")]
        self.assertFalse(deep["fired"])

    # -- response_rejection ------------------------------------------------

    def test_response_rejection_fires_on_lexical_cue(self):
        rec = self.recs[("chatgpt", "reject-1", "response_rejection")]
        self.assertTrue(rec["fired"])
        self.assertGreaterEqual(rec["evidence"]["rejection_count"], 1)
        self.assertIn("lexical_cue_english_only", rec["degraded_reasons"])
        # cue ids only, never the message text
        self.assertTrue(all(isinstance(i, int) for i in rec["evidence"]["matched_cue_ids"]))

    def test_response_rejection_fires_on_regenerated_branch(self):
        rec = self.recs[("chatgpt", "regen-1", "response_rejection")]
        self.assertTrue(rec["fired"])
        self.assertEqual(rec["evidence"]["rejection_count"], 0)
        self.assertGreaterEqual(rec["evidence"]["regenerated_count"], 1)

    def test_response_rejection_not_fired_control(self):
        rec = self.recs[("claude", "quick-1", "response_rejection")]
        self.assertFalse(rec["fired"])
        self.assertEqual(rec["evidence"]["rejection_count"], 0)
        self.assertEqual(rec["evidence"]["regenerated_count"], 0)

    def test_response_rejection_ignores_cue_as_bare_substring(self):
        # Regression for the substring-matching bug: "now I know" contains
        # "no" as a bare substring but is not a rejection -- must not match.
        rec = self.recs[("claude", "benign-1", "response_rejection")]
        self.assertFalse(rec["fired"])
        self.assertEqual(rec["evidence"]["rejection_count"], 0)
        self.assertEqual(rec["evidence"]["matched_cue_ids"], [])

    # -- recurrence ---------------------------------------------------------

    def test_recurrence_fires_on_shared_titles_across_days(self):
        rec = self.recs[("claude", "recur-a", "recurrence")]
        self.assertTrue(rec["fired"])
        self.assertGreaterEqual(rec["evidence"]["shared_conversations"], 2)
        self.assertGreaterEqual(rec["evidence"]["distinct_days"], 2)

    def test_recurrence_not_fired_control_solo_title(self):
        rec = self.recs[("claude", "recur-solo", "recurrence")]
        self.assertIsNotNone(rec["value"])  # covered -- has tokens, just no overlap
        self.assertFalse(rec["fired"])

    def test_recurrence_not_covered_with_zero_title_tokens(self):
        rec = self.recs[("claude", "no-title-1", "recurrence")]
        self.assertIsNone(rec["value"])
        self.assertFalse(rec["fired"])
        self.assertEqual(rec["confidence"], 0.0)
        self.assertIn("no_title_tokens", rec["degraded_reasons"])

    # -- markdown_reconstructed downgrade ------------------------------

    def test_markdown_reconstructed_downgrades_confidence(self):
        # md-1 uses the REAL markdown_reconstructed raw_json shape (see
        # migrate_md_to_sqlite.py: {title, messages: [{role, text}]}, no
        # chat_messages/mapping), so get_current_branch() finds nothing and
        # the branch-based labels come back not-covered (empty_branch)
        # rather than covered-and-downgraded. The markdown_reconstructed
        # downgrade still applies on every label regardless.
        base_confidence = {
            "depth": 0.9, "sustained_followup": 0.8, "rapid_abandonment": 0.5,
            "response_rejection": 0.4, "recurrence": 0.6,
        }
        for label in wl.LABEL_ORDER:
            rec = self.recs[("claude", "md-1", label)]
            self.assertIn("markdown_reconstructed", rec["degraded_reasons"], label)
            self.assertLess(rec["confidence"], base_confidence[label], label)

        depth = self.recs[("claude", "md-1", "depth")]
        self.assertIsNone(depth["value"])
        self.assertEqual(depth["confidence"], 0.0)
        self.assertIn("empty_branch", depth["degraded_reasons"])

        # recurrence only needs the title, which is present regardless of
        # raw_json shape, so it's still covered -- just downgraded.
        recurrence = self.recs[("claude", "md-1", "recurrence")]
        self.assertIsNotNone(recurrence["value"])
        self.assertAlmostEqual(recurrence["confidence"], 0.3)  # 0.6 * 0.5

    # -- determinism ---------------------------------------------------------

    def test_determinism_byte_identical_ignoring_generated_at(self):
        with patch("weak_labels._now_iso", return_value="2026-01-01T00:00:00Z"):
            conn1 = sqlite3.connect(":memory:")
            wl.build_synthetic_corpus(conn1)
            state1 = wl.derive(conn1, db_desc="synthetic fixture")
            conn1.close()

            conn2 = sqlite3.connect(":memory:")
            wl.build_synthetic_corpus(conn2)
            state2 = wl.derive(conn2, db_desc="synthetic fixture")
            conn2.close()

        tmp = Path(tempfile.mkdtemp())
        try:
            out1, out2 = tmp / "a.json", tmp / "b.json"
            wl.write(out1, state1)
            wl.write(out2, state2)
            self.assertEqual(out1.read_bytes(), out2.read_bytes())
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    # -- privacy ---------------------------------------------------------

    def test_privacy_no_message_bodies_leak(self):
        blob = json.dumps(self.state, ensure_ascii=False)
        for text in FIXTURE_MESSAGE_TEXTS:
            self.assertNotIn(text, blob, f"message body leaked into artifact: {text!r}")

    # -- report ---------------------------------------------------------

    def test_report_contains_all_labels_and_informative_flags(self):
        report = wl.format_report(self.state)
        self.assertIn("synthetic fixture", report)
        for label in wl.LABEL_ORDER:
            self.assertIn(label, report)

    # -- CLI safety --------------------------------------------------------

    def test_synthetic_corpus_refuses_existing_path(self):
        # --synthetic-corpus writes fixture rows outside upsert() -- must
        # refuse rather than silently inject them into an existing DB file
        # (e.g. a real conversations.db passed by mistake).
        tmp = Path(tempfile.mkdtemp())
        try:
            existing = tmp / "already-here.db"
            existing.write_bytes(b"")
            with patch("sys.argv", ["weak_labels.py", "--synthetic-corpus", str(existing)]):
                with self.assertRaises(SystemExit):
                    wl.main()
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    # -- timestamp robustness ------------------------------------------

    def test_message_timestamps_never_raise_and_normalize_to_utc(self):
        # A non-str Claude created_at (AttributeError inside
        # parse_api_timestamp) and a naive-vs-aware timestamp mix must not
        # raise -- both are defensive paths the plan requires ("must never
        # raise").
        self.assertIsNone(wl._safe_claude_ts(12345))
        self.assertIsNone(wl._safe_claude_ts(None))
        naive = wl._safe_claude_ts("2026-01-01T10:00:00")  # no Z/offset
        self.assertIsNotNone(naive.tzinfo)

    # -- no-downstream-consumer guard ------------------------------------

    def test_no_other_module_references_weak_labels(self):
        repo_root = Path(__file__).resolve().parent
        allowed = {"weak_labels.py", "test_weak_labels.py"}
        offenders = []
        for path in repo_root.glob("*.py"):
            if path.name in allowed:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            if re.search(r"\bweak_labels\b", text):
                offenders.append(path.name)
        self.assertEqual(offenders, [], f"weak_labels referenced outside its own files: {offenders}")


if __name__ == "__main__":
    unittest.main()
