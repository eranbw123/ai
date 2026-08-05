#!/usr/bin/env python3
"""Offline logic tests for export_to_sqlite.py (no network, no Chrome, no real
credentials -- run_claude()/run_chatgpt() are exercised against a fake CDP
connection and a throwaway in-memory db).
"""
import sys
import sqlite3
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import export_to_sqlite as ets  # noqa: E402


class FakeCDPConnection:
    """Stands in for cdp.CDPConnection: .evaluate(js) returns whatever the
    test queues up next, in call order. .close() is a no-op."""

    def __init__(self, responses):
        self._responses = list(responses)

    def evaluate(self, _js, timeout=None):
        return self._responses.pop(0)

    def close(self):
        pass


class TestRunChatgptTitleFallback(unittest.TestCase):
    """Regression coverage for a real bug caught live: chatgpt.com's
    per-conversation detail fetch returns title=null for conversations too
    fresh for the title to have been generated server-side yet, even though
    the list page already has it -- run_chatgpt() must fall back to that
    list-page title rather than storing/reporting "(untitled)" for every
    brand-new conversation."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(ets.SCHEMA)

    def run_chatgpt_with(self, list_item, detail_response):
        fake_conn = FakeCDPConnection(responses=["fake-token", detail_response])
        args = types.SimpleNamespace(
            port=9222, limit=None, batch_size=10, notify=False, after=None, before=None,
        )
        with patch("export_to_sqlite.gc.connect", return_value=fake_conn), \
             patch("export_to_sqlite.gc.fetch_all_conversation_summaries", return_value=[list_item]):
            return ets.run_chatgpt(self.conn, args)

    def test_falls_back_to_list_title_when_detail_title_is_null(self):
        list_item = {"id": "6a7362e1-8470-83eb-b822-ef415f941698", "title": "Test conversation 2",
                     "create_time": 1754411, "update_time": 1754411}
        detail = {"id": list_item["id"], "title": None, "mapping": {}, "current_node": None,
                   "create_time": 1754411, "update_time": 1754411}
        stats = self.run_chatgpt_with(list_item, detail)
        self.assertEqual(stats["inserted_titles"], ["Test conversation 2"])
        row = self.conn.execute(
            "SELECT title FROM raw_conversations WHERE conversation_id = ?", (list_item["id"],)
        ).fetchone()
        self.assertEqual(row[0], "Test conversation 2")

    def test_prefers_detail_title_when_present(self):
        list_item = {"id": "6a700000-0000-0000-0000-000000000002", "title": "Stale list title",
                     "create_time": 1, "update_time": 1}
        detail = {"id": list_item["id"], "title": "Fresh detail title", "mapping": {}, "current_node": None,
                   "create_time": 1, "update_time": 1}
        stats = self.run_chatgpt_with(list_item, detail)
        self.assertEqual(stats["inserted_titles"], ["Fresh detail title"])

    def test_untitled_fallback_when_both_are_missing(self):
        list_item = {"id": "6a700000-0000-0000-0000-000000000003", "title": None,
                     "create_time": 1, "update_time": 1}
        detail = {"id": list_item["id"], "title": None, "mapping": {}, "current_node": None,
                   "create_time": 1, "update_time": 1}
        stats = self.run_chatgpt_with(list_item, detail)
        self.assertEqual(stats["inserted_titles"], ["(untitled)"])


if __name__ == "__main__":
    unittest.main()
