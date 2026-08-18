#!/usr/bin/env python3
"""Offline logic tests for corpus_backfill.py.

No network, no Chrome, no real credentials: the browser side is a fake source
client, sleeps are captured instead of taken, and the sink is a throwaway
in-memory db.
"""
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import corpus_backfill as cb  # noqa: E402

# A minimal but non-empty ChatGPT `mapping` -- an empty one is exactly what
# is_valid_conversation_payload() rejects as a bad fetch (see
# test_export_to_sqlite.py), so a fixture standing for a real conversation
# needs a node in it.
FAKE_MAPPING = {"n1": {"id": "n1", "message": {"author": {"role": "user"},
                                               "content": {"parts": ["hi"]}}, "children": []}}


def gpt_detail(cid, title="T", update="2026-08-17T00:00:00Z", create="2026-08-17T00:00:00Z"):
    return {"conversation_id": cid, "title": title, "create_time": create,
            "update_time": update, "mapping": dict(FAKE_MAPPING)}


def gpt_summary(cid, title="T", update="2026-08-17T00:00:00Z", gizmo_id=None):
    return {"id": cid, "title": title, "create_time": update, "update_time": update,
            "gizmo_id": gizmo_id}


def open_memory_db():
    conn = sqlite3.connect(":memory:")
    conn.executescript(cb.SCHEMA)
    conn.executescript(cb.PROJECT_SCHEMA)
    return conn


class FakeClient:
    """Stands in for SourceClient: serves queued payloads (or raises queued
    exceptions) per conversation id, and records reconnects."""

    instances = []

    def __init__(self, source, port, script=None):
        self.source = source
        self.port = port
        self.script = script or {}
        self.connected = 0
        self.closed = 0
        FakeClient.instances.append(self)

    def connect(self):
        self.connected += 1
        return self

    def close(self):
        self.closed += 1

    def fetch(self, conversation_id):
        queue = self.script.get(conversation_id)
        value = queue.pop(0) if isinstance(queue, list) else queue
        if isinstance(value, Exception):
            raise value
        return value


def factory_for(script):
    def make(source, port):
        return FakeClient(source, port, script)
    return make


class TestSignalClassification(unittest.TestCase):
    """Rate-limiting and a dead socket need opposite responses -- back off hard
    vs. reconnect -- so misclassifying one as the other is the difference
    between riding out a throttle and hammering through it."""

    def test_rate_limit_signals(self):
        for message in ["JS exception: HTTP 429", "HTTP 403", "Too Many Requests",
                        "rate limit exceeded", "HTTP 529"]:
            self.assertTrue(cb.is_rate_limited(RuntimeError(message)), message)

    def test_non_rate_limit_signals(self):
        for message in ["HTTP 404", "empty/invalid response for x after 4 attempts"]:
            self.assertFalse(cb.is_rate_limited(RuntimeError(message)), message)

    def test_connection_lost_signals(self):
        self.assertTrue(cb.is_connection_lost(
            OSError("[WinError 10053] An established connection was aborted")))
        self.assertTrue(cb.is_connection_lost(
            ConnectionError("WebSocket connection closed unexpectedly")))
        self.assertFalse(cb.is_connection_lost(RuntimeError("HTTP 404")))


class TestPacer(unittest.TestCase):
    def test_delay_is_jittered_around_the_base(self):
        pacer = cb.Pacer(base=4.0, jitter=0.4)
        delays = [pacer.delay() for _ in range(200)]
        self.assertGreater(len(set(delays)), 100, "a fixed delay is exactly the periodic "
                                                  "request train limiters key on")
        self.assertTrue(all(4.0 * 0.6 - 1e-9 <= d <= 4.0 * 1.4 + 1e-9 for d in delays))

    def test_penalty_doubles_and_decays_slowly(self):
        pacer = cb.Pacer(base=4.0)
        pacer.penalize()
        pacer.penalize()
        self.assertEqual(pacer.penalty, 4.0)
        pacer.relax()
        # Decay must be slower than growth, or a throttled run oscillates
        # straight back into the limiter.
        self.assertGreater(pacer.penalty, 4.0 / 2)
        for _ in range(20):
            pacer.relax()
        self.assertEqual(pacer.penalty, 1.0)

    def test_penalty_is_capped(self):
        pacer = cb.Pacer(base=4.0, max_penalty=8.0)
        for _ in range(10):
            pacer.penalize()
        self.assertEqual(pacer.penalty, 8.0)

    def test_rate_limit_backoff_is_exponential_and_capped(self):
        slept = []
        pacer = cb.Pacer(rate_limit_pause=90.0, max_rate_limit_pause=1800.0)
        waits = [pacer.rate_limit_backoff(slept.append) for _ in range(6)]
        self.assertEqual(waits[0], 180.0)
        self.assertEqual(waits[1], 360.0)
        self.assertTrue(all(w <= 1800.0 for w in waits))
        self.assertEqual(waits[-1], 1800.0)
        self.assertEqual(slept, waits)

    def test_long_pause_fires_on_the_interval(self):
        slept = []
        pacer = cb.Pacer(base=1.0, jitter=0.0, long_pause_every=3, long_pause=60.0)
        for _ in range(3):
            pacer.sleep(slept.append)
        self.assertEqual(len(slept), 4)  # three per-item delays + one long pause
        self.assertEqual(slept[-1], 60.0)


class TestManifestMerge(unittest.TestCase):
    """The whole point of the manifest is that a project conversation missing
    from the flat list still ends up in it, exactly once, with its project."""

    def _build(self, flat, projects):
        captured = {}

        class FakeConn:
            def evaluate(self, _js, timeout=None):
                return "fake-token"

        def fake_own_tab(port, url, **kwargs):
            import contextlib

            @contextlib.contextmanager
            def cm():
                yield FakeConn()
            return cm()

        original_own_tab = cb.own_tab
        original_flat = cb.gc.fetch_all_conversation_summaries
        original_projects = cb.fetch_chatgpt_projects
        cb.own_tab = fake_own_tab
        cb.gc.fetch_all_conversation_summaries = lambda *a, **k: flat
        cb.fetch_chatgpt_projects = lambda *a, **k: projects
        try:
            captured["manifest"] = cb.build_manifest(9222, {"chatgpt"}, cb.Pacer())
        finally:
            cb.own_tab = original_own_tab
            cb.gc.fetch_all_conversation_summaries = original_flat
            cb.fetch_chatgpt_projects = original_projects
        return captured["manifest"]

    def test_project_only_conversation_is_added(self):
        manifest = self._build(
            flat=[gpt_summary("flat-1", "In the flat list")],
            projects=[{"id": "g-p-1", "name": "Novo",
                       "conversations": [gpt_summary("proj-1", "Project only",
                                                     gizmo_id="g-p-1")]}],
        )
        ids = {e["conversation_id"] for e in manifest["entries"]}
        self.assertEqual(ids, {"flat-1", "proj-1"})
        proj_entry = next(e for e in manifest["entries"] if e["conversation_id"] == "proj-1")
        self.assertEqual(proj_entry["project_name"], "Novo")
        self.assertEqual(manifest["projects"][0]["conversation_count"], 1)

    def test_conversation_in_both_appears_once_and_keeps_its_project(self):
        manifest = self._build(
            flat=[gpt_summary("both-1", "Seen twice", update="2026-08-10T00:00:00Z")],
            projects=[{"id": "g-p-1", "name": "Dates",
                       "conversations": [gpt_summary("both-1", "Seen twice",
                                                     update="2026-08-16T00:00:00Z",
                                                     gizmo_id="g-p-1")]}],
        )
        self.assertEqual(len(manifest["entries"]), 1)
        entry = manifest["entries"][0]
        self.assertEqual(entry["project_name"], "Dates")
        # The newer of the two sightings wins, so a stale flat-list
        # update_time can never mask a change the project walk saw.
        self.assertTrue(entry["updated_at"].startswith("2026-08-16"))

    def test_entries_are_newest_first(self):
        manifest = self._build(
            flat=[gpt_summary("old", update="2026-01-01T00:00:00Z"),
                  gpt_summary("new", update="2026-08-01T00:00:00Z")],
            projects=[],
        )
        self.assertEqual([e["conversation_id"] for e in manifest["entries"]], ["new", "old"])


class TestFetchChatgptProjects(unittest.TestCase):
    """The endpoint returns a 200 with `{"items": []}` for an over-cap `limit`,
    so a too-large page size reports every project as empty instead of failing
    loudly. That mistake silently hid every project-only conversation on the
    first live manifest run; these tests pin the fix."""

    class ScriptedConn:
        def __init__(self, sidebar_pages, project_pages):
            self.sidebar_pages = list(sidebar_pages)
            self.project_pages = project_pages
            self.paths = []

        def evaluate(self, js, timeout=None):
            path = js.split("fetch('", 1)[1].split("'", 1)[0]
            self.paths.append(path)
            if "snorlax/sidebar" in path:
                return self.sidebar_pages.pop(0)
            queue = self.project_pages
            return queue.pop(0) if isinstance(queue, list) else queue

    @staticmethod
    def sidebar(gizmos, cursor=None):
        return {"items": [{"gizmo": {"gizmo": {"id": gid,
                                               "display": {"name": name}}}}
                          for gid, name in gizmos],
                "cursor": cursor}

    def test_page_limit_stays_under_the_endpoint_cap(self):
        self.assertLessEqual(cb.PROJECT_CONVERSATIONS_PAGE_LIMIT, 50)

    def test_requested_limit_is_the_capped_one(self):
        conn = self.ScriptedConn(
            [self.sidebar([("g-p-1", "Novo")])],
            [{"items": [gpt_summary("c1")], "cursor": None}],
        )
        cb.fetch_chatgpt_projects(conn, "tok", cb.Pacer(base=0.0, jitter=0.0,
                                                        long_pause_every=0))
        conv_path = next(p for p in conn.paths if "/conversations" in p)
        self.assertIn(f"limit={cb.PROJECT_CONVERSATIONS_PAGE_LIMIT}", conv_path)

    def test_empty_first_page_is_retried_before_being_believed(self):
        conn = self.ScriptedConn(
            [self.sidebar([("g-p-1", "Novo")])],
            [{"items": [], "cursor": None},
             {"items": [gpt_summary("c1")], "cursor": None}],
        )
        projects = cb.fetch_chatgpt_projects(
            conn, "tok", cb.Pacer(base=0.0, jitter=0.0, long_pause_every=0))
        self.assertEqual(len(projects[0]["conversations"]), 1,
                         "an empty first page is a failure signal on this API, "
                         "not a genuinely empty project")

    def test_sidebar_stops_when_a_page_adds_no_new_project(self):
        """The sidebar hands back a non-null cursor forever, so trusting it
        would page until max_pages."""
        page = self.sidebar([("g-p-1", "Novo")], cursor="always-set")
        conn = self.ScriptedConn([page, page, page],
                                 {"items": [gpt_summary("c1")], "cursor": None})
        projects = cb.fetch_chatgpt_projects(
            conn, "tok", cb.Pacer(base=0.0, jitter=0.0, long_pause_every=0))
        self.assertEqual([p["id"] for p in projects], ["g-p-1"])
        self.assertEqual(sum(1 for p in conn.paths if "snorlax" in p), 2)


class TestPendingEntries(unittest.TestCase):
    def setUp(self):
        self.manifest = {"entries": [
            {"source": "chatgpt", "conversation_id": "a", "title": "A",
             "created_at": "2026-08-01T00:00:00+00:00", "updated_at": "2026-08-01T00:00:00+00:00",
             "project_id": None, "project_name": None},
            {"source": "chatgpt", "conversation_id": "b", "title": "B",
             "created_at": "2026-02-01T00:00:00+00:00", "updated_at": "2026-02-01T00:00:00+00:00",
             "project_id": None, "project_name": None},
            {"source": "claude", "conversation_id": "c", "title": "C",
             "created_at": "2026-08-05T00:00:00+00:00", "updated_at": "2026-08-05T00:00:00+00:00",
             "project_id": None, "project_name": None},
        ]}

    def test_unchanged_rows_are_skipped_entirely(self):
        stored = {("chatgpt", "a"): "2026-08-01T00:00:00+00:00"}
        pending = cb.pending_entries(self.manifest, stored)
        self.assertEqual([e["conversation_id"] for e in pending], ["c", "b"])

    def test_changed_updated_at_is_refetched(self):
        stored = {("chatgpt", "a"): "2026-07-01T00:00:00+00:00"}
        pending = cb.pending_entries(self.manifest, stored)
        self.assertIn("a", [e["conversation_id"] for e in pending])

    def test_source_and_since_filters(self):
        pending = cb.pending_entries(self.manifest, {}, sources={"chatgpt"})
        self.assertEqual([e["conversation_id"] for e in pending], ["a", "b"])
        pending = cb.pending_entries(self.manifest, {}, since="2026-06-01T00:00:00+00:00")
        self.assertEqual([e["conversation_id"] for e in pending], ["c", "a"])

    def test_council_bot_scratch_is_never_queued(self):
        """council_bot's own leftover scratch conversations must not enter the
        corpus -- see is_council_bot_scratch_conversation()'s docstring for why
        importing them is a correctness bug, not just noise."""
        from export_to_sqlite import COUNCIL_BOT_TITLE_PREFIX
        manifest = {"entries": [{"source": "claude", "conversation_id": "s",
                                 "title": COUNCIL_BOT_TITLE_PREFIX + " 12345",
                                 "created_at": None, "updated_at": None,
                                 "project_id": None, "project_name": None}]}
        self.assertEqual(cb.pending_entries(manifest, {}), [])


class TestSameInstant(unittest.TestCase):
    def test_z_suffix_and_offset_spelling_are_the_same_moment(self):
        self.assertTrue(cb.same_instant("2026-08-04T21:20:54.003049Z",
                                        "2026-08-04T21:20:54.003049+00:00"))

    def test_different_moments_are_not_equal(self):
        self.assertFalse(cb.same_instant("2026-08-04T21:20:54Z",
                                         "2026-08-04T21:20:55+00:00"))

    def test_missing_values_never_count_as_matching(self):
        self.assertFalse(cb.same_instant(None, "2026-08-04T21:20:54+00:00"))
        self.assertFalse(cb.same_instant("2026-08-04T21:20:54+00:00", None))

    def test_legacy_z_row_is_not_refetched(self):
        manifest = {"entries": [{"source": "claude", "conversation_id": "c",
                                 "title": "C", "created_at": None,
                                 "updated_at": "2026-08-04T21:20:54.003049+00:00",
                                 "project_id": None, "project_name": None}]}
        stored = {("claude", "c"): "2026-08-04T21:20:54.003049Z"}
        self.assertEqual(cb.pending_entries(manifest, stored), [])


class TestRun(unittest.TestCase):
    def setUp(self):
        FakeClient.instances = []
        self.conn = open_memory_db()
        self.tmp = tempfile.TemporaryDirectory()
        self.progress = str(Path(self.tmp.name) / "progress.jsonl")
        self.slept = []

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _manifest(self, entries):
        return {"entries": entries}

    def _entry(self, cid, updated="2026-08-17T00:00:00+00:00", project=None, pid=None):
        return {"source": "chatgpt", "conversation_id": cid, "title": cid,
                "created_at": updated, "updated_at": updated,
                "project_id": pid, "project_name": project}

    def _run(self, manifest, script, **kwargs):
        return cb.run(self.conn, manifest, port=9222, sources={"chatgpt"}, since=None,
                      pacer=cb.Pacer(base=0.0, jitter=0.0, long_pause_every=0),
                      progress_path=self.progress, sleeper=self.slept.append,
                      client_factory=factory_for(script), **kwargs)

    def test_stores_the_list_update_time_so_a_resume_can_skip(self):
        """ChatGPT's list and detail endpoints stamp update_time 1-2s apart.
        Storing the detail's while comparing against the list's is why the old
        skip-cache never fired and every pass re-requested the whole history."""
        entry = self._entry("c1", updated="2026-08-17T00:00:00+00:00")
        manifest = self._manifest([entry])
        detail = gpt_detail("c1", update="2026-08-17T00:00:02Z")  # 2s newer
        self._run(manifest, {"c1": detail})
        stored = self.conn.execute(
            "SELECT updated_at FROM raw_conversations WHERE conversation_id='c1'").fetchone()[0]
        self.assertTrue(cb.same_instant(stored, entry["updated_at"]),
                        f"stored {stored!r} must match the manifest value the "
                        f"next run compares against")
        self.assertEqual(cb.pending_entries(manifest, cb.stored_updated_at(self.conn)), [])

    def test_imports_and_records_the_project(self):
        manifest = self._manifest([self._entry("c1", project="Novo", pid="g-p-1")])
        stats = self._run(manifest, {"c1": gpt_detail("c1", "Live XYZ")})
        self.assertEqual(stats["inserted"], 1)
        row = self.conn.execute(
            "SELECT title, raw_source FROM raw_conversations WHERE conversation_id='c1'"
        ).fetchone()
        self.assertEqual(row, ("Live XYZ", "api_json"))
        self.assertEqual(
            self.conn.execute("SELECT project_id, project_name FROM conversation_projects "
                              "WHERE conversation_id='c1'").fetchone(),
            ("g-p-1", "Novo"),
        )

    def test_checkpoints_after_every_conversation(self):
        """A commit per conversation is what makes an interruption free: the
        row for c1 must already be durable while c2 is still being fetched."""
        committed = []

        manifest = self._manifest([self._entry("c1"), self._entry("c2")])

        class WatchingClient(FakeClient):
            def fetch(self, conversation_id):
                committed.append(self.conn_rowcount())
                return gpt_detail(conversation_id)

            def conn_rowcount(self_inner):
                return self.conn.execute(
                    "SELECT COUNT(*) FROM raw_conversations").fetchone()[0]

        def make(source, port):
            return WatchingClient(source, port, {})

        cb.run(self.conn, manifest, port=9222, sources={"chatgpt"}, since=None,
               pacer=cb.Pacer(base=0.0, jitter=0.0, long_pause_every=0),
               progress_path=self.progress, sleeper=self.slept.append,
               client_factory=make)
        # Before the first fetch nothing is stored; before the second, c1 is.
        self.assertEqual(committed, [0, 1])

    def test_progress_log_gets_one_line_per_conversation(self):
        manifest = self._manifest([self._entry("c1"), self._entry("c2")])
        self._run(manifest, {"c1": gpt_detail("c1"), "c2": gpt_detail("c2")})
        lines = [json.loads(line) for line in
                 Path(self.progress).read_text(encoding="utf-8").splitlines()]
        self.assertEqual([line["conversation_id"] for line in lines], ["c1", "c2"])
        self.assertTrue(all(line["outcome"] == "inserted" for line in lines))

    def test_rerun_is_idempotent_and_fetches_nothing(self):
        manifest = self._manifest([self._entry("c1")])
        self._run(manifest, {"c1": gpt_detail("c1")})
        FakeClient.instances = []
        stats = self._run(manifest, {})  # empty script: any fetch would KeyError
        self.assertEqual(stats["processed"], 0)
        self.assertEqual(FakeClient.instances, [], "a resume must not even open a tab "
                                                   "when there is nothing to do")
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM raw_conversations").fetchone()[0], 1)

    def test_rate_limit_backs_off_hard_and_does_not_retry_tightly(self):
        manifest = self._manifest([self._entry("c1")])
        script = {"c1": [RuntimeError("JS exception: HTTP 429"), gpt_detail("c1")]}
        stats = self._run(manifest, script)
        self.assertEqual(stats["rate_limited"], 1)
        self.assertEqual(stats["inserted"], 1)
        self.assertIn(180.0, self.slept, "a 429 must trigger the exponential backoff, "
                                         "not the ordinary per-item delay")

    def test_lost_connection_reconnects_and_retries_the_same_item(self):
        manifest = self._manifest([self._entry("c1")])
        script = {"c1": [OSError("[WinError 10053] An established connection was aborted"),
                         gpt_detail("c1")]}
        stats = self._run(manifest, script)
        self.assertEqual(stats["reconnects"], 1)
        self.assertEqual(stats["inserted"], 1)
        self.assertEqual(len(FakeClient.instances), 2, "reconnect means a fresh own tab")

    def test_permanent_failure_is_recorded_and_does_not_stop_the_run(self):
        manifest = self._manifest([self._entry("c1"), self._entry("c2")])
        script = {"c1": [RuntimeError("HTTP 404")] * 4, "c2": gpt_detail("c2")}
        stats = self._run(manifest, script)
        self.assertEqual(stats["failed"], 1)
        self.assertEqual(stats["inserted"], 1)
        lines = [json.loads(line) for line in
                 Path(self.progress).read_text(encoding="utf-8").splitlines()]
        self.assertEqual(lines[0]["outcome"], "failed")
        self.assertIn("404", lines[0]["error"])

    def test_empty_payload_is_never_written(self):
        """The bug that once destroyed 1441 ChatGPT conversations was writing a
        rate-limited empty response as if it were the conversation."""
        manifest = self._manifest([self._entry("c1")])
        # fetch_conversation_with_retry's real 10/20/30s backoff is the point of
        # that module's own tests, not this one -- don't pay for it here.
        with patch("export_to_sqlite.time.sleep"):
            stats = self._run(manifest, {"c1": [{}] * 8})
        self.assertEqual(stats["failed"], 1)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM raw_conversations").fetchone()[0], 0)

    def test_max_runtime_stops_early_with_a_resumable_state(self):
        manifest = self._manifest([self._entry(f"c{i}") for i in range(5)])
        script = {f"c{i}": gpt_detail(f"c{i}") for i in range(5)}
        stats = self._run(manifest, script, max_runtime_minutes=-1)
        self.assertTrue(stats["stopped_early"])
        self.assertEqual(stats["processed"], 0)


class TestStatusReport(unittest.TestCase):
    def test_report_mentions_projects_and_remaining_work(self):
        conn = open_memory_db()
        try:
            conn.execute(
                "INSERT INTO raw_conversations (source, conversation_id, title, created_at, "
                "updated_at, raw_json, content_hash) VALUES "
                "('chatgpt','c1','A','2026-08-01T00:00:00+00:00','2026-08-01T00:00:00+00:00','{}','h')")
            cb.record_project(conn, "chatgpt", "c1", "g-p-1", "Novo")
            manifest = {"built_at": "2026-08-18T00:00:00+00:00", "entries": [
                {"source": "chatgpt", "conversation_id": "c1", "title": "A",
                 "created_at": "2026-08-01T00:00:00+00:00",
                 "updated_at": "2026-08-01T00:00:00+00:00",
                 "project_id": "g-p-1", "project_name": "Novo"},
                {"source": "chatgpt", "conversation_id": "c2", "title": "B",
                 "created_at": "2026-08-02T00:00:00+00:00",
                 "updated_at": "2026-08-02T00:00:00+00:00",
                 "project_id": None, "project_name": None},
            ]}
            report = cb.status_report(conn, manifest)
            self.assertIn("Novo: 1", report)
            self.assertIn("2 conversations on the servers, 1 still to fetch", report)
        finally:
            conn.close()


class TestRecordProject(unittest.TestCase):
    def test_is_idempotent_and_updates_a_renamed_project(self):
        conn = open_memory_db()
        try:
            cb.record_project(conn, "chatgpt", "c1", "g-p-1", "Old name")
            cb.record_project(conn, "chatgpt", "c1", "g-p-1", "New name")
            rows = conn.execute("SELECT project_name FROM conversation_projects").fetchall()
            self.assertEqual(rows, [("New name",)])
        finally:
            conn.close()

    def test_no_project_records_nothing(self):
        conn = open_memory_db()
        try:
            cb.record_project(conn, "chatgpt", "c1", None, None)
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM conversation_projects").fetchone()[0], 0)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
