"""Gemini fallback (13 Nov): Claude first and unchanged; Gemini only when Claude is Dry AND a Gemini budget is
set; every Gemini problem is a Dry (= today's HOLD); its spend is counted apart from the Claude money."""
import _ident  # noqa: F401
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from polished import gemini, ledger, llm  # noqa: E402

USAGE = {"promptTokenCount": 20000, "candidatesTokenCount": 800, "thoughtsTokenCount": 1200}


def claude_ok(self, *a, **k):
    self.calls.append({"task": a[0], "cost": 0.01})
    self.last_lane = a[2]
    return '{"verdict": "pass"}', 0.01


class FallbackTest(unittest.TestCase):
    def setUp(self):
        os.environ.pop("GTA6_GEMINI_PLAN_MODEL", None)
        os.environ.pop("GTA6_GEMINI_REVIEW_MODEL", None)

    def test_claude_path_untouched_when_it_can_pay(self):
        b = llm.Budget(5.0, 0.0, gem_limit=10.0)
        with mock.patch.object(llm.Budget, "_claude", claude_ok), mock.patch.object(gemini, "generate") as g:
            text, c = b.call("t", "p", model="haiku")
        g.assert_not_called()
        self.assertEqual(b.last_lane, "haiku")
        self.assertEqual(b.gem_spent, 0.0)

    def test_no_gemini_budget_holds_exactly_as_before(self):
        b = llm.Budget(0.0, 0.0)            # api_dry, no GTA6_GEMINI_BUDGET
        with mock.patch.object(gemini, "generate") as g, mock.patch.dict(os.environ, {"GTA6_ANTHROPIC_KEY": "x"}):
            with self.assertRaises(llm.Dry) as cm:
                b.call("gta6-review-plan", "p" * 1000, model="sonnet")
        g.assert_not_called()
        self.assertIn("cloud budget", str(cm.exception))
        self.assertNotIn("Gemini", str(cm.exception))

    def test_dry_claude_goes_to_gemini_and_counts_apart(self):
        b = llm.Budget(0.0, 0.0, gem_limit=10.0, gem_spent=1.0)
        with mock.patch.object(gemini, "generate", return_value=('{"x": 1}', USAGE, "STOP", "gemini-3.1-pro-preview")) as g:
            text, c = b.call("gta6-review-plan", "p" * 1000, model="sonnet")
        self.assertEqual(g.call_count, 1)
        self.assertEqual(text, '{"x": 1}')
        self.assertAlmostEqual(c, (20000 * 2.00 + 2000 * 12.00) / 1e6)
        self.assertEqual(b.spent, 0.0)                       # Claude money untouched
        self.assertAlmostEqual(b.gem_spent, 1.0 + c)
        self.assertEqual(b.calls[-1]["lane"], "gemini")
        self.assertTrue(b.last_lane.startswith("gemini "))

    def test_reviewer_role_maps_to_the_review_model(self):
        b = llm.Budget(0.0, 0.0, gem_limit=10.0)
        with mock.patch.object(gemini, "generate", return_value=("{}", USAGE, "STOP", "m")) as g:
            b.call("gta6-review-episode", "p", model="haiku", images=())
        self.assertEqual(g.call_args[0][2], gemini.DEFAULT_MODELS["review"][0])

    def test_planner_on_haiku_still_gets_the_plan_models(self):
        b = llm.Budget(0.0, 0.0, gem_limit=10.0)
        with mock.patch.object(gemini, "generate", return_value=("{}", USAGE, "STOP", "m")) as g:
            b.call("gta6-review-plan", "p", model="haiku")
        self.assertEqual(g.call_args[0][2], gemini.DEFAULT_MODELS["plan"][0])

    def test_broken_json_tries_the_next_model_and_counts_both(self):
        b = llm.Budget(0.0, 0.0, gem_limit=10.0)
        answers = [('{"end_option": 2, "menu_cut": ["M0" }', USAGE, "STOP", "a"), ('{"end_option": 2}', USAGE, "STOP", "b")]
        with mock.patch.object(gemini, "generate", side_effect=answers) as g:
            text, c = b.call("gta6-review-plan", "p", model="sonnet")
        self.assertEqual([x[0][2] for x in g.call_args_list], gemini.DEFAULT_MODELS["plan"])
        self.assertEqual(text, '{"end_option": 2}')
        self.assertEqual(len(b.calls), 2)
        self.assertAlmostEqual(b.gem_spent, sum(x["cost"] for x in b.calls))

    def test_all_answers_broken_returns_the_last_so_the_caller_holds(self):
        b = llm.Budget(0.0, 0.0, gem_limit=10.0)
        with mock.patch.object(gemini, "generate", return_value=("not json", USAGE, "STOP", "m")):
            text, c = b.call("gta6-review-episode", "p", model="haiku")
        self.assertEqual(text, "not json")             # review.py / plan_cut.py then HOLD "answer unusable"
        self.assertEqual(len(b.calls), 2)

    def test_first_model_error_falls_to_the_second(self):
        b = llm.Budget(0.0, 0.0, gem_limit=10.0)
        with mock.patch.object(gemini, "generate", side_effect=[gemini.GeminiDry("HTTP 404: retired"),
                                                                ('{"verdict": "pass"}', USAGE, "STOP", "b")]):
            text, c = b.call("gta6-review-episode", "p", model="haiku")
        self.assertIn("pass", text)
        self.assertEqual(len(b.calls), 1)

    def test_gemini_cap_refuses_before_calling(self):
        b = llm.Budget(0.0, 0.0, gem_limit=1.0, gem_spent=0.999)
        with mock.patch.object(gemini, "generate") as g:
            with self.assertRaises(llm.Dry) as cm:
                b.call("gta6-review-plan", "p" * 50000, model="sonnet")
        g.assert_not_called()
        self.assertIn("Gemini budget", str(cm.exception))
        self.assertIn("budget", str(cm.exception))           # conductor files it as kind "credit"

    def test_any_gemini_error_is_a_hold(self):
        b = llm.Budget(0.0, 0.0, gem_limit=10.0)
        with mock.patch.object(gemini, "generate", side_effect=gemini.GeminiDry("HTTP 429: quota")):
            with self.assertRaises(llm.Dry) as cm:
                b.call("gta6-review-plan", "p", model="sonnet")
        self.assertIn("429", str(cm.exception))
        self.assertEqual(b.calls, [])

    def test_unknown_model_price_is_refused(self):
        b = llm.Budget(0.0, 0.0, gem_limit=10.0)
        with mock.patch.dict(os.environ, {"GTA6_GEMINI_PLAN_MODEL": "gemini-9-ultra"}), \
                mock.patch.object(gemini, "generate") as g:
            with self.assertRaises(llm.Dry):
                b.call("gta6-review-plan", "p", model="sonnet")
        g.assert_not_called()

    def test_price_lookup_takes_the_longest_match(self):
        self.assertEqual(gemini.price_of("gemini-3.5-flash-lite-preview-11-2026"), gemini.PRICES["gemini-3.5-flash-lite"])
        self.assertEqual(gemini.price_of("gemini-3.5-flash"), gemini.PRICES["gemini-3.5-flash"])
        self.assertIsNone(gemini.price_of("gemini-3.5"))

    def test_no_credentials_is_a_dry(self):
        with mock.patch.dict(os.environ, {"GTA6_GCP_TOKEN": ""}), mock.patch.object(gemini, "_gcloud", return_value=None), \
                mock.patch.dict(sys.modules, {"google.auth": None}):
            gemini._tok.update(v=None)
            with self.assertRaises(gemini.GeminiDry):
                gemini.token()


class LedgerTest(unittest.TestCase):
    def test_old_names_still_parse_and_gem_is_optional(self):
        self.assertEqual(ledger.ledger_name(1, 1, 0.1, 0.2, 0.3), "gta6-ledger-1-1-api100000-modal200000-vast300000")
        n = ledger.ledger_name(1, 1, 0.0, 0.2, 0.0, 0.0123)
        self.assertTrue(n.endswith("-gem12300"))
        arts = {"artifacts": [{"name": "gta6-ledger-5-1-api100000-modal0-vast0", "created_at": "2026-11-14T00:00:00Z"},
                              {"name": n, "created_at": "2026-11-14T01:00:00Z"},
                              {"name": "gta6-state-1-0", "created_at": "2026-11-14T01:00:00Z"}]}
        with mock.patch.object(ledger, "_gh", return_value=arts), mock.patch.dict(os.environ, {"GITHUB_REPOSITORY": "o/r"}):
            self.assertAlmostEqual(ledger.api_spent("2026-11-13"), 0.1)
            self.assertAlmostEqual(ledger.gemini_spent("2026-11-13"), 0.0123)
            self.assertAlmostEqual(ledger.gemini_spent("2026-11-15"), 0.0)


if __name__ == "__main__":
    unittest.main()
