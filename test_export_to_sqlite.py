#!/usr/bin/env python3
"""Offline logic tests for export_to_sqlite.py (no network, no Chrome, no real
credentials -- run_claude()/run_chatgpt() are exercised against a fake CDP
connection and a throwaway in-memory db).
"""
import json
import sys
import sqlite3
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import export_to_sqlite as ets  # noqa: E402


# A minimal but non-empty ChatGPT `mapping` -- real conversations always have
# at least one message node; an empty mapping is exactly the shape
# is_valid_conversation_payload() now (correctly) rejects as a bad fetch, so
# fixtures representing a normal conversation need this instead of {}.
FAKE_MAPPING = {"node-1": {"id": "node-1", "message": {"author": {"role": "user"},
                                                        "content": {"parts": ["hi"]}}, "children": []}}


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
        self.addCleanup(self.conn.close)

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
        detail = {"id": list_item["id"], "title": None, "mapping": FAKE_MAPPING, "current_node": None,
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
        detail = {"id": list_item["id"], "title": "Fresh detail title", "mapping": FAKE_MAPPING, "current_node": None,
                   "create_time": 1, "update_time": 1}
        stats = self.run_chatgpt_with(list_item, detail)
        self.assertEqual(stats["inserted_titles"], ["Fresh detail title"])

    def test_untitled_fallback_when_both_are_missing(self):
        list_item = {"id": "6a700000-0000-0000-0000-000000000003", "title": None,
                     "create_time": 1, "update_time": 1}
        detail = {"id": list_item["id"], "title": None, "mapping": FAKE_MAPPING, "current_node": None,
                   "create_time": 1, "update_time": 1}
        stats = self.run_chatgpt_with(list_item, detail)
        self.assertEqual(stats["inserted_titles"], ["(untitled)"])


class TestUpdatedAtFiltering(unittest.TestCase):
    """Regression coverage for a real bug: a conversation that was already
    imported but later gets a new message must be re-synced, not permanently
    skipped once --after moves past its (unchanged) created_at. run_claude()/
    run_chatgpt() must filter candidates by updated_at, not created_at --
    upsert()'s content_hash comparison only ever gets a chance to run if the
    conversation isn't thrown away by the filter first.

    Each test's --after sits strictly after the old created_at but at/before
    the recent updated_at: filtering by "created" would exclude the
    conversation entirely (stats would stay all-zero, item never reaches
    upsert()); filtering by "updated" (the fix) includes it."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(ets.SCHEMA)
        self.addCleanup(self.conn.close)

    def seed_existing_row(self, source, conv_id, old_created_at):
        self.conn.execute(
            """INSERT INTO raw_conversations
               (source, conversation_id, title, created_at, updated_at,
                raw_json, raw_source, content_hash)
               VALUES (?, ?, 'Old title', ?, ?, '{"old": true}', 'api_json', 'stale-hash')""",
            (source, conv_id, old_created_at, old_created_at),
        )
        self.conn.commit()

    def test_chatgpt_resyncs_old_conversation_that_was_recently_updated(self):
        conv_id = "6a700000-0000-0000-0000-000000000010"
        old_created, recent_updated = "2026-01-01T00:00:00Z", "2026-08-05T12:00:00Z"
        self.seed_existing_row("chatgpt", conv_id, old_created)

        list_item = {"id": conv_id, "title": "Old title", "create_time": old_created, "update_time": recent_updated}
        detail = {"id": conv_id, "title": "Old title", "mapping": FAKE_MAPPING, "current_node": None,
                  "create_time": old_created, "update_time": recent_updated}
        args = types.SimpleNamespace(
            port=9222, limit=None, batch_size=10, notify=False,
            after=datetime(2026, 6, 1, tzinfo=timezone.utc), before=None,
        )
        fake_conn = FakeCDPConnection(responses=["fake-token", detail])
        with patch("export_to_sqlite.gc.connect", return_value=fake_conn), \
             patch("export_to_sqlite.gc.fetch_all_conversation_summaries", return_value=[list_item]):
            stats = ets.run_chatgpt(self.conn, args)

        self.assertEqual(stats["updated"] + stats["inserted"], 1, "conversation was wrongly filtered out")
        self.assertEqual(stats["updated"], 1)  # already existed -> updated, not inserted
        self.assertEqual(stats["updated_titles"], ["Old title"])

    def test_claude_resyncs_old_conversation_that_was_recently_updated(self):
        conv_id = "6a700000-0000-4000-8000-000000000011"
        old_created, recent_updated = "2026-01-01T00:00:00.000000Z", "2026-08-05T12:00:00.000000Z"
        self.seed_existing_row("claude", conv_id, old_created)

        list_item = {"uuid": conv_id, "name": "Old title", "created_at": old_created, "updated_at": recent_updated}
        detail = {"uuid": conv_id, "name": "Old title", "created_at": old_created, "updated_at": recent_updated,
                  "current_leaf_message_uuid": None, "chat_messages": [{"uuid": "m1", "sender": "human", "text": "hi"}]}
        args = types.SimpleNamespace(
            port=9222, limit=None, batch_size=10, notify=False,
            after=datetime(2026, 6, 1, tzinfo=timezone.utc), before=None,
        )
        fake_conn = FakeCDPConnection(responses=[detail])
        with patch("export_to_sqlite.cc.require_org_id", return_value="6a700000-0000-4000-8000-000000000000"), \
             patch("export_to_sqlite.cc.connect", return_value=fake_conn), \
             patch("export_to_sqlite.cc.fetch_all_conversation_summaries", return_value=[list_item]):
            stats = ets.run_claude(self.conn, args)

        self.assertEqual(stats["updated"] + stats["inserted"], 1, "conversation was wrongly filtered out")
        self.assertEqual(stats["updated"], 1)
        self.assertEqual(stats["updated_titles"], ["Old title"])


class TestContentHashStability(unittest.TestCase):
    """Regression coverage for a real bug caught live: ChatGPT returns some
    bookkeeping list fields (e.g. safe_urls) in a different element order on
    every single fetch, even when nothing about the conversation actually
    changed -- which made content_hash (and therefore the "updated" outcome)
    flap on essentially every poll cycle. compute_content_hash() must ignore
    that kind of reordering while still catching real content changes."""

    def test_reordered_scalar_list_hashes_the_same(self):
        a = {"id": "c1", "safe_urls": ["https://a.com", "https://b.com", "https://c.com"]}
        b = {"id": "c1", "safe_urls": ["https://c.com", "https://a.com", "https://b.com"]}
        self.assertEqual(ets.compute_content_hash(a), ets.compute_content_hash(b))

    def test_reordered_message_list_still_changes_the_hash(self):
        # Lists of dicts (actual message content) must NOT be reordered for
        # hashing purposes -- their order is semantically meaningful.
        a = {"chat_messages": [{"uuid": "1", "text": "first"}, {"uuid": "2", "text": "second"}]}
        b = {"chat_messages": [{"uuid": "2", "text": "second"}, {"uuid": "1", "text": "first"}]}
        self.assertNotEqual(ets.compute_content_hash(a), ets.compute_content_hash(b))

    def test_a_real_added_message_still_changes_the_hash(self):
        a = {"id": "c1", "safe_urls": ["https://a.com"], "chat_messages": [{"uuid": "1", "text": "hi"}]}
        b = {"id": "c1", "safe_urls": ["https://a.com"],
             "chat_messages": [{"uuid": "1", "text": "hi"}, {"uuid": "2", "text": "new reply"}]}
        self.assertNotEqual(ets.compute_content_hash(a), ets.compute_content_hash(b))

    def test_dict_key_order_does_not_affect_the_hash(self):
        a = {"id": "c1", "title": "t"}
        b = {"title": "t", "id": "c1"}
        self.assertEqual(ets.compute_content_hash(a), ets.compute_content_hash(b))

    def test_upsert_reports_unchanged_for_reordered_only_content(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.executescript(ets.SCHEMA)
        first = {"id": "c1", "safe_urls": ["https://a.com", "https://b.com"], "mapping": FAKE_MAPPING}
        second = {"id": "c1", "safe_urls": ["https://b.com", "https://a.com"], "mapping": FAKE_MAPPING}  # reordered

        outcome1, _ = ets.upsert(conn, source="chatgpt", conversation_id="c1", title="t", model=None,
                                  created_at="2026-01-01T00:00:00+00:00", updated_at="2026-01-01T00:00:00+00:00",
                                  raw_obj=first)
        outcome2, _ = ets.upsert(conn, source="chatgpt", conversation_id="c1", title="t", model=None,
                                  created_at="2026-01-01T00:00:00+00:00", updated_at="2026-08-05T00:00:00+00:00",
                                  raw_obj=second)
        self.assertEqual(outcome1, "inserted")
        self.assertEqual(outcome2, "unchanged", "reordering-only content must not look like a real update")


class TestRehashMigration(unittest.TestCase):
    """rehash_all_content_hashes() -- the one-time migration that has to run
    once after the hashing-scheme change, so old rows' stored content_hash
    (computed with the pre-fix, order-sensitive scheme) doesn't look
    "changed" the next time the poller merely re-checks them."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(ets.SCHEMA)
        self.addCleanup(self.conn.close)

    def insert_row(self, conv_id, raw_obj, stored_hash):
        self.conn.execute(
            """INSERT INTO raw_conversations
               (source, conversation_id, title, raw_json, raw_source, content_hash)
               VALUES ('chatgpt', ?, 't', ?, 'api_json', ?)""",
            (conv_id, json.dumps(raw_obj), stored_hash),
        )
        self.conn.commit()

    def test_recomputes_stale_hashes_and_reports_the_count(self):
        # Simulates a row whose stored hash was computed with the old,
        # order-sensitive scheme (a bogus placeholder here) -- must be
        # brought up to date to the new canonical value.
        self.insert_row("c1", {"id": "c1", "safe_urls": ["a", "b"]}, "stale-placeholder-hash")
        changed, total = ets.rehash_all_content_hashes(self.conn)
        self.assertEqual((changed, total), (1, 1))
        row = self.conn.execute("SELECT content_hash FROM raw_conversations WHERE conversation_id='c1'").fetchone()
        self.assertEqual(row[0], ets.compute_content_hash({"id": "c1", "safe_urls": ["a", "b"]}))

    def test_leaves_already_correct_hashes_untouched(self):
        raw_obj = {"id": "c1", "safe_urls": ["a", "b"]}
        self.insert_row("c1", raw_obj, ets.compute_content_hash(raw_obj))
        changed, total = ets.rehash_all_content_hashes(self.conn)
        self.assertEqual((changed, total), (0, 1))

    def test_does_not_touch_raw_json_or_other_columns(self):
        raw_obj = {"id": "c1", "safe_urls": ["a", "b"]}
        self.insert_row("c1", raw_obj, "stale")
        ets.rehash_all_content_hashes(self.conn)
        row = self.conn.execute(
            "SELECT raw_json, title, raw_source FROM raw_conversations WHERE conversation_id='c1'"
        ).fetchone()
        self.assertEqual(json.loads(row[0]), raw_obj)
        self.assertEqual(row[1], "t")
        self.assertEqual(row[2], "api_json")


class TestEmptyPayloadProtection(unittest.TestCase):
    """Regression coverage for the real incident, 2026-08-05: chatgpt.com
    returned an empty response ({}) for some per-conversation fetches under
    load, and run_chatgpt() -- which reads the conversation id from the
    separate list-page summary, not from the fetched detail -- upserted that
    empty payload as-is, silently overwriting the existing good row's
    raw_json with '{}'. By the time it was caught, 1441 of 1626 ChatGPT
    conversations' local copies had been destroyed this way. See
    is_valid_conversation_payload()'s docstring for the full story."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(ets.SCHEMA)
        self.addCleanup(self.conn.close)

    def test_empty_dict_is_not_a_valid_payload(self):
        self.assertFalse(ets.is_valid_conversation_payload("chatgpt", {}))
        self.assertFalse(ets.is_valid_conversation_payload("claude", {}))

    def test_payload_missing_the_messages_key_is_invalid(self):
        self.assertFalse(ets.is_valid_conversation_payload("chatgpt", {"id": "c1", "title": "t"}))
        self.assertFalse(ets.is_valid_conversation_payload("claude", {"uuid": "c1", "name": "t"}))

    def test_payload_with_empty_messages_container_is_invalid(self):
        self.assertFalse(ets.is_valid_conversation_payload("chatgpt", {"mapping": {}}))
        self.assertFalse(ets.is_valid_conversation_payload("claude", {"chat_messages": []}))

    def test_normal_payload_is_valid(self):
        self.assertTrue(ets.is_valid_conversation_payload("chatgpt", {"mapping": FAKE_MAPPING}))
        self.assertTrue(ets.is_valid_conversation_payload("claude", {"chat_messages": [{"uuid": "m1"}]}))

    def test_upsert_refuses_to_insert_an_empty_payload(self):
        with self.assertRaises(ValueError):
            ets.upsert(self.conn, source="chatgpt", conversation_id="c1", title="t", model=None,
                       created_at=None, updated_at=None, raw_obj={})
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM raw_conversations").fetchone()[0], 0)

    def test_upsert_refuses_to_overwrite_an_existing_good_row_with_an_empty_payload(self):
        # This is the exact shape of the incident: a row that already has real
        # content must survive an upsert() call carrying a bad fetch, not get
        # clobbered by it.
        good = {"id": "c1", "mapping": FAKE_MAPPING}
        ets.upsert(self.conn, source="chatgpt", conversation_id="c1", title="Real conversation", model=None,
                   created_at="2026-01-01T00:00:00+00:00", updated_at="2026-01-01T00:00:00+00:00", raw_obj=good)

        with self.assertRaises(ValueError):
            ets.upsert(self.conn, source="chatgpt", conversation_id="c1", title="Real conversation", model=None,
                       created_at="2026-01-01T00:00:00+00:00", updated_at="2026-08-05T00:00:00+00:00", raw_obj={})

        row = self.conn.execute(
            "SELECT raw_json FROM raw_conversations WHERE conversation_id = 'c1'"
        ).fetchone()
        self.assertEqual(json.loads(row[0]), good, "the existing good row must survive an empty-payload upsert")

    def test_fetch_with_retry_returns_data_once_a_later_attempt_succeeds(self):
        responses = iter([{}, {}, {"mapping": FAKE_MAPPING}])
        with patch("export_to_sqlite.time.sleep") as mock_sleep:
            data = ets.fetch_conversation_with_retry(
                lambda: next(responses), source="chatgpt", label="conv-1",
            )
        self.assertEqual(data, {"mapping": FAKE_MAPPING})
        self.assertEqual(mock_sleep.call_count, 2)  # backoff before attempts 2 and 3, none after success

    def test_fetch_with_retry_raises_after_exhausting_all_attempts(self):
        with patch("export_to_sqlite.time.sleep"):
            with self.assertRaises(RuntimeError):
                ets.fetch_conversation_with_retry(
                    lambda: {}, source="chatgpt", label="conv-1", max_attempts=4,
                )

    def test_fetch_with_retry_uses_linear_backoff(self):
        with patch("export_to_sqlite.time.sleep") as mock_sleep:
            with self.assertRaises(RuntimeError):
                ets.fetch_conversation_with_retry(
                    lambda: {}, source="chatgpt", label="conv-1", max_attempts=4, backoff_base=10,
                )
        self.assertEqual([c.args[0] for c in mock_sleep.call_args_list], [10, 20, 30])

    def test_run_chatgpt_leaves_existing_row_untouched_when_every_retry_comes_back_empty(self):
        # End-to-end replay of the actual incident, through run_chatgpt() as a
        # whole rather than just upsert()/fetch_conversation_with_retry() in
        # isolation: an existing good row must come out the other side of a
        # cycle that only ever sees empty responses for it completely
        # unchanged, and that item must be reported as failed, never inserted
        # or updated.
        conv_id = "6a700000-0000-0000-0000-0000000000aa"
        good_raw = {"id": conv_id, "mapping": FAKE_MAPPING}
        ets.upsert(self.conn, source="chatgpt", conversation_id=conv_id, title="Real conversation", model=None,
                   created_at="2026-01-01T00:00:00+00:00", updated_at="2026-01-01T00:00:00+00:00", raw_obj=good_raw)

        list_item = {"id": conv_id, "title": "Real conversation",
                     "create_time": "2026-01-01T00:00:00Z", "update_time": "2026-08-05T00:00:00Z"}
        args = types.SimpleNamespace(port=9222, limit=None, batch_size=10, notify=False, after=None, before=None)
        fake_conn = FakeCDPConnection(responses=["fake-token"] + [{}] * 4)  # every retry attempt comes back empty
        with patch("export_to_sqlite.gc.connect", return_value=fake_conn), \
             patch("export_to_sqlite.gc.fetch_all_conversation_summaries", return_value=[list_item]), \
             patch("export_to_sqlite.time.sleep"):
            stats = ets.run_chatgpt(self.conn, args)

        self.assertEqual(stats["failed"], 1)
        self.assertEqual(stats["inserted"], 0)
        self.assertEqual(stats["updated"], 0)
        row = self.conn.execute(
            "SELECT raw_json FROM raw_conversations WHERE conversation_id = ?", (conv_id,)
        ).fetchone()
        self.assertEqual(json.loads(row[0]), good_raw, "the existing good row must survive the failed cycle")


class TestImportLock(unittest.TestCase):
    """import_lock() -- prevents two `python export_to_sqlite.py <source>` CLI
    runs from stacking, which is what let two independent sessions hammer
    chatgpt.com concurrently during the real incident (see import_lock's own
    docstring for the full rationale)."""

    def setUp(self):
        self.lock_path = Path(tempfile.mkdtemp()) / "conversations.db.import-lock"
        self.addCleanup(lambda: self.lock_path.unlink(missing_ok=True))

    def test_second_acquire_while_held_raises(self):
        with ets.import_lock(self.lock_path):
            self.assertTrue(self.lock_path.exists())
            with self.assertRaises(ets.ImportLockError):
                with ets.import_lock(self.lock_path):
                    pass  # pragma: no cover -- must not be reached

    def test_lock_file_is_removed_after_the_with_block_exits(self):
        with ets.import_lock(self.lock_path):
            pass
        self.assertFalse(self.lock_path.exists())

    def test_lock_file_is_removed_even_if_the_with_block_raises(self):
        with self.assertRaises(ValueError):
            with ets.import_lock(self.lock_path):
                raise ValueError("boom")
        self.assertFalse(self.lock_path.exists())

    def test_can_reacquire_after_a_clean_release(self):
        with ets.import_lock(self.lock_path):
            pass
        with ets.import_lock(self.lock_path):  # must not raise -- previous lock was released
            pass


if __name__ == "__main__":
    unittest.main()
