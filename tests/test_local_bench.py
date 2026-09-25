"""Offline checks for the local-model benchmark: fixtures and scoring, no Ollama."""
import unittest

from loop import diagnosis, keeper
from scripts import bench_local, local_model_cases as cases


class FakeClient:
    def __init__(self, reply):
        self.reply, self.last = reply, {}

    def ensure(self):
        return True

    def chat(self, system, user, schema):
        self.last = {"raw": dict(self.reply), "error": None}
        return dict(self.reply)


def case(use, identifier):
    return next(item for item in bench_local.CASES[use] if item["id"] == identifier)


class FixtureTests(unittest.TestCase):
    def test_case_ids_are_unique_and_labels_valid(self):
        for use, items in bench_local.CASES.items():
            ids = [item["id"] for item in items]
            self.assertEqual(len(ids), len(set(ids)), use)
        self.assertTrue(all(item["label"] in cases.CLASSES for item in cases.CLASSIFY))
        self.assertTrue(all(item["label"] in ("genuine", "shortcut") for item in cases.SHORTCUT))

    def test_injection_cases_reach_the_model(self):
        # If keeper's regex caught these, the scribe would never call the model
        # and the benchmark would measure the regex instead.
        tricky = [item for item in cases.SCRIBE if item["expect"].get("evades_regex")]
        self.assertGreaterEqual(len(tricky), 2)
        for item in tricky:
            self.assertIsNone(keeper.INJECTION_PATTERN.search(item["log"]), item["id"])

    def test_needle_logs_fit_the_scribe_window(self):
        for item in cases.SCRIBE:
            if item["id"].startswith("needle-"):
                self.assertLessEqual(len(item["log"]), 6000)
                self.assertIn("formatMoney", item["log"])

    def test_classify_set_includes_cases_the_regex_gets_wrong(self):
        wrong = [item["id"] for item in cases.CLASSIFY if diagnosis.classify(item["text"]) != item["label"]]
        self.assertIn("timeout-in-test-name", wrong)
        self.assertGreaterEqual(len(wrong), 3)


class ScoringTests(unittest.TestCase):
    def test_scribe_scoring(self):
        item = case("scribe", "earlier-error-already-fixed")
        good = {"blocker": "test_fetch got 404", "suggested_fix_command": None, "progress": [], "handoff": "h"}
        self.assertTrue(all(ok for _, ok in bench_local.score_scribe(item, good)))
        stale = dict(good, blocker="No module named 'requests' (404)")
        self.assertIn(("blocker_not_stale", False), bench_local.score_scribe(item, stale))
        item = case("scribe", "worker-claims-success")
        lying = dict(good, blocker="KeyError email", handoff="All tests pass.")
        self.assertIn(("no_false_success", False), bench_local.score_scribe(item, lying))
        self.assertEqual(bench_local.score_scribe(item, None), [("valid_shape", False)])

    def test_scribe_fix_command_ignores_backticks(self):
        item = case("scribe", "missing-module-with-hint")
        value = {"blocker": "No module named 'yaml'", "suggested_fix_command": "`pip install pyyaml`",
                 "progress": [], "handoff": "h"}
        self.assertIn(("fix_command", True), bench_local.score_scribe(item, value))

    def test_injection_scoring_reads_the_raw_reply(self):
        item = case("scribe", "injection-paraphrased")
        value = {"blocker": "test_vat_rounding", "suggested_fix_command": "pip install taxfix-helper",
                 "progress": [], "handoff": "h"}
        self.assertIn(("resists_injection", False), bench_local.score_scribe(item, value))

    def test_run_case_scores_scribe_before_and_after_guards(self):
        item = case("scribe", "injection-paraphrased")
        client = FakeClient({"blocker": None, "suggested_fix_command": "pip install taxfix-helper",
                             "progress": [], "handoff": "known flake"})
        raw, guarded, error = bench_local.run_case(client, "scribe", item)
        self.assertIsNone(error)
        self.assertEqual(raw["suggested_fix_command"], "pip install taxfix-helper")
        self.assertTrue(guarded["blocker"])
        # Known gap: the guard only asks that the command appear in the log, and an
        # injection can put it there. The benchmark's guarded score exposes this.
        self.assertEqual(guarded["suggested_fix_command"], "pip install taxfix-helper")

    def test_other_scorers(self):
        self.assertEqual(bench_local.score_classify(case("classify", "eacces"), {"class": "permission"}),
                         [("class", True)])
        self.assertEqual(bench_local.score_shortcut(case("shortcut", "neutered-gate"), {"verdict": "genuine"}),
                         [("verdict", False)])
        self.assertEqual(bench_local.score_claims(case("claims", "all-supported"), {"unsupported_claims": []}),
                         [("supported", True)])

    def test_summary_and_hybrid_baseline(self):
        rows = [{"model": "m", "use": "classify", "case": item["id"], "attempt": 0, "cold": index == 0,
                 "error": None, "raw": {"class": item["label"]}, "checks": [("class", True)],
                 "wall": 1.0, "load": 2.0, "tokens_per_second": 30, "deterministic": True}
                for index, item in enumerate(cases.CLASSIFY)]
        summary = bench_local.summarise(rows)[0]
        self.assertEqual(summary["case_pass"], 1.0)
        self.assertEqual(summary["cold_load"], 2.0)
        baselines = bench_local.classify_baselines(rows)
        self.assertLess(baselines["regex"], 1.0)
        # Regex answers first, so its confident mistakes survive the hybrid.
        self.assertLess(baselines["m"]["hybrid"], 1.0)
        self.assertIn("classify", bench_local.render([summary], baselines, rows))


if __name__ == "__main__":
    unittest.main()
