#!/usr/bin/env python3
"""Offline logic tests for council_bot.py (no network, no Telegram/Anthropic
credentials, no Chrome needed).

Exercises answer extraction, context loading, Telegram message chunking, and
the claude.ai JS payload builders (pure string construction -- never actually
evaluated in a browser here) against synthetic data.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from council_bot import (  # noqa: E402
    TELEGRAM_MESSAGE_LIMIT,
    REPO_ROOT,
    extract_final_call,
    js_delete_conversation,
    js_ensure_conversation,
    js_send_completion,
    js_upload_context_file,
    load_context,
    tg_send_message,
)


class TestExtractFinalCall(unittest.TestCase):
    def test_strips_everything_before_the_call(self):
        text = "Stage 1 blah blah\nStage 2 blah\nTHE CALL: Do the thing."
        self.assertEqual(extract_final_call(text), "THE CALL: Do the thing.")

    def test_strips_markdown_bold_wrapper(self):
        text = "preamble\n**THE CALL:** Do the thing."
        self.assertEqual(extract_final_call(text), "THE CALL: Do the thing.")

    def test_missing_label_returns_full_text_stripped(self):
        text = "  no label here, just an answer  "
        self.assertEqual(extract_final_call(text), "no label here, just an answer")


class TestLoadContext(unittest.TestCase):
    def test_single_file_via_env_var(self):
        # load_context() reports the path relative to REPO_ROOT, so the temp
        # file has to live under it (an absolute path elsewhere would raise).
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, encoding="utf-8", dir=REPO_ROOT
        ) as f:
            f.write("hello context")
            path = Path(f.name)
        try:
            with patch.dict("os.environ", {"COUNCIL_CONTEXT_FILE": str(path)}):
                text, n_files = load_context()
            self.assertEqual(n_files, 1)
            self.assertIn("hello context", text)
        finally:
            path.unlink()

    def test_globs_exports_dirs_relative_to_repo_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            exports_dir = tmp / "exports_test"
            exports_dir.mkdir()
            (exports_dir / "a.md").write_text("md content", encoding="utf-8")
            (exports_dir / "b.txt").write_text("txt content", encoding="utf-8")
            (exports_dir / "c.json").write_text("{}", encoding="utf-8")  # ignored pattern

            with patch("council_bot.REPO_ROOT", tmp), patch.dict(
                "os.environ", {}, clear=False
            ):
                import os as _os

                _os.environ.pop("COUNCIL_CONTEXT_FILE", None)
                text, n_files = load_context()

            self.assertEqual(n_files, 2)
            self.assertIn("md content", text)
            self.assertIn("txt content", text)
            self.assertNotIn("{}", text)


class TestTgSendMessage(unittest.TestCase):
    def test_splits_long_text_into_chunks_under_limit(self):
        long_text = "x" * (TELEGRAM_MESSAGE_LIMIT * 2 + 500)
        sent_chunks = []

        def fake_tg_call(token, method, params=None, timeout=None):
            sent_chunks.append(params["text"])
            return {}

        with patch("council_bot.tg_call", side_effect=fake_tg_call):
            tg_send_message("fake-token", 123, long_text)

        self.assertEqual("".join(sent_chunks), long_text)
        self.assertTrue(all(len(c) <= TELEGRAM_MESSAGE_LIMIT for c in sent_chunks))
        self.assertEqual(len(sent_chunks), 3)

    def test_short_text_sent_as_single_chunk(self):
        with patch("council_bot.tg_call") as mock_call:
            tg_send_message("fake-token", 123, "short reply")
        mock_call.assert_called_once_with(
            "fake-token", "sendMessage", {"chat_id": 123, "text": "short reply"}
        )


class TestJsPayloadBuilders(unittest.TestCase):
    def test_ensure_conversation_embeds_ids_and_json_escaped_name(self):
        js = js_ensure_conversation("org-1", "conv-1", 'Weird "name"')
        self.assertIn("org-1", js)
        self.assertIn("conv-1", js)
        self.assertIn(json.dumps('Weird "name"'), js)

    def test_upload_context_file_json_escapes_text(self):
        js = js_upload_context_file("org-1", "conv-1", "line with \"quotes\" and \n newline")
        self.assertIn(json.dumps("line with \"quotes\" and \n newline"), js)

    def test_send_completion_handles_missing_file_uuid(self):
        js = js_send_completion("org-1", "conv-1", "prompt text", "claude-opus-5", [], None)
        self.assertIn("files: fileUuid ? [fileUuid] : []", js)
        self.assertIn(json.dumps(None), js)

    def test_delete_conversation_embeds_ids(self):
        js = js_delete_conversation("org-1", "conv-1")
        self.assertIn("org-1", js)
        self.assertIn("conv-1", js)
        self.assertIn("DELETE", js)


if __name__ == "__main__":
    unittest.main(verbosity=2)
