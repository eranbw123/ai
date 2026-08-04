#!/usr/bin/env python3
"""Offline logic tests for chatgpt_export.py (no network, no real credentials needed).

Exercises mapping-tree branch reconstruction (including skipping system/context
nodes, per extractConversationResult() in pionxzh/chatgpt-exporter), format
conversion, and date filtering against synthetic data shaped like the real
chatgpt.com /backend-api/conversation/{id} response.
"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from chatgpt_export import (  # noqa: E402
    convert_to_markdown,
    convert_to_text,
    filter_conversations,
    get_current_branch,
    write_export,
)
from common import parse_date  # noqa: E402


def node(node_id, parent, role=None, text=None, content_type="text", children=None):
    message = None
    if role is not None:
        message = {
            "author": {"role": role},
            "content": {"content_type": content_type, "parts": [text] if text is not None else []},
        }
    return node_id, {"id": node_id, "parent": parent, "children": children or [], "message": message}


# root(no message) -> sys(system, skipped) -> a1(user) -> [b1(assistant, abandoned) | b2(assistant)] -> c1(user, current)
MAPPING = dict(
    [
        node("root", None, role=None),
        node("sys", "root", role="system", text="You are ChatGPT"),
        node("a1", "sys", role="user", text="Hello"),
        node("b1", "a1", role="assistant", text="Abandoned reply"),
        node("b2", "a1", role="assistant", text="Hi there"),
        node("c1", "b2", role="user", text="Follow-up"),
        node("ctx", "root", role="user", text="hidden context", content_type="user_editable_context"),
    ]
)

SAMPLE_CONVERSATION = {
    "id": "conv-1",
    "title": "Test Conversation",
    "create_time": 1748772000,  # 2025-06-01T10:00:00Z
    "update_time": 1748775600,  # 2025-06-01T11:00:00Z
    "current_node": "c1",
    "mapping": MAPPING,
}

SAMPLE_CONVERSATIONS_LIST = [
    {"id": "1", "title": "Jan chat", "create_time": 1736899200, "update_time": 1736985600},  # 2025-01-15 / 16
    {"id": "2", "title": "June chat", "create_time": 1750000000, "update_time": 1750100000},  # ~2025-06
    {"id": "3", "title": "Dec chat", "create_time": 1765756800, "update_time": 1765843200},  # 2025-12-15 / 16
]


class TestBranchReconstruction(unittest.TestCase):
    def test_skips_system_and_context_nodes_follows_current_node(self):
        branch = get_current_branch(SAMPLE_CONVERSATION)
        ids = [n["id"] for n in branch]
        self.assertEqual(ids, ["a1", "b2", "c1"])
        self.assertNotIn("sys", ids, "system messages must be excluded")
        self.assertNotIn("ctx", ids, "context-injection nodes must be excluded")
        self.assertNotIn("b1", ids, "abandoned branch must be excluded")
        self.assertNotIn("root", ids, "root has no message, never included")

    def test_empty_when_no_current_node_and_no_leaf(self):
        self.assertEqual(get_current_branch({"mapping": {}, "current_node": None}), [])


class TestFormatConversion(unittest.TestCase):
    def test_markdown_includes_only_real_branch_messages(self):
        md = convert_to_markdown(SAMPLE_CONVERSATION, include_metadata=True)
        self.assertIn("# Test Conversation", md)
        self.assertIn("Hi there", md)
        self.assertIn("Follow-up", md)
        self.assertNotIn("Abandoned reply", md)
        self.assertNotIn("You are ChatGPT", md)
        self.assertNotIn("hidden context", md)

    def test_text_abbreviates_repeated_sender_labels(self):
        text = convert_to_text(SAMPLE_CONVERSATION, include_metadata=False)
        self.assertIn("Human: Hello", text)  # 1st human turn: full label
        self.assertIn("Assistant: Hi there", text)  # only assistant turn: full label
        self.assertIn("H: Follow-up", text)  # 2nd human turn: abbreviated


class TestDateFiltering(unittest.TestCase):
    def test_after_before_range_on_create_time(self):
        result = filter_conversations(
            SAMPLE_CONVERSATIONS_LIST,
            after=parse_date("2025-03-01"),
            before=parse_date("2025-11-01"),
            date_field="created",
        )
        self.assertEqual([c["id"] for c in result], ["2"])

    def test_updated_field_selection(self):
        result = filter_conversations(
            SAMPLE_CONVERSATIONS_LIST, after=parse_date("2025-12-01"), before=None, date_field="updated"
        )
        self.assertEqual([c["id"] for c in result], ["3"])

    def test_no_filters_returns_all(self):
        result = filter_conversations(SAMPLE_CONVERSATIONS_LIST, None, None, "created")
        self.assertEqual(len(result), 3)

    def test_iso_string_timestamps_also_supported(self):
        # Some accounts return create_time/update_time as ISO strings instead
        # of Unix floats, matching the reference project's `number | string` type.
        conversations = [{"id": "iso-1", "title": "String ts", "create_time": "2026-05-07T12:00:00.000000Z"}]
        result = filter_conversations(
            conversations, after=parse_date("2026-05-05"), before=parse_date("2026-05-10"), date_field="created"
        )
        self.assertEqual([c["id"] for c in result], ["iso-1"])


class TestFileWriting(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_write_export_json_roundtrip(self):
        path = write_export(SAMPLE_CONVERSATION, "json", True, self.tmp)
        self.assertTrue(path.exists())
        import json
        self.assertEqual(json.loads(path.read_text())["id"], "conv-1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
