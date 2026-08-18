#!/usr/bin/env python3
"""Offline tests for claude_browser.py.

The CDP connection is a fake that records the JavaScript it was handed and
returns canned completion payloads, so nothing here needs Chrome, a network,
or a claude.ai session.
"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import claude_browser as cb  # noqa: E402


class FakeConn:
    """Records every evaluate() call and answers the completion one.

    `replies` is consumed one completion at a time; an Exception entry is
    raised instead, which is how the error paths get exercised.
    """

    def __init__(self, replies):
        self.replies = list(replies)
        self.js = []
        self.closed = False

    def evaluate(self, js, timeout=None):
        self.js.append(js)
        if "completion" in js and "getReader" in js:
            reply = self.replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply
        if "upload-file" in js:
            return "file-uuid-1"
        return True

    def close(self):
        self.closed = True


def make_llm(replies, **kw):
    conn = FakeConn(replies)
    llm = cb.BrowserClaude(org_id="org-1", connect=lambda: conn, **kw)
    return llm, conn


def completion(text):
    return json.dumps({"text": text, "messageLimit": None})


class TestExtractJsonObject(unittest.TestCase):
    def test_bare_object(self):
        self.assertEqual(cb.extract_json_object('{"a": 1}'), '{"a": 1}')

    def test_fenced(self):
        self.assertEqual(json.loads(cb.extract_json_object('```json\n{"a": 1}\n```'))["a"], 1)

    def test_narrated(self):
        text = 'Sure! Here is the JSON:\n{"a": 1}\nLet me know if you need more.'
        self.assertEqual(json.loads(cb.extract_json_object(text))["a"], 1)

    def test_hebrew_content_survives(self):
        text = '{"quote": "כמה מגנזיום כדאי לקחת?"}'
        self.assertEqual(json.loads(cb.extract_json_object(text))["quote"], "כמה מגנזיום כדאי לקחת?")

    def test_no_object_raises(self):
        with self.assertRaises(ValueError):
            cb.extract_json_object("I'd rather not.")


class TestComplete(unittest.TestCase):
    def test_round_trip_creates_and_deletes_a_scratch_conversation(self):
        llm, conn = make_llm([completion("hello")])
        self.assertEqual(llm.complete("hi"), "hello")
        joined = " ".join(conn.js)
        self.assertIn("chat_conversations", joined)   # created
        self.assertIn("DELETE", joined)               # and cleaned up
        self.assertEqual(llm.calls, 1)

    def test_reuses_one_connection_across_calls(self):
        llm, conn = make_llm([completion("a"), completion("b")])
        llm.complete("1")
        llm.complete("2")
        self.assertEqual(llm.calls, 2)
        self.assertFalse(conn.closed)  # not torn down between calls

    def test_empty_reply_is_an_error(self):
        llm, _ = make_llm([completion("   ")])
        with self.assertRaises(cb.BrowserClaudeError):
            llm.complete("hi")

    def test_none_reply_is_an_error(self):
        """The tab was navigated mid-request -- a real failure, not an empty
        answer, and it must not read as 'the model said nothing'."""
        llm, _ = make_llm([None])
        with self.assertRaises(cb.BrowserClaudeError) as ctx:
            llm.complete("hi")
        self.assertIn("navigated", str(ctx.exception))

    def test_js_exception_becomes_a_browser_error(self):
        llm, _ = make_llm([RuntimeError("JS exception: HTTP 403")])
        with self.assertRaises(cb.BrowserClaudeError):
            llm.complete("hi")

    def test_scratch_conversation_is_deleted_even_when_the_call_fails(self):
        llm, conn = make_llm([RuntimeError("JS exception: HTTP 500")])
        with self.assertRaises(cb.BrowserClaudeError):
            llm.complete("hi")
        self.assertTrue(any("DELETE" in js for js in conn.js))

    def test_context_is_uploaded_as_a_file(self):
        llm, conn = make_llm([completion("ok")])
        llm.complete("prompt", context="a very long corpus")
        self.assertTrue(any("upload-file" in js for js in conn.js))

    def test_dict_payload_is_accepted(self):
        """Some Chrome/CDP combinations hand the value back already
        deserialized instead of as a JSON string."""
        llm, _ = make_llm([{"text": "already a dict", "messageLimit": None}])
        self.assertEqual(llm.complete("hi"), "already a dict")


class TestCompleteJson(unittest.TestCase):
    def test_parses_and_returns_an_object(self):
        llm, _ = make_llm([completion('{"candidates": []}')])
        self.assertEqual(llm.complete_json("p", "{}"), {"candidates": []})

    def test_retries_once_on_unparseable_output(self):
        llm, conn = make_llm([completion("no json here, sorry"), completion('{"ok": true}')])
        self.assertEqual(llm.complete_json("p", "{}"), {"ok": True})
        self.assertEqual(llm.calls, 2)
        # The retry must carry the sterner instruction, otherwise it is just
        # the same request again.
        sent = [js for js in conn.js if "getReader" in js]
        self.assertIn("not parseable", sent[1])

    def test_gives_up_after_the_retry(self):
        llm, _ = make_llm([completion("nope"), completion("still nope")])
        with self.assertRaises(cb.BrowserClaudeError):
            llm.complete_json("p", "{}")

    def test_a_json_array_is_rejected(self):
        """The callers all want an object; a bare array means the model
        answered a different question."""
        llm, _ = make_llm([completion("[1, 2, 3]"), completion("[4]")])
        with self.assertRaises(cb.BrowserClaudeError):
            llm.complete_json("p", "{}")

    def test_shape_is_included_in_the_prompt(self):
        llm, conn = make_llm([completion('{"a": 1}')])
        llm.complete_json("do the thing", '{"a": <int>}')
        sent = [js for js in conn.js if "getReader" in js][0]
        self.assertIn("do the thing", sent)
        self.assertIn("a\\\": <int>", sent.replace("\\\\", "\\"))


class TestJsBuilders(unittest.TestCase):
    def test_titles_and_prompts_are_json_escaped(self):
        js = cb.js_ensure_conversation("org-1", "conv-1", 'Weird "name" with \n newline')
        self.assertIn('\\"name\\"', js)

    def test_hebrew_prompt_is_embedded_safely(self):
        """json.dumps escapes non-ASCII to \\uXXXX, which is what makes a
        Hebrew prompt safe to paste into a JS source string regardless of how
        Chrome decodes the CDP payload. Assert it round-trips rather than
        that the raw characters appear."""
        js = cb.js_send_completion("org-1", "conv-1", "כמה מגנזיום?", "claude-opus-5", [], None)
        embedded = js.split("prompt: ", 1)[1].split(",\n", 1)[0]
        self.assertNotIn("כמה", js)  # escaped, not raw
        self.assertEqual(json.loads(embedded), "כמה מגנזיום?")

    def test_parse_completion_result_accepts_both_shapes(self):
        self.assertEqual(cb.parse_completion_result('{"text": "hi"}')["text"], "hi")
        self.assertEqual(cb.parse_completion_result({"text": "hi"})["text"], "hi")


if __name__ == "__main__":
    unittest.main(verbosity=2)
