#!/usr/bin/env python3
"""Offline tests for cdp.py's Runtime.evaluate result handling.

No socket is opened: CDPConnection.__init__ is bypassed and send() is
replaced with canned CDP responses, which is enough to pin the one thing
that actually went wrong in production -- how a JS exception is detected.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cdp  # noqa: E402


def make_connection(response):
    """A CDPConnection whose send() returns `response`, with no socket."""
    conn = cdp.CDPConnection.__new__(cdp.CDPConnection)
    conn.send = lambda method, params=None, timeout=60: response
    return conn


class TestEvaluate(unittest.TestCase):
    def test_returns_the_value(self):
        conn = make_connection({"id": 1, "result": {"result": {"type": "object",
                                                                "value": {"items": [1, 2]}}}})
        self.assertEqual(conn.evaluate("whatever"), {"items": [1, 2]})

    def test_raises_on_a_js_exception(self):
        """The regression that mattered.

        Chrome nests exceptionDetails INSIDE the response's "result" object,
        not beside it. Checking the top level (as this did until 2026-08-18)
        never matched, so a thrown error was returned as an ordinary value --
        `{}` for a rejected async IIFE. Live effect: a logged-out claude.ai
        tab made the conversation-list fetch throw HTTP 403, evaluate()
        handed back {}, the caller read `data` as [], and the poller recorded
        a successful "0 conversations" cycle and advanced its watermark past
        conversations it had never fetched.
        """
        conn = make_connection({"id": 1, "result": {
            "result": {"type": "object", "className": "Error", "value": {}},
            "exceptionDetails": {"text": "Uncaught (in promise)",
                                  "exception": {"description": "Error: HTTP 403"}},
        }})
        with self.assertRaises(RuntimeError) as ctx:
            conn.evaluate("(async () => { throw new Error('HTTP 403') })()")
        self.assertIn("403", str(ctx.exception))

    def test_missing_value_is_none_not_an_error(self):
        """A JS expression that legitimately evaluates to undefined returns
        None -- that is not an exception and must not be treated as one."""
        conn = make_connection({"id": 1, "result": {"result": {"type": "undefined"}}})
        self.assertIsNone(conn.evaluate("void 0"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
