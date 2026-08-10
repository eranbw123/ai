#!/usr/bin/env python3
"""Offline tests for personal_state.py -- no network, no CDP, no real
conversations.db. Builds a throwaway SQLite DB reusing the real
raw_conversations schema from export_to_sqlite.SCHEMA.
"""
import json
import shutil
import sys
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import export_to_sqlite as ets  # noqa: E402
import personal_state as ps  # noqa: E402


class PersonalStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.conn = sqlite3.connect(str(self.tmp / "conversations.db"))
        self.conn.executescript(ets.SCHEMA)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _insert(self, source, conversation_id, title, created_at, updated_at):
        self.conn.execute(
            "INSERT INTO raw_conversations "
            "(source, conversation_id, title, model, created_at, updated_at, "
            "raw_json, content_hash) VALUES (?,?,?,?,?,?,?,?)",
            (
                source, conversation_id, title, "test-model", created_at, updated_at,
                json.dumps({"marker": "RAWJSONMARKER", "conversation_id": conversation_id}),
                f"hash-{conversation_id}",
            ),
        )
        self.conn.commit()

    def test_required_top_level_keys(self):
        now = datetime.now(timezone.utc).isoformat()
        self._insert("claude", "c1", "Debugging the export pipeline", now, now)
        state = ps.derive(self.conn)
        for key in ("contract_version", "generated_at", "window_days",
                    "conversation_count", "sources", "topics"):
            self.assertIn(key, state)
        self.assertEqual(state["contract_version"], 1)

    @patch("personal_state._now_iso", return_value="2026-01-01T00:00:00Z")
    def test_determinism_and_tie_break(self, _mock_now):
        now = datetime.now(timezone.utc).isoformat()
        # 'apples' and 'bananas' each occur in the same 2 distinct
        # conversations -> tie on count, must break by key asc.
        self._insert("claude", "c1", "Zeta apples bananas", now, now)
        self._insert("claude", "c2", "Alpha apples bananas", now, now)

        state1 = ps.derive(self.conn, min_conversations=2)
        state2 = ps.derive(self.conn, min_conversations=2)

        out1, out2 = self.tmp / "out1.json", self.tmp / "out2.json"
        ps.write(out1, state1)
        ps.write(out2, state2)
        self.assertEqual(out1.read_bytes(), out2.read_bytes())

        keys = [t["key"] for t in state1["topics"]]
        self.assertEqual(keys, ["apples", "bananas"])

    def test_min_conversations_and_max_topics(self):
        now = datetime.now(timezone.utc).isoformat()
        self._insert("claude", "c1", "Common rareonly topic alpha", now, now)
        self._insert("claude", "c2", "Common topic beta", now, now)
        self._insert("chatgpt", "c3", "Common topic gamma", now, now)

        state = ps.derive(self.conn, min_conversations=2)
        keys = {t["key"] for t in state["topics"]}
        self.assertIn("common", keys)
        self.assertIn("topic", keys)
        self.assertNotIn("rareonly", keys)  # only 1 conversation, below min_conversations

        state_trunc = ps.derive(self.conn, min_conversations=1, max_topics=1)
        self.assertEqual(len(state_trunc["topics"]), 1)

    def test_window_filters_on_updated_at_not_created_at(self):
        now = datetime.now(timezone.utc)
        recent = now - timedelta(days=1)
        old = now - timedelta(days=400)
        # created_at old, updated_at recent -> included (edited recently)
        self._insert("claude", "c1", "Recentlyeditedxyz topic", old.isoformat(), recent.isoformat())
        # created_at recent, updated_at old -> excluded (stale despite recent created_at)
        self._insert("claude", "c2", "Stalecreatedxyz topic", recent.isoformat(), old.isoformat())

        state = ps.derive(self.conn, window_days=30, min_conversations=1)
        keys = {t["key"] for t in state["topics"]}
        self.assertIn("recentlyeditedxyz", keys)
        self.assertNotIn("stalecreatedxyz", keys)
        self.assertEqual(state["conversation_count"], 1)

    def test_privacy_no_raw_identifiers_leak(self):
        now = datetime.now(timezone.utc).isoformat()
        self._insert("claude", "super-secret-conversation-id-42",
                      "My Private Medical Diagnosis Topic", now, now)
        state = ps.derive(self.conn, min_conversations=1)
        blob = json.dumps(state)
        self.assertNotIn("super-secret-conversation-id-42", blob)
        self.assertNotIn("My Private Medical Diagnosis Topic", blob)
        self.assertNotIn("RAWJSONMARKER", blob)

    def test_version_pin(self):
        self.assertEqual(
            ps.CONTRACT_VERSION, 1,
            "personal_state.CONTRACT_VERSION changed -- before bumping, update "
            "PERSONAL_STATE_CONTRACT.md in this repo AND SUPPORTED_VERSIONS in "
            "the internet repo's discovery/personal_state.py",
        )

    def test_empty_db_produces_valid_artifact(self):
        state = ps.derive(self.conn)
        self.assertEqual(state["topics"], [])
        self.assertEqual(state["conversation_count"], 0)
        self.assertEqual(state["sources"], {"claude": 0, "chatgpt": 0})


if __name__ == "__main__":
    unittest.main()
