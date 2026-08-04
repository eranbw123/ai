#!/usr/bin/env python3
"""Offline test for the early-stop pagination logic in claude_export_cdp.py.

Uses a fake CDP connection (no live Chrome/network) that serves canned
chat_conversations_v2-shaped pages, to verify fetch_all_conversation_summaries()
stops paging as soon as it sees an item older than `after`, instead of
walking every page via has_more.
"""
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from claude_export_cdp import fetch_all_conversation_summaries  # noqa: E402
from common import parse_date  # noqa: E402


def make_item(idx, updated_at):
    return {"uuid": f"uuid-{idx}", "name": f"conv {idx}", "created_at": updated_at, "updated_at": updated_at}


class FakeConn:
    """Serves 3 pages of 5 items each (v2 shape: data/has_more), updated_at
    strictly descending. Counts how many evaluate() calls were made."""

    def __init__(self):
        self.call_count = 0
        dates = [
            "2026-05-10", "2026-05-09", "2026-05-08", "2026-05-07", "2026-05-06",
            "2026-05-05", "2026-05-04", "2026-05-03", "2026-05-02", "2026-05-01",
            "2026-04-30", "2026-04-29", "2026-04-28", "2026-04-27", "2026-04-26",
        ]
        self.items = [make_item(i, f"{d}T00:00:00Z") for i, d in enumerate(dates)]

    def evaluate(self, js_expr, **kwargs):
        self.call_count += 1
        offset = int(re.search(r"offset=(\d+)", js_expr).group(1))
        limit = int(re.search(r"limit=(\d+)", js_expr).group(1))
        page_items = self.items[offset:offset + limit]
        has_more = (offset + limit) < len(self.items)
        return {"data": page_items, "has_more": has_more}


class TestEarlyStopPagination(unittest.TestCase):
    def test_stops_once_page_drops_below_after_bound(self):
        conn = FakeConn()
        # Same shape as the ChatGPT test: page 0 = 05-10..05-06 (all >= after),
        # page 1 = 05-05..05-01 (oldest item 05-01 < after) -> stop, never
        # fetch page 2 (04-30..04-26).
        after = parse_date("2026-05-05")
        result = fetch_all_conversation_summaries(conn, org_id="fake-org", after=after, limit=5)
        self.assertEqual(conn.call_count, 2, "should stop after page 1, not walk into page 2")
        self.assertEqual(len(result), 10)

    def test_no_after_bound_walks_all_pages_via_has_more(self):
        conn = FakeConn()
        result = fetch_all_conversation_summaries(conn, org_id="fake-org", after=None, limit=5)
        self.assertEqual(conn.call_count, 3, "has_more=False on the 3rd page ends the loop, no extra empty page needed")
        self.assertEqual(len(result), 15)


if __name__ == "__main__":
    unittest.main(verbosity=2)
