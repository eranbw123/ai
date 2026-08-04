#!/usr/bin/env python3
"""Offline logic tests for claude_export.py (no network, no real credentials needed).

Exercises branch reconstruction, format conversion, and date/model filtering
against synthetic data shaped like the real claude.ai API response.
"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from claude_export import (  # noqa: E402
    convert_to_markdown,
    convert_to_text,
    filter_conversations,
    get_current_branch,
    write_export,
)
from common import parse_date, safe_filename  # noqa: E402

# A conversation with a branch: root -> a1 -> (b1 [abandoned] | b2 -> c1 [current])
SAMPLE_CONVERSATION = {
    "uuid": "conv-1",
    "name": "Test Conversation",
    "created_at": "2025-06-01T10:00:00.000000Z",
    "updated_at": "2025-06-02T11:00:00.000000Z",
    "model": "claude-sonnet-4-5-20250929",
    "current_leaf_message_uuid": "c1",
    "chat_messages": [
        # root's parent_message_uuid is None, which isn't in the map, so the loop
        # includes root itself before breaking -- matches content.js's getCurrentBranch.
        {"uuid": "root", "sender": "assistant", "parent_message_uuid": None, "text": "root msg (is included)"},
        {"uuid": "a1", "sender": "human", "parent_message_uuid": "root", "text": "Hello"},
        {"uuid": "b1", "sender": "assistant", "parent_message_uuid": "a1", "text": "Abandoned reply"},
        {"uuid": "b2", "sender": "assistant", "parent_message_uuid": "a1", "content": [{"text": "Hi there"}]},
        {"uuid": "c1", "sender": "human", "parent_message_uuid": "b2", "text": "Follow-up"},
    ],
}

SAMPLE_CONVERSATIONS_LIST = [
    {"uuid": "1", "name": "Jan chat", "model": "claude-sonnet-4-5-20250929", "created_at": "2025-01-15T00:00:00Z", "updated_at": "2025-01-16T00:00:00Z"},
    {"uuid": "2", "name": "June chat", "model": "claude-sonnet-4-5-20250929", "created_at": "2025-06-15T00:00:00Z", "updated_at": "2025-06-16T00:00:00Z"},
    {"uuid": "3", "name": "Dec chat, other model", "model": "claude-opus-4-5-20251101", "created_at": "2025-12-15T00:00:00Z", "updated_at": "2025-12-16T00:00:00Z"},
]


class TestBranchReconstruction(unittest.TestCase):
    def test_follows_current_leaf_not_abandoned_branch(self):
        branch = get_current_branch(SAMPLE_CONVERSATION)
        uuids = [m["uuid"] for m in branch]
        self.assertEqual(uuids, ["root", "a1", "b2", "c1"])
        self.assertNotIn("b1", uuids, "abandoned branch message must be excluded")

    def test_empty_when_no_leaf(self):
        self.assertEqual(get_current_branch({"chat_messages": [], "current_leaf_message_uuid": None}), [])


class TestFormatConversion(unittest.TestCase):
    def test_markdown_includes_metadata_and_branch_only(self):
        md = convert_to_markdown(SAMPLE_CONVERSATION, include_metadata=True)
        self.assertIn("# Test Conversation", md)
        self.assertIn("**Model:** claude-sonnet-4-5-20250929", md)
        self.assertIn("Hi there", md)  # from b2 (content list form)
        self.assertIn("Follow-up", md)  # from c1 (plain text form)
        self.assertNotIn("Abandoned reply", md)

    def test_text_abbreviates_repeated_sender_labels(self):
        text = convert_to_text(SAMPLE_CONVERSATION, include_metadata=False)
        self.assertIn("Assistant: root msg (is included)", text)  # 1st assistant turn: full label
        self.assertIn("Human: Hello", text)  # 1st human turn: full label
        self.assertIn("A: Hi there", text)  # 2nd assistant turn: abbreviated
        self.assertIn("H: Follow-up", text)  # 2nd human turn: abbreviated


class TestDateAndModelFiltering(unittest.TestCase):
    def test_after_before_range_on_updated_at(self):
        result = filter_conversations(
            SAMPLE_CONVERSATIONS_LIST,
            after=parse_date("2025-03-01"),
            before=parse_date("2025-11-01"),
            date_field="updated",
            model=None,
        )
        self.assertEqual([c["uuid"] for c in result], ["2"])

    def test_created_field_selection(self):
        result = filter_conversations(
            SAMPLE_CONVERSATIONS_LIST, after=parse_date("2025-12-01"), before=None, date_field="created", model=None
        )
        self.assertEqual([c["uuid"] for c in result], ["3"])

    def test_model_filter_combined_with_date(self):
        result = filter_conversations(
            SAMPLE_CONVERSATIONS_LIST,
            after=None,
            before=None,
            date_field="updated",
            model="claude-sonnet-4-5-20250929",
        )
        self.assertEqual({c["uuid"] for c in result}, {"1", "2"})

    def test_no_filters_returns_all(self):
        result = filter_conversations(SAMPLE_CONVERSATIONS_LIST, None, None, "updated", None)
        self.assertEqual(len(result), 3)


class TestFileWriting(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_write_export_json_roundtrip(self):
        path = write_export(SAMPLE_CONVERSATION, "json", True, self.tmp)
        self.assertTrue(path.exists())
        import json
        self.assertEqual(json.loads(path.read_text())["uuid"], "conv-1")

    def test_write_export_sanitizes_filename(self):
        conv = {**SAMPLE_CONVERSATION, "name": 'weird:/name*?"<>|'}
        path = write_export(conv, "markdown", False, self.tmp)
        self.assertTrue(path.exists())
        self.assertNotIn(":", path.name)
        self.assertNotIn("*", path.name)

    def test_safe_filename_truncates(self):
        self.assertLessEqual(len(safe_filename("x" * 300)), 150)


if __name__ == "__main__":
    unittest.main(verbosity=2)
