#!/usr/bin/env python3
"""Offline logic tests for poll_conversations.py (no network, no Chrome, no
real credentials -- exercises the catch-up window math and state persistence
only; the actual fetch cycle is exercised live via a real --once run).
"""
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from poll_conversations import (  # noqa: E402
    compute_after,
    db_max_created_at,
    load_state,
    poll_once,
    save_state,
)
import export_to_sqlite as ets  # noqa: E402

NOW = datetime(2026, 8, 5, 12, 0, 0, tzinfo=timezone.utc)
OVERLAP = timedelta(minutes=2)
BOOTSTRAP_DAYS = 7


class TestComputeAfter(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(ets.SCHEMA)
        self.addCleanup(self.conn.close)

    def insert(self, source, conv_id, created_at):
        self.conn.execute(
            """INSERT INTO raw_conversations
               (source, conversation_id, title, created_at, updated_at, raw_json, raw_source, content_hash)
               VALUES (?, ?, 't', ?, ?, '{}', 'api_json', 'h')""",
            (source, conv_id, created_at, created_at),
        )
        self.conn.commit()

    def test_first_run_no_rows_falls_back_to_bootstrap_window(self):
        after = compute_after(self.conn, "claude", {}, NOW, overlap=OVERLAP, bootstrap_days=BOOTSTRAP_DAYS)
        self.assertEqual(after, NOW - timedelta(days=BOOTSTRAP_DAYS))

    def test_first_run_uses_most_recent_db_conversation_over_bootstrap_window(self):
        recent = (NOW - timedelta(days=1)).isoformat()
        self.insert("claude", "c1", recent)
        self.insert("claude", "c2", (NOW - timedelta(days=3)).isoformat())
        after = compute_after(self.conn, "claude", {}, NOW, overlap=OVERLAP, bootstrap_days=BOOTSTRAP_DAYS)
        self.assertEqual(after, datetime.fromisoformat(recent))

    def test_sources_are_independent(self):
        self.insert("claude", "c1", (NOW - timedelta(days=1)).isoformat())
        # chatgpt has no rows -- should still fall back to bootstrap, unaffected by claude's data
        after_gpt = compute_after(self.conn, "chatgpt", {}, NOW, overlap=OVERLAP, bootstrap_days=BOOTSTRAP_DAYS)
        self.assertEqual(after_gpt, NOW - timedelta(days=BOOTSTRAP_DAYS))

    def test_steady_state_uses_last_success_minus_overlap(self):
        last_success = NOW - timedelta(seconds=90)  # one interval ago, within the overlap window
        state = {"claude": {"last_success_at": last_success.isoformat()}}
        after = compute_after(self.conn, "claude", state, NOW, overlap=OVERLAP, bootstrap_days=BOOTSTRAP_DAYS)
        # overlap (2m) is wider than the 90s gap, so the overlap floor wins
        self.assertEqual(after, NOW - OVERLAP)

    def test_downtime_widens_the_window_instead_of_skipping(self):
        last_success = NOW - timedelta(days=3)  # process was down for 3 days
        state = {"claude": {"last_success_at": last_success.isoformat()}}
        after = compute_after(self.conn, "claude", state, NOW, overlap=OVERLAP, bootstrap_days=BOOTSTRAP_DAYS)
        self.assertEqual(after, last_success)

    def test_db_max_created_at_ignores_other_source(self):
        self.insert("chatgpt", "g1", (NOW - timedelta(days=1)).isoformat())
        self.assertIsNone(db_max_created_at(self.conn, "claude"))


class TestPollOnceRetryOnFailure(unittest.TestCase):
    """A single failed item within an otherwise-successful cycle (e.g. a
    transient 'database is locked' collision with a concurrent manual
    import) must not advance that source's window -- otherwise the failed
    conversation's created_at falls before the next cycle's --after and is
    silently skipped forever. Regression coverage for exactly that bug,
    caught live: a real conversation ('Test convo') hit this during
    development and was dropped until this check was added."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(ets.SCHEMA)
        self.addCleanup(self.conn.close)

    def test_failed_item_does_not_advance_window(self):
        state = {}
        with patch("poll_conversations.ets.run_claude", return_value={"inserted": 0, "failed": 1}), \
             patch("poll_conversations.ets.run_chatgpt", return_value={"inserted": 0, "failed": 0}), \
             patch("poll_conversations.save_state"):
            poll_once(self.conn, state, port=9222, overlap=OVERLAP, bootstrap_days=BOOTSTRAP_DAYS)
        self.assertNotIn("last_success_at", state["claude"])
        self.assertIn("last_success_at", state["chatgpt"])

    def test_clean_cycle_advances_window(self):
        state = {}
        with patch("poll_conversations.ets.run_claude", return_value={"inserted": 2, "failed": 0}), \
             patch("poll_conversations.ets.run_chatgpt", return_value={"inserted": 0, "failed": 0}), \
             patch("poll_conversations.save_state"):
            poll_once(self.conn, state, port=9222, overlap=OVERLAP, bootstrap_days=BOOTSTRAP_DAYS)
        self.assertIn("last_success_at", state["claude"])

    def test_notify_fires_only_when_something_was_inserted(self):
        state = {}
        with patch("poll_conversations.ets.run_claude", return_value={"inserted": 3, "failed": 0}), \
             patch("poll_conversations.ets.run_chatgpt", return_value={"inserted": 0, "failed": 0}), \
             patch("poll_conversations.save_state"), \
             patch("poll_conversations.ets.ntfy_notify") as mock_notify:
            poll_once(self.conn, state, port=9222, overlap=OVERLAP, bootstrap_days=BOOTSTRAP_DAYS)
        self.assertEqual(mock_notify.call_count, 1)
        args, kwargs = mock_notify.call_args
        self.assertIn("claude", args[0])
        self.assertEqual(kwargs.get("priority"), "low")

    def test_system_exit_from_missing_tab_does_not_crash_the_cycle(self):
        state = {}
        with patch("poll_conversations.ets.run_claude", side_effect=SystemExit("no tab")), \
             patch("poll_conversations.ets.run_chatgpt", return_value={"inserted": 0, "failed": 0}), \
             patch("poll_conversations.save_state"):
            poll_once(self.conn, state, port=9222, overlap=OVERLAP, bootstrap_days=BOOTSTRAP_DAYS)  # must not raise
        self.assertNotIn("last_success_at", state["claude"])
        self.assertIn("last_error", state["claude"])


class TestStatePersistence(unittest.TestCase):
    def test_save_then_load_roundtrips(self):
        with tempfile.TemporaryDirectory() as d:
            state_path = Path(d) / "poll_state.json"
            import poll_conversations as pc
            old_path = pc.STATE_PATH
            pc.STATE_PATH = state_path
            try:
                data = {"claude": {"last_success_at": NOW.isoformat(), "last_error": None}}
                save_state(data)
                self.assertTrue(state_path.exists())
                self.assertEqual(load_state(), data)
            finally:
                pc.STATE_PATH = old_path

    def test_load_missing_file_returns_empty_dict(self):
        with tempfile.TemporaryDirectory() as d:
            import poll_conversations as pc
            old_path = pc.STATE_PATH
            pc.STATE_PATH = Path(d) / "does_not_exist.json"
            try:
                self.assertEqual(load_state(), {})
            finally:
                pc.STATE_PATH = old_path

    def test_load_corrupt_file_returns_empty_dict_not_crash(self):
        with tempfile.TemporaryDirectory() as d:
            state_path = Path(d) / "poll_state.json"
            state_path.write_text("{not valid json", encoding="utf-8")
            import poll_conversations as pc
            old_path = pc.STATE_PATH
            pc.STATE_PATH = state_path
            try:
                self.assertEqual(load_state(), {})
            finally:
                pc.STATE_PATH = old_path


if __name__ == "__main__":
    unittest.main()
