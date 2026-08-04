#!/usr/bin/env python3
"""Offline test for the early-stop pagination logic in chatgpt_export_cdp.py.

Uses a fake CDP connection (no live Chrome/network) that serves canned pages,
to verify fetch_all_conversation_summaries() stops paging as soon as it sees
an item older than `after`, instead of walking every page.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from chatgpt_export_cdp import fetch_all_conversation_summaries  # noqa: E402
from common import parse_date  # noqa: E402


def make_item(idx, update_time):
    return {"id": f"id-{idx}", "title": f"conv {idx}", "create_time": update_time, "update_time": update_time}


class FakeConn:
    """Serves 3 pages of 5 items each, update_time strictly descending,
    then an empty page. Counts how many evaluate() calls were made."""

    def __init__(self):
        self.call_count = 0
        # Page 0: 2026-05-10..06 (newest), Page 1: 2026-05-05..01, Page 2: 2026-04-30..26
        dates = [
            "2026-05-10", "2026-05-09", "2026-05-08", "2026-05-07", "2026-05-06",
            "2026-05-05", "2026-05-04", "2026-05-03", "2026-05-02", "2026-05-01",
            "2026-04-30", "2026-04-29", "2026-04-28", "2026-04-27", "2026-04-26",
        ]
        self.items = [make_item(i, f"{d}T00:00:00Z") for i, d in enumerate(dates)]

    def evaluate(self, js_expr, **kwargs):
        self.call_count += 1
        # js_fetch_conversations_page embeds offset/limit as literals in the JS string
        import re
        offset = int(re.search(r"offset=(\d+)", js_expr).group(1))
        limit = int(re.search(r"limit=(\d+)", js_expr).group(1))
        page_items = self.items[offset:offset + limit]
        return {"items": page_items, "total": len(self.items), "offset": offset, "limit": limit}


class TestEarlyStopPagination(unittest.TestCase):
    def test_stops_once_page_drops_below_after_bound(self):
        conn = FakeConn()
        # after = 2026-05-05: page 0 (05-10..05-06) is all >= after, so must
        # fetch page 1 too (05-05..05-01) since its *newest* item (05-05) still
        # qualifies -- but page 1's oldest item (05-01) is < after, so it
        # should stop there and never fetch page 2.
        after = parse_date("2026-05-05")
        result = fetch_all_conversation_summaries(conn, token="fake", after=after, limit=5)
        self.assertEqual(conn.call_count, 2, "should stop after page 1, not walk into page 2")
        self.assertEqual(len(result), 10)  # pages 0 and 1 combined

    def test_no_after_bound_walks_all_pages(self):
        conn = FakeConn()
        result = fetch_all_conversation_summaries(conn, token="fake", after=None, limit=5)
        self.assertEqual(conn.call_count, 4)  # 3 pages of data + 1 empty page to confirm end
        self.assertEqual(len(result), 15)


if __name__ == "__main__":
    unittest.main(verbosity=2)
