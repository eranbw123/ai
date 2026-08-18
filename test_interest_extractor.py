#!/usr/bin/env python3
"""Offline tests for interest_extractor.py.

No Chrome, no network, no LLM: the browser transport is replaced by a
scripted fake, and every fixture is synthetic. The corpus these were written
against is bilingual and actively being backfilled, so nothing here asserts a
corpus size -- only shapes and thresholds.

The acceptance tests near the bottom are the ones that matter most: the real
corpus contains a ~40-conversation, 7-month gaming cluster and a
supplements/health cluster that today's interest set covers with nothing at
all, alongside ~20 one-off tech-support conversations that must NOT become
interests. Those three populations are reproduced here at their measured
shape, and the extractor is required to surface the first two and reject the
third.
"""
import json
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import interest_extractor as ie  # noqa: E402

NOW = datetime(2026, 8, 18, tzinfo=timezone.utc)


# --- fixture helpers ---------------------------------------------------------

def topic(label, *, domain="other", lang="en", register="research", depth=0.5,
          quote="", entities=()):
    return {"label_en": label, "domain": domain, "lang": lang, "register": register,
            "depth": depth, "entities": list(entities), "quote": quote}


def digest(conv_id, when, topics, *, title="t", transient=False, source="chatgpt"):
    """A stored digest row as load_digests() would hand it back."""
    return {
        "content_hash": f"h-{conv_id}", "conversation_id": conv_id, "source": source,
        "title": title, "created_at": when.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
        "updated_at": when.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
        "topics": topics, "is_transient_support": transient,
    }


def spread(label, n, *, start_days_ago, span_days, prefix, **topic_kw):
    """n conversations on one theme, evenly spread over span_days."""
    out = []
    step = span_days / max(n - 1, 1)
    for i in range(n):
        when = NOW - timedelta(days=start_days_ago - i * step)
        out.append(digest(f"{prefix}-{i}", when, [topic(label, **topic_kw)]))
    return out


class FakeLLM:
    """Stands in for claude_browser.BrowserClaude.

    `replies` is a list consumed one call at a time; an entry that is an
    Exception is raised instead of returned, which is how the failure and
    resume paths get exercised.
    """

    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts = []
        self.shapes = []

    def complete_json(self, prompt, shape, timeout=None, context=None):
        self.prompts.append(prompt)
        self.shapes.append(shape)
        if not self.replies:
            raise AssertionError("FakeLLM ran out of scripted replies")
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def preflight(self):
        return True, ""

    def close(self):
        pass


# extract_conversation_nodes() walks parent links and stops at the node whose
# parent is None (the root, which carries no message), so a fixture needs a
# root plus at least one real child or it renders as an empty conversation.
CHATGPT_MAPPING = {
    "n0": {"id": "n0", "message": None, "parent": None, "children": ["n1"]},
    "n1": {"id": "n1", "message": {"author": {"role": "user"},
                                    "content": {"parts": ["how do I unlock Azazel"]}},
            "parent": "n0", "children": []},
}


def make_corpus_db(path, rows):
    """A conversations.db-shaped fixture. Mirrors the real schema closely
    enough for read_corpus()/render_conversation(); the extractor only ever
    reads it."""
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE raw_conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL,
            conversation_id TEXT NOT NULL, title TEXT, model TEXT,
            created_at TEXT, updated_at TEXT, raw_json TEXT NOT NULL,
            raw_source TEXT NOT NULL DEFAULT 'api_json', content_hash TEXT NOT NULL,
            imported_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (source, conversation_id))""")
    for r in rows:
        conn.execute(
            "INSERT INTO raw_conversations (source, conversation_id, title, created_at, "
            "updated_at, raw_json, content_hash) VALUES (?,?,?,?,?,?,?)",
            (r.get("source", "chatgpt"), r["conversation_id"], r.get("title"),
             r.get("created_at"), r.get("updated_at"),
             r.get("raw_json", json.dumps({"title": r.get("title"), "mapping": CHATGPT_MAPPING,
                                           "current_node": "n1"})),
             r["content_hash"]))
    conn.commit()
    return conn


# --- normalization -----------------------------------------------------------

class TestNormalization(unittest.TestCase):
    def test_theme_key_merges_word_order_and_plurals(self):
        self.assertEqual(ie.theme_key("Binding of Isaac unlocks"),
                          ie.theme_key("unlocks in the Binding of Isaac"))
        self.assertEqual(ie.theme_key("Steam Deck battery"), ie.theme_key("steam decks battery"))

    def test_theme_key_keeps_distinct_themes_distinct(self):
        self.assertNotEqual(ie.theme_key("Binding of Isaac unlocks"),
                             ie.theme_key("Elden Ring boss strategy"))

    def test_theme_key_survives_hebrew(self):
        """A Hebrew label must produce a real, stable, non-empty key -- an
        ASCII-only normalizer would collapse every Hebrew theme onto one."""
        k1 = ie.theme_key("שיטות זיכרון ללמידה")
        k2 = ie.theme_key("ללמידה שיטות זיכרון")  # same words, reordered
        self.assertTrue(k1)
        self.assertNotEqual(k1, "untitled")
        self.assertEqual(k1, k2)
        self.assertNotEqual(k1, ie.theme_key("מסלולי לימוד לפסיכיאטר"))

    def test_theme_key_never_returns_empty(self):
        self.assertTrue(ie.theme_key("the and of"))
        self.assertTrue(ie.theme_key(""))

    def test_detect_lang(self):
        self.assertEqual(ie.detect_lang("Steam Deck battery life"), "en")
        self.assertEqual(ie.detect_lang("מסלולי לימוד לפסיכיאטר"), "he")
        self.assertEqual(ie.detect_lang("כמה מודפיניל modafinil לקחת ביום"), "mixed")
        self.assertEqual(ie.detect_lang("123 !!!"), "und")

    def test_slugify_handles_both_scripts(self):
        self.assertEqual(ie.slugify("Binding of Isaac: unlocks!"), "binding-of-isaac-unlocks")
        self.assertTrue(ie.slugify("זיכרון עבודה"))


# --- the durability separator ------------------------------------------------

class TestDurabilitySeparator(unittest.TestCase):
    """The separator is the design's measured one (>=30 day span AND >=2
    calendar months = durable; a single <7 day burst = transient), not a
    re-derived one. These pin it so a later refactor can't quietly move it."""

    def test_durable_needs_both_span_and_months(self):
        self.assertEqual(ie.classify_durability(210, 7), "durable")
        self.assertEqual(ie.classify_durability(30, 2), "durable")
        # A fortnight of daily conversations is a binge, not a durable interest.
        self.assertEqual(ie.classify_durability(29, 2), "emerging")
        self.assertEqual(ie.classify_durability(60, 1), "emerging")

    def test_transient_is_the_single_short_burst(self):
        self.assertEqual(ie.classify_durability(0, 1), "transient")
        self.assertEqual(ie.classify_durability(6, 1), "transient")
        self.assertEqual(ie.classify_durability(7, 1), "emerging")

    def test_gate_admits_broad_or_deep(self):
        broad = {"n_convs": 3, "active_months": 2, "depth_max": 0.1}
        deep = {"n_convs": 2, "active_months": 1, "depth_max": 0.7}
        shallow_pair = {"n_convs": 2, "active_months": 1, "depth_max": 0.3}
        self.assertTrue(ie.passes_durability_gate(broad))
        self.assertTrue(ie.passes_durability_gate(deep))
        self.assertFalse(ie.passes_durability_gate(shallow_pair))


# --- aggregation -------------------------------------------------------------

class TestAggregation(unittest.TestCase):
    def test_computes_durability_axes(self):
        digests = spread("Steam Deck tuning", 5, start_days_ago=200, span_days=180,
                         prefix="sd", domain="gaming", depth=0.7, quote="my deck runs hot")
        themes = ie.aggregate_themes(digests, now=NOW)
        self.assertEqual(len(themes), 1)
        t = themes[0]
        self.assertEqual(t["n_convs"], 5)
        self.assertGreaterEqual(t["span_days"], 175)
        self.assertGreaterEqual(t["active_months"], 5)
        self.assertEqual(t["durability"], "durable")
        self.assertEqual(t["domain"], "gaming")
        self.assertLessEqual(t["recency_days"], 25)

    def test_support_topics_are_dropped(self):
        digests = spread("AirPlay won't connect", 5, start_days_ago=100, span_days=90,
                          prefix="ap", register="support")
        self.assertEqual(ie.aggregate_themes(digests, now=NOW), [])

    def test_transient_flagged_conversations_are_dropped(self):
        d = digest("x1", NOW - timedelta(days=5), [topic("Printer setup")], transient=True)
        self.assertEqual(ie.aggregate_themes([d], now=NOW), [])

    def test_evidence_quotes_are_kept_verbatim_including_hebrew(self):
        quote = "כמה מגנזיום כדאי לקחת בערב?"
        d = digest("he1", NOW - timedelta(days=10),
                    [topic("Magnesium timing", lang="he", quote=quote, domain="health")])
        themes = ie.aggregate_themes([d], now=NOW)
        ev = themes[0]["evidence"][0]
        self.assertEqual(ev["quote"], quote)  # byte-for-byte, not transliterated
        self.assertEqual(ev["lang"], "he")
        self.assertEqual(ev["conversation_id"], "he1")

    def test_quotes_are_capped(self):
        d = digest("q1", NOW, [topic("Long", quote="x" * 500)])
        self.assertLessEqual(len(ie.aggregate_themes([d], now=NOW)[0]["evidence"][0]["quote"]),
                              ie.MAX_QUOTE_CHARS)

    def test_hebrew_and_english_themes_stay_separate_for_the_reduce_pass(self):
        """String rules must not guess cross-language merges -- that is the
        reduce pass's semantic job. Both themes must survive to reach it."""
        digests = (spread("Working memory training", 3, start_days_ago=120, span_days=90, prefix="en")
                   + spread("זיכרון עבודה ואימון", 3, start_days_ago=120, span_days=90,
                            prefix="he", lang="he"))
        keys = {t["key"] for t in ie.aggregate_themes(digests, now=NOW)}
        self.assertEqual(len(keys), 2)

    def test_output_is_deterministic(self):
        digests = spread("Elden Ring builds", 4, start_days_ago=90, span_days=60, prefix="er")
        digests += spread("Hades runs", 2, start_days_ago=40, span_days=30, prefix="hd")
        a = ie.aggregate_themes(digests, now=NOW)
        b = ie.aggregate_themes(list(reversed(digests)), now=NOW)
        self.assertEqual([t["key"] for t in a], [t["key"] for t in b])


# --- scoring -----------------------------------------------------------------

class TestScoring(unittest.TestCase):
    def test_terms_match_the_design(self):
        score, terms = ie.score_candidate(
            {"n_convs": 8, "active_months": 4, "recency_days": 0},
            expected_yield=1.0, max_similarity=0.0)
        self.assertAlmostEqual(terms["evidence_strength"], 1.0, places=3)
        self.assertAlmostEqual(terms["recurrence"], 1.0, places=3)
        self.assertAlmostEqual(terms["recency"], 1.0, places=3)
        self.assertAlmostEqual(terms["novelty"], 1.0, places=3)
        self.assertAlmostEqual(score, 1.0, places=3)

    def test_recency_half_life_is_90_days(self):
        _, terms = ie.score_candidate({"n_convs": 2, "active_months": 1, "recency_days": 90})
        self.assertAlmostEqual(terms["recency"], 0.5, places=3)

    def test_similarity_suppresses_a_duplicate(self):
        novel, _ = ie.score_candidate({"n_convs": 5, "active_months": 3, "recency_days": 10},
                                       expected_yield=0.8, max_similarity=0.0)
        dupe, _ = ie.score_candidate({"n_convs": 5, "active_months": 3, "recency_days": 10},
                                      expected_yield=0.8, max_similarity=1.0)
        self.assertAlmostEqual(novel - dupe, ie.WEIGHTS["novelty"], places=3)

    def test_weights_sum_to_one(self):
        self.assertAlmostEqual(sum(ie.WEIGHTS.values()), 1.0, places=6)


# --- reduce-side plumbing ----------------------------------------------------

def make_candidate(key, theme_keys, **kw):
    base = {
        "kind": "new", "key": key, "title": key, "description": "d",
        "positive_signals": [], "negative_signals": [], "suggested_min_score": 0.7,
        "suggested_sources": ["web_search"],
        "parent_key": None, "related_keys": [], "from_theme_keys": list(theme_keys),
        "expected_yield": 0.8, "expected_yield_rationale": "", "exploratory": False,
        "similarity_to_existing": [],
    }
    base.update(kw)
    return base


class TestReducePlumbing(unittest.TestCase):
    def test_parse_map_reply_is_tolerant(self):
        reply = {"conversations": [
            {"index": 0, "topics": [{"label_en": "Good", "depth": "0.7", "register": "nonsense"}]},
            {"index": 99, "topics": [{"label_en": "Out of range"}]},   # dropped
            {"index": 1, "topics": "not a list"},                       # topics -> []
            "garbage",                                                   # dropped
        ]}
        got = ie.parse_map_reply(reply, batch_len=2)
        self.assertEqual(set(got), {0, 1})
        self.assertEqual(got[0]["topics"][0]["register"], "research")  # invalid -> default
        self.assertAlmostEqual(got[0]["topics"][0]["depth"], 0.7)
        self.assertEqual(got[1]["topics"], [])

    def test_parse_map_reply_clamps_depth(self):
        got = ie.parse_map_reply(
            {"conversations": [{"index": 0, "topics": [{"label_en": "L", "depth": 9}]}]}, 1)
        self.assertEqual(got[0]["topics"][0]["depth"], 1.0)

    def test_attach_evidence_uses_local_numbers_not_the_models(self):
        themes = ie.aggregate_themes(
            spread("Creatine dosing", 6, start_days_ago=300, span_days=280, prefix="cr",
                   domain="health", quote="how much creatine daily"), now=NOW)
        cands = ie.attach_evidence([make_candidate("creatine", [themes[0]["key"]])], themes)
        self.assertEqual(cands[0]["durability"]["n_convs"], 6)
        self.assertEqual(cands[0]["durability"]["class"], "durable")
        self.assertTrue(cands[0]["evidence"])
        self.assertEqual(cands[0]["source_conversations"][0], "cr-0")

    def test_merged_themes_union_their_active_months(self):
        """The map pass writes very specific labels, so a real cluster arrives
        as many single-conversation themes and only becomes one interest when
        the reduce pass merges them. Recurrence must then be the union of the
        months they were active in -- taking the max of their counts would
        report active_months=1 for a seven-month cluster and fail it at the
        gate."""
        # theme_key() drops digits, so the labels have to differ by real words.
        labels = ["Isaac unlock routes", "Elden Ring poise breakpoints",
                  "Hearthstone tempo curves", "Steam Deck thermal limits",
                  "Hades boon synergy", "Cuphead parry timing"]
        digests = []
        for i, label in enumerate(labels):  # one label per conversation, a month apart
            when = NOW - timedelta(days=200 - i * 30)
            digests.append(digest(f"m-{i}", when, [topic(label, domain="gaming")]))
        themes = ie.aggregate_themes(digests, now=NOW)
        self.assertTrue(all(t["n_convs"] == 1 for t in themes))
        self.assertTrue(all(t["active_months"] == 1 for t in themes))

        merged = ie.attach_evidence(
            [make_candidate("merged", [t["key"] for t in themes])], themes)[0]
        self.assertEqual(merged["durability"]["n_convs"], 6)
        # 30-day steps can land twice inside one calendar month, so the exact
        # count is 5 or 6 -- what matters is that it is the union, not the 1
        # that max() would have produced.
        self.assertGreaterEqual(merged["durability"]["active_months"], 5)
        self.assertEqual(merged["durability"]["class"], "durable")
        self.assertTrue(ie.rank_candidates([merged])[0]["qualified"])

    def test_evidence_quotes_come_from_distinct_conversations(self):
        """Four quotes from four conversations is provenance; four from one
        conversation is the same conversation four times."""
        themes = ie.aggregate_themes(
            spread("Isaac unlocks", 8, start_days_ago=200, span_days=180, prefix="iz",
                   domain="gaming", quote="best unlock order"), now=NOW)
        cand = ie.attach_evidence([make_candidate("iz", [themes[0]["key"]])], themes)[0]
        convs = [e["conversation_id"] for e in cand["evidence"]]
        self.assertEqual(len(convs), len(set(convs)))

    def test_invented_candidate_gets_no_durability_and_is_rejected(self):
        """A candidate the model made up, matching no theme, must not be able
        to claim evidence it does not have."""
        themes = ie.aggregate_themes(spread("Real theme", 4, start_days_ago=200,
                                             span_days=150, prefix="rt"), now=NOW)
        cands = ie.attach_evidence([make_candidate("fabricated", ["no-such-theme"])], themes)
        self.assertEqual(cands[0]["durability"]["n_convs"], 0)
        self.assertEqual(cands[0]["evidence"], [])
        ranked = ie.rank_candidates(cands)
        self.assertFalse(ranked[0]["qualified"])
        self.assertFalse(ranked[0]["offered"])

    def test_similarity_to_a_nonexistent_interest_does_not_depress_novelty(self):
        """Measured on the first real run: 6 of 84 similarity entries named a
        sibling CANDIDATE rather than an existing interest. Counting those
        would make a genuinely novel theme look like a duplicate of something
        the engine does not even follow. The consumer filters the same way."""
        themes = ie.aggregate_themes(
            spread("Isaac unlocks", 6, start_days_ago=200, span_days=150, prefix="iz",
                   domain="gaming"), now=NOW)
        cand = make_candidate("roguelikes", [themes[0]["key"]],
                               similarity_to_existing=[{"key": "some-other-candidate", "sim": 0.95}])
        enriched = ie.attach_evidence([cand], themes)

        unfiltered = ie.rank_candidates(enriched)[0]
        filtered = ie.rank_candidates(enriched, known_interest_keys={"a-real-interest"})[0]

        self.assertAlmostEqual(unfiltered["max_similarity"], 0.95)
        self.assertAlmostEqual(filtered["max_similarity"], 0.0)
        self.assertGreater(filtered["score"], unfiltered["score"])
        # The full list still ships, for provenance.
        self.assertEqual(len(filtered["similarity_to_existing"]), 1)

    def test_similarity_to_a_real_interest_still_counts(self):
        themes = ie.aggregate_themes(
            spread("Isaac unlocks", 6, start_days_ago=200, span_days=150, prefix="iz"), now=NOW)
        cand = make_candidate("dupe", [themes[0]["key"]],
                               similarity_to_existing=[{"key": "a-real-interest", "sim": 0.9}])
        ranked = ie.rank_candidates(ie.attach_evidence([cand], themes),
                                     known_interest_keys={"a-real-interest"})[0]
        self.assertAlmostEqual(ranked["max_similarity"], 0.9)

    def test_rank_caps_offers_and_keeps_the_serendipity_slot(self):
        themes = []
        cands = []
        for i in range(8):
            th = ie.aggregate_themes(
                spread(f"Theme {i}", 6 - (i % 3), start_days_ago=200, span_days=150,
                       prefix=f"t{i}"), now=NOW)
            themes += th
            cands.append(make_candidate(f"k{i}", [th[0]["key"]], expected_yield=0.9 - i * 0.05))
        # The weakest candidate is the exploratory one -- it must still make
        # the inbox, because serendipity is a lane rather than a weight.
        cands[-1]["exploratory"] = True
        ranked = ie.rank_candidates(ie.attach_evidence(cands, themes))
        offered = [c for c in ranked if c["offered"]]
        self.assertLessEqual(len(offered), ie.MAX_OFFERS_PER_RUN)
        self.assertTrue(any(c["exploratory"] for c in offered))

    def test_reduce_prompt_carries_stats_and_existing_interests_but_no_quotes(self):
        themes = ie.aggregate_themes(
            spread("Nebius earnings", 5, start_days_ago=120, span_days=100, prefix="nb",
                   quote="SECRET-QUOTE-SHOULD-NOT-APPEAR"), now=NOW)
        prompt, context = ie.build_reduce_prompt(
            themes, [{"key": "nbis-nebius", "title": "Nebius", "description": "x"}],
            ["blocked-thing"])
        self.assertIsNone(context)  # small corpus stays inline
        self.assertIn("nbis-nebius", prompt)
        self.assertIn("blocked-thing", prompt)
        self.assertIn("active_months", prompt)
        # Evidence never leaves the machine through the prompt -- the model
        # names and de-duplicates, quotes are attached locally afterwards.
        self.assertNotIn("SECRET-QUOTE-SHOULD-NOT-APPEAR", prompt)

    def test_reduce_prompt_groups_by_domain(self):
        themes = ie.aggregate_themes(
            spread("Isaac unlocks", 3, start_days_ago=200, span_days=150, prefix="g",
                   domain="gaming")
            + spread("Magnesium timing", 3, start_days_ago=200, span_days=150, prefix="h",
                     domain="health"),
            now=NOW)
        prompt, _ = ie.build_reduce_prompt(themes, [], [])
        self.assertIn("### DOMAIN: gaming", prompt)
        self.assertIn("### DOMAIN: health", prompt)

    def test_large_theme_block_becomes_an_uploaded_file(self):
        """A cap on themes would silently decide which interests may exist,
        so a big corpus is uploaded rather than truncated."""
        themes = ie.aggregate_themes(
            [digest(f"c-{i}", NOW - timedelta(days=i),
                    [topic(f"Theme number {'x' * (i % 40)} alpha{i}")]) for i in range(400)],
            now=NOW)
        prompt, context = ie.build_reduce_prompt(themes, [], [])
        self.assertIsNotNone(context)
        self.assertIn("attached file", prompt)
        self.assertIn("THEMES FROM THE OWNER", context)

    def test_no_theme_is_dropped_by_default(self):
        themes = ie.aggregate_themes(
            [digest(f"c-{i}", NOW - timedelta(days=i), [topic(f"Alpha{i} beta gamma")])
             for i in range(300)], now=NOW)
        prompt, context = ie.build_reduce_prompt(themes, [], [])
        body = context or prompt
        for t in themes:
            self.assertIn(t["key"], body)


# --- map: batching, checkpointing, resume ------------------------------------

class TestMapRun(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = Path(self.tmp.name)
        self.corpus_path = str(d / "corpus.db")
        self.digests_path = str(d / "digests.db")
        rows = [{"conversation_id": f"c{i}", "title": f"Conversation {i}",
                  "created_at": "2026-05-0{}T00:00:00+00:00".format(i + 1),
                  "updated_at": "2026-05-0{}T00:00:00+00:00".format(i + 1),
                  "content_hash": f"hash-{i}"} for i in range(4)]
        make_corpus_db(self.corpus_path, rows).close()
        self.conn = sqlite3.connect(f"file:{self.corpus_path}?mode=ro", uri=True)
        self.addCleanup(self.conn.close)
        self.dconn = ie.open_digests(self.digests_path)
        self.addCleanup(self.dconn.close)

    def _reply(self, n):
        return {"conversations": [
            {"index": i, "is_transient_support": False,
             "topics": [{"label_en": "Steam Deck tuning", "domain": "gaming", "lang": "en",
                          "register": "hobby-question", "depth": 0.7, "entities": ["Steam Deck"],
                          "quote": "my deck runs hot"}]} for i in range(n)]}

    def test_digests_everything_in_batches(self):
        llm = FakeLLM([self._reply(2), self._reply(2)])
        stats = ie.run_map(self.conn, self.dconn, llm, batch_size=2)
        self.assertEqual(stats["digested"], 4)
        self.assertEqual(stats["batches"], 2)
        self.assertEqual(len(ie.load_digests(self.dconn)), 4)

    def test_resume_skips_finished_work(self):
        """The whole point of checkpointing: a second run through a shared,
        flaky browser tab must not redo what already succeeded."""
        ie.run_map(self.conn, self.dconn, FakeLLM([self._reply(2)]), batch_size=2, limit=2)
        second = FakeLLM([self._reply(2)])
        stats = ie.run_map(self.conn, self.dconn, second, batch_size=2)
        self.assertEqual(stats["skipped_existing"], 2)
        self.assertEqual(stats["digested"], 2)
        self.assertEqual(len(second.prompts), 1)  # only the undone half was sent
        self.assertEqual(len(ie.load_digests(self.dconn)), 4)

    def test_a_failed_batch_is_recorded_and_the_run_continues(self):
        llm = FakeLLM([ie.BrowserClaudeError("tab died"), self._reply(2)])
        stats = ie.run_map(self.conn, self.dconn, llm, batch_size=2)
        self.assertEqual(stats["failed"], 2)
        self.assertEqual(stats["digested"], 2)

    def test_repeatedly_failing_conversations_are_retired(self):
        for _ in range(3):
            ie.run_map(self.conn, self.dconn, FakeLLM([ie.BrowserClaudeError("nope")] * 2),
                        batch_size=2, limit=2, max_attempts=3)
        # After max_attempts they drop out of the candidate set entirely, so a
        # later run converges instead of retrying poison rows forever.
        llm = FakeLLM([self._reply(2)])
        stats = ie.run_map(self.conn, self.dconn, llm, batch_size=2, limit=2, max_attempts=3)
        self.assertEqual(stats["digested"], 2)
        self.assertNotIn("c0", [d["conversation_id"] for d in ie.load_digests(self.dconn)])

    def test_a_later_success_clears_an_earlier_failure(self):
        ie.run_map(self.conn, self.dconn, FakeLLM([ie.BrowserClaudeError("x")] * 2),
                    batch_size=2, limit=2)
        ie.run_map(self.conn, self.dconn, FakeLLM([self._reply(2)]), batch_size=2, limit=2)
        left = self.dconn.execute("SELECT COUNT(*) FROM digest_failures").fetchone()[0]
        self.assertEqual(left, 0)

    def test_prompt_carries_bodies_and_indices(self):
        llm = FakeLLM([self._reply(2)])
        ie.run_map(self.conn, self.dconn, llm, batch_size=2, limit=2)
        self.assertIn("### CONVERSATION 0", llm.prompts[0])
        self.assertIn("### CONVERSATION 1", llm.prompts[0])
        self.assertIn("how do I unlock Azazel", llm.prompts[0])  # the rendered body

    def test_truncate_middle_keeps_both_ends(self):
        text = "START" + ("x" * 5000) + "END"
        out = ie.truncate_middle(text, 200)
        self.assertTrue(out.startswith("START"))
        self.assertTrue(out.endswith("END"))
        self.assertLess(len(out), 400)


# --- artifact ----------------------------------------------------------------

class TestArtifact(unittest.TestCase):
    def test_v2_artifact_is_v1_plus_candidates(self):
        import tempfile
        d = Path(tempfile.mkdtemp())
        path = str(d / "c.db")
        make_corpus_db(path, [{"conversation_id": "c1", "title": "Steam Deck battery",
                                "created_at": "2026-05-01T00:00:00+00:00",
                                "updated_at": "2026-05-01T00:00:00+00:00",
                                "content_hash": "h1"}]).close()
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            art = ie.build_artifact(conn, [{"conversation_id": "c1"}], [], [{"key": "x"}])
        finally:
            conn.close()
        self.assertEqual(art["contract_version"], 2)
        for key in ("topics", "candidates", "corpus", "generated_at", "sources"):
            self.assertIn(key, art)
        self.assertEqual(art["corpus"]["conversations_in_db"], 1)
        self.assertEqual(art["corpus"]["conversations_digested"], 1)

    def test_artifact_survives_a_json_round_trip_with_hebrew(self):
        art = {"contract_version": 2, "candidates": [
            {"key": "k", "evidence": [{"quote": "כמה מגנזיום כדאי לקחת?"}]}]}
        back = json.loads(json.dumps(art, ensure_ascii=False))
        self.assertEqual(back["candidates"][0]["evidence"][0]["quote"], "כמה מגנזיום כדאי לקחת?")


# --- loop closure: the machine's own chatter never becomes corpus -------------

class TestScratchExclusion(unittest.TestCase):
    """Both the council bot and this extractor create real claude.ai
    conversations and delete them best-effort. A conversation importer walking
    the account picks up the survivors, so they can land in conversations.db --
    observed live 2026-08-18. Digesting one would feed the extractor its own
    prompt (which contains conversation bodies) back as evidence.
    """

    def test_read_corpus_skips_both_kinds_of_scratch(self):
        import tempfile
        d = Path(tempfile.mkdtemp())
        path = str(d / "c.db")
        rows = [
            {"conversation_id": "real", "title": "Steam Deck battery life",
             "created_at": "2026-05-01T00:00:00+00:00",
             "updated_at": "2026-05-01T00:00:00+00:00", "content_hash": "h1"},
            {"conversation_id": "council", "title": "Council: what should I do about X",
             "created_at": "2026-05-02T00:00:00+00:00",
             "updated_at": "2026-05-02T00:00:00+00:00", "content_hash": "h2"},
            {"conversation_id": "scratch", "title": "interest-extractor scratch",
             "created_at": "2026-05-03T00:00:00+00:00",
             "updated_at": "2026-05-03T00:00:00+00:00", "content_hash": "h3"},
        ]
        make_corpus_db(path, rows).close()
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            got = [r["conversation_id"] for r in ie.read_corpus(conn)]
        finally:
            conn.close()
        self.assertEqual(got, ["real"])

    def test_scratch_predicate_matches_the_title_actually_used(self):
        import claude_browser as cb
        self.assertTrue(cb.is_scratch_conversation(cb.SCRATCH_TITLE_PREFIX))
        self.assertFalse(cb.is_scratch_conversation("A real conversation"))
        self.assertFalse(cb.is_scratch_conversation(None))


# --- acceptance: the clusters that today's interest set misses ----------------

class TestAcceptanceUncoveredClusters(unittest.TestCase):
    """The measured ground truth from the design's corpus analysis.

    Gaming (~42 conversations, 7 months, zero interest coverage) and
    supplements/health (~12 conversations, 18 months, zero coverage) are the
    two themes a working extractor MUST surface; the ~20 one-off tech-support
    conversations are the population it must not. Reproduced here at their
    measured shape rather than their real contents, so no conversation text
    enters git.
    """

    def _corpus(self):
        digests = []
        # Gaming: several sub-themes over 7 months, as the real cluster is.
        for name, n in (("Binding of Isaac progression and unlocks", 14),
                        ("Steam Deck performance tuning", 12),
                        ("Hearthstone Battlegrounds drafting", 9),
                        ("Elden Ring build planning", 7)):
            digests += spread(name, n, start_days_ago=250, span_days=210,
                              prefix=f"g-{ie.slugify(name)[:8]}", domain="gaming",
                              register="hobby-question", depth=0.7,
                              quote=f"what is the best approach in {name.split()[0]}")
        # Supplements/health: fewer conversations, much longer span.
        for name, n in (("Magnesium and sleep timing", 6),
                        ("Creatine and cognition", 4)):
            digests += spread(name, n, start_days_ago=540, span_days=520,
                              prefix=f"s-{ie.slugify(name)[:8]}", domain="health",
                              register="research", depth=0.65,
                              quote="how much should I take and when")
        # Hebrew-only theme: a real cluster that exists only in Hebrew.
        digests += spread("Psychiatry study tracks in Israel", 5, start_days_ago=200,
                          span_days=160, prefix="he-psy", domain="career", lang="he",
                          register="research", depth=0.7, quote="מסלולי לימוד לפסיכיאטר")
        # Transient support bursts: five AirPlay tickets in one week, etc.
        digests += spread("AirPlay will not connect to the TV", 5, start_days_ago=100,
                          span_days=4, prefix="sup-ap", register="support")
        digests += spread("Printer driver install", 3, start_days_ago=60, span_days=2,
                          prefix="sup-pr", register="support")
        return digests

    def setUp(self):
        self.themes = ie.aggregate_themes(self._corpus(), now=NOW)
        self.gated = [t for t in self.themes if ie.passes_durability_gate(t)]
        self.by_key = {t["key"]: t for t in self.themes}

    def test_gaming_cluster_surfaces_as_durable(self):
        gaming = [t for t in self.gated if t["domain"] == "gaming"]
        self.assertGreaterEqual(len(gaming), 4, "every gaming sub-theme must clear the gate")
        for t in gaming:
            self.assertEqual(t["durability"], "durable")
        self.assertGreaterEqual(max(t["n_convs"] for t in gaming), 14)

    def test_supplements_cluster_surfaces_as_durable(self):
        health = [t for t in self.gated if t["domain"] == "health"]
        self.assertGreaterEqual(len(health), 2)
        for t in health:
            self.assertEqual(t["durability"], "durable")

    def test_hebrew_only_theme_surfaces_with_its_hebrew_evidence(self):
        he = [t for t in self.gated if "he" in t["langs"]]
        self.assertTrue(he, "a theme that exists only in Hebrew must still be offerable")
        self.assertIn("מסלולי לימוד לפסיכיאטר", he[0]["evidence"][0]["quote"])

    def test_support_bursts_never_reach_the_gate(self):
        labels = " ".join(t["label"] for t in self.themes).lower()
        self.assertNotIn("airplay", labels)
        self.assertNotIn("printer", labels)

    def test_ranking_puts_the_uncovered_clusters_in_the_inbox(self):
        """End to end through the code-side half: given a model reply that
        simply names the gaming and supplements themes, both must clear the
        floors and be offered."""
        gaming_keys = [k for k, t in self.by_key.items() if t["domain"] == "gaming"]
        health_keys = [k for k, t in self.by_key.items() if t["domain"] == "health"]
        cands = [
            make_candidate("gaming-roguelikes-and-handhelds", gaming_keys, expected_yield=0.8),
            make_candidate("supplements-and-cognitive-performance", health_keys, expected_yield=0.7),
        ]
        ranked = ie.rank_candidates(ie.attach_evidence(cands, self.themes))
        offered = {c["key"] for c in ranked if c["offered"]}
        self.assertIn("gaming-roguelikes-and-handhelds", offered)
        self.assertIn("supplements-and-cognitive-performance", offered)
        for c in ranked:
            self.assertTrue(c["evidence"], "an offer must carry evidence quotes")
            self.assertGreaterEqual(c["durability"]["n_convs"], 4)

    def test_a_candidate_duplicating_an_existing_interest_loses_to_a_novel_one(self):
        gaming_keys = [k for k, t in self.by_key.items() if t["domain"] == "gaming"]
        novel = make_candidate("gaming-novel", gaming_keys, expected_yield=0.8)
        dupe = make_candidate("gaming-dupe", gaming_keys, expected_yield=0.8,
                               similarity_to_existing=[{"key": "existing", "sim": 0.95}])
        ranked = ie.rank_candidates(ie.attach_evidence([dupe, novel], self.themes))
        self.assertEqual(ranked[0]["key"], "gaming-novel")


# --- conformance with the consumer (internet/discovery/offers.py, PR H) ------

class TestConsumerConformance(unittest.TestCase):
    """The offers store parses specific field names out of each candidate.

    These pin the ones it actually reads, so a rename here fails locally
    instead of silently producing offers with no evidence or no novelty term
    in the other repo. Mirrors offers.py's `_normalize_candidate()` and
    `_normalize_evidence()` as shipped in PR H.
    """

    def setUp(self):
        themes = ie.aggregate_themes(
            spread("Magnesium and sleep", 6, start_days_ago=300, span_days=280, prefix="mg",
                   domain="health", lang="he", depth=0.7, quote="כמה מגנזיום כדאי לקחת בערב?"),
            now=NOW)
        cand = make_candidate("supplements-sleep", [themes[0]["key"]],
                               similarity_to_existing=[{"key": "sleep-health", "sim": 0.3}])
        self.ranked = ie.rank_candidates(ie.attach_evidence([cand], themes))[0]

    def test_evidence_carries_exactly_the_five_fields_the_store_reads(self):
        ev = self.ranked["evidence"][0]
        self.assertEqual(set(ev), {"date", "quote", "lang", "depth", "conversation_id"})
        self.assertTrue(ev["conversation_id"], "provenance is a first-class requirement")
        self.assertEqual(ev["lang"], "he")  # populated honestly, not defaulted

    def test_similarity_entries_use_the_key_the_store_reads_first(self):
        for entry in self.ranked["similarity_to_existing"]:
            self.assertIn("sim", entry)
            self.assertIn("key", entry)

    def test_durability_keys_match_the_stores_scorer_and_floors(self):
        for key in ("n_convs", "active_months", "recency_days"):
            self.assertIn(key, self.ranked["durability"])

    def test_candidate_carries_every_field_the_store_normalizes(self):
        for key in ("key", "kind", "title", "description", "positive_signals",
                    "negative_signals", "suggested_min_score", "suggested_sources",
                    "parent_key", "related_keys", "durability", "similarity_to_existing",
                    "expected_yield", "exploratory", "evidence", "source_conversations"):
            self.assertIn(key, self.ranked)

    def test_expected_yield_is_the_models_own_number(self):
        """The store computes neither expected_yield nor similarity, so a
        fabricated value here would silently corrupt its ranking. It must
        arrive from the reduce reply and be clamped, never invented."""
        parsed = ie.parse_reduce_reply({"candidates": [
            {"key": "a", "title": "A", "expected_yield": 0.83},
            {"key": "b", "title": "B", "expected_yield": "nonsense"},
            {"key": "c", "title": "C", "expected_yield": 7},
        ]})
        self.assertAlmostEqual(parsed[0]["expected_yield"], 0.83)
        self.assertAlmostEqual(parsed[1]["expected_yield"], 0.5)   # unparseable -> neutral
        self.assertAlmostEqual(parsed[2]["expected_yield"], 1.0)   # clamped


if __name__ == "__main__":
    unittest.main(verbosity=2)
