"""Unit tests for the pure logic: filtering and state.

Deliberately does NOT touch the network — the ATS fetchers are exercised by
running `python main.py --dry-run`, not here. Run with:  python -m unittest -v
"""

import os
import tempfile
import unittest
from datetime import datetime, timezone

import state as state_mod
from filters import Filters, apply_filters, matches
from sources import Posting


def mk(pid="greenhouse:acme:1", title="Software Engineer Intern", location="NYC"):
    return Posting(
        id=pid, company="Acme", title=title, location=location,
        url="https://x/1", posted_at=datetime.now(timezone.utc), source="greenhouse",
    )


class TestFilters(unittest.TestCase):
    def setUp(self):
        self.f = Filters(
            title_include=["intern", "internship", "co-op", "new grad"],
            title_exclude=["senior", "staff", "principal", "manager", "director"],
        )

    def test_keeps_intern(self):
        self.assertTrue(matches(mk(title="Software Engineer Intern"), self.f))

    def test_keeps_internship_and_coop_and_newgrad(self):
        self.assertTrue(matches(mk(title="Backend Internship"), self.f))
        self.assertTrue(matches(mk(title="Engineering Co-op"), self.f))
        self.assertTrue(matches(mk(title="New Grad Software Engineer"), self.f))

    def test_word_boundary_rejects_internal_and_international(self):
        # The whole point of word-boundary matching: these are NOT internships.
        self.assertFalse(matches(mk(title="Internal Audit Lead"), self.f))
        self.assertFalse(matches(mk(title="Software Engineer, International"), self.f))

    def test_exclude_wins_over_include(self):
        self.assertFalse(matches(mk(title="Senior Software Engineer Intern"), self.f))

    def test_empty_include_matches_any_title(self):
        f = Filters(title_include=[], title_exclude=["manager"])
        self.assertTrue(matches(mk(title="Anything At All"), f))
        self.assertFalse(matches(mk(title="Product Manager"), f))

    def test_location_include_and_exclude(self):
        f = Filters(title_include=["intern"], location_include=["remote"])
        self.assertTrue(matches(mk(title="Intern", location="Remote - USA"), f))
        self.assertFalse(matches(mk(title="Intern", location="New York"), f))

        f2 = Filters(title_include=["intern"], location_exclude=["india"])
        self.assertFalse(matches(mk(title="Intern", location="Bengaluru, India"), f2))
        self.assertTrue(matches(mk(title="Intern", location="Dublin"), f2))

    def test_empty_location_include_matches_missing_location(self):
        f = Filters(title_include=["intern"], location_include=[])
        self.assertTrue(matches(mk(title="Intern", location=""), f))

    def test_apply_filters_returns_subset(self):
        posts = [mk(title="Intern"), mk(title="Senior Engineer"), mk(title="Co-op")]
        self.assertEqual(len(apply_filters(posts, self.f)), 2)


class TestState(unittest.TestCase):
    def test_add_dedup_and_new_ids(self):
        s = state_mod.State()
        self.assertEqual(s.add(["a", "b", "c"]), 3)
        self.assertEqual(s.add(["b", "c"]), 0)  # dedup
        self.assertTrue(s.is_seen("a"))
        self.assertFalse(s.is_seen("z"))
        new = s.new_ids([mk("a"), mk("z")])
        self.assertEqual([p.id for p in new], ["z"])

    def test_cap_drops_oldest_first(self):
        orig = state_mod.MAX_IDS
        state_mod.MAX_IDS = 3
        try:
            s = state_mod.State()
            s.add(["a", "b", "c", "d", "e"])
            self.assertEqual(s.ids, ["c", "d", "e"])
            self.assertFalse(s.is_seen("a"))
            self.assertTrue(s.is_seen("e"))
        finally:
            state_mod.MAX_IDS = orig

    def test_save_load_roundtrip(self):
        s = state_mod.State(ids=["x", "y"])
        s.touch()
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "seen.json")
            state_mod.save(s, path)
            s2 = state_mod.load(path)
            self.assertEqual(s2.ids, ["x", "y"])
            self.assertTrue(s2.last_run)

    def test_load_missing_file_is_empty(self):
        with tempfile.TemporaryDirectory() as d:
            s = state_mod.load(os.path.join(d, "nope.json"))
            self.assertEqual(s.ids, [])

    def test_load_corrupt_file_is_empty(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "seen.json")
            with open(path, "w") as fh:
                fh.write("{not valid json")
            self.assertEqual(state_mod.load(path).ids, [])


class TestMessages(unittest.TestCase):
    def test_summary_over_threshold(self):
        import notify
        posts = [mk(f"id{i}", title=f"Intern {i}") for i in range(notify.SUMMARY_THRESHOLD + 1)]
        msgs = notify.build_messages(posts)
        self.assertEqual(len(msgs), 1)
        self.assertIn("more than", msgs[0])

    def test_split_under_limit(self):
        import notify
        # Under SUMMARY_THRESHOLD (so we get a full list, not a summary) but with
        # enough text per posting to overflow a single 4096-char message.
        posts = [mk(f"id{i}", title="X" * 400, location="Y" * 200) for i in range(20)]
        msgs = notify.build_messages(posts)
        self.assertTrue(len(msgs) > 1)
        self.assertTrue(all(len(m) <= notify.TELEGRAM_LIMIT for m in msgs))

    def test_empty(self):
        import notify
        self.assertEqual(notify.build_messages([]), [])


class TestMatcher(unittest.TestCase):
    def _posting(self, pid, title="Intern", desc="Python Django backend"):
        return Posting(pid, "Acme", title, "NYC", "http://x", None, "greenhouse", desc)

    def test_build_matcher_disabled(self):
        import matcher
        self.assertIsNone(matcher.build_matcher({"matching": {"enabled": False}}, "r"))

    def test_build_matcher_no_resume(self):
        import matcher
        cfg = {"matching": {"enabled": True}}
        self.assertIsNone(matcher.build_matcher(cfg, None))

    def test_build_matcher_no_token(self):
        import matcher
        os.environ.pop("HF_API_TOKEN", None)
        cfg = {"matching": {"enabled": True, "provider": "huggingface"}}
        self.assertIsNone(matcher.build_matcher(cfg, "resume text"))

    def test_build_matcher_unknown_provider(self):
        import matcher
        os.environ["HF_API_TOKEN"] = "hf_x"
        try:
            cfg = {"matching": {"enabled": True, "provider": "openai"}}
            self.assertIsNone(matcher.build_matcher(cfg, "resume"))
        finally:
            os.environ.pop("HF_API_TOKEN", None)

    def test_threshold_gate_and_clamp(self):
        import matcher
        m = matcher.HuggingFaceMatcher(token="hf_x", threshold=0.3)
        m._similarities = lambda src, sents: [0.55, 0.10, -0.2, 1.4][: len(sents)]
        posts = [self._posting(f"id{i}") for i in range(4)]
        res = m.score_batch("resume", posts)
        self.assertEqual([r.matched for r in res], [True, False, False, True])
        self.assertEqual(res[2].score, 0.0)   # clamped up from -0.2
        self.assertEqual(res[3].score, 1.0)   # clamped down from 1.4

    def test_bad_response_shape_raises(self):
        import matcher, requests
        m = matcher.HuggingFaceMatcher(token="hf_x")

        class FakeResp:
            status_code = 200
            def raise_for_status(self): pass
            def json(self): return [0.5]  # wrong length (2 sentences expected)

        orig = requests.post
        requests.post = lambda *a, **k: FakeResp()
        try:
            with self.assertRaises(Exception):
                m.score_batch("r", [self._posting("a"), self._posting("b")])
        finally:
            requests.post = orig

    def test_posting_text_includes_title_and_desc(self):
        import matcher
        t = matcher.posting_text(self._posting("id", title="SWE Intern", desc="Django REST"))
        self.assertIn("SWE Intern", t)
        self.assertIn("Django REST", t)

    def test_empty_batch(self):
        import matcher
        m = matcher.HuggingFaceMatcher(token="hf_x")
        self.assertEqual(m.score_batch("r", []), [])


if __name__ == "__main__":
    unittest.main()
