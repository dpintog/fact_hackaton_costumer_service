"""Rubric tests: material failures and denominators without fabricating success."""

import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from campaigns.evaluation import (check_fixture_facts, check_step, classification_metrics, FixtureStore,
                                  load_cases, percentile, summarize,
                                  validate_split_isolation)


def row(**changes):
    result = dict(case_id="c", language="es", segment="Basic", in_scope=True,
                  expected_transfer=False, intent_eval=True, expected_intent="account_info",
                  predicted_intent="account_info", final_status="resolution", transferred=False,
                  automation_attempted=True, passed=True, failures=[], unsafe_reasons=[], latency_ms=10.0)
    result.update(changes)
    return result


class EvaluationTests(unittest.TestCase):
    def test_frozen_reserve_is_grouped_bilingual_and_unchanged(self):
        cases = load_cases(ROOT / "datasets/evaluation_cases.jsonl", ROOT / "datasets/evaluation_manifest.json")
        self.assertEqual(48, len(cases))
        self.assertEqual(24, len({case["family_id"] for case in cases}))
        with tempfile.TemporaryDirectory() as temp:
            changed = Path(temp) / "cases.jsonl"
            changed.write_text((ROOT / "datasets/evaluation_cases.jsonl").read_text(encoding="utf-8") + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "frozen"):
                load_cases(changed, ROOT / "datasets/evaluation_manifest.json")

    def test_family_and_exact_text_leakage_are_rejected(self):
        cases = load_cases(ROOT / "datasets/evaluation_cases.jsonl")
        with tempfile.TemporaryDirectory() as temp:
            training = Path(temp) / "train.jsonl"
            training.write_text(json.dumps(dict(family_id=cases[0]["family_id"], text="different")) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "family leakage"):
                validate_split_isolation(cases, training)
            training.write_text(json.dumps(dict(family_id="independent", text=cases[0]["steps"][0]["message"])) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "text leakage"):
                validate_split_isolation(cases, training)

    def test_denominators_do_not_confuse_containment_and_resolution(self):
        metrics = summarize([row(), row(case_id="unclear", passed=True, final_status="clarify", automation_attempted=False),
                             row(case_id="needed", expected_transfer=True, final_status="handoff_created", transferred=True)])
        self.assertEqual(1, metrics["safe_automated_resolution"]["count"])
        self.assertEqual(3, metrics["safe_automated_resolution"]["denominator"])
        self.assertEqual(2, metrics["containment"]["count"])
        self.assertEqual(2, metrics["automation_attempted"]["count"])
        self.assertEqual(1, metrics["escalation"]["correct_count"])

    def test_zero_resolution_and_empty_samples_have_undefined_rates_costs(self):
        metrics = summarize([row(final_status="clarify", automation_attempted=False)])
        self.assertIsNone(metrics["cost"]["external_api_per_safe_resolution_usd"])
        self.assertEqual(1, metrics["cost"]["evaluated_case_n"])
        self.assertEqual(0, metrics["cost"]["automation_attempt_n"])
        self.assertEqual(0.0, metrics["cost"]["external_api_per_evaluated_case_usd"])
        self.assertIsNone(metrics["cost"]["external_api_per_automation_attempt_usd"])
        self.assertIsNone(summarize([])["success_rate"])
        self.assertIsNone(classification_metrics([])["accuracy"])
        self.assertAlmostEqual(20.0, percentile([10, 20, 30], 0.5))
        self.assertAlmostEqual(29.0, percentile([10, 20, 30], 0.95))

    def test_persistence_and_confirmation_failures_are_unsafe(self):
        response = dict(status="handoff_created", request_id="fake", facts={})
        checked = check_step(dict(status_in=["handoff_created"], request_count=0), response, request_count=1, request=None)
        self.assertIn("unconfirmed_or_duplicate_write", checked["unsafe_reasons"])
        self.assertIn("claimed_action_without_verified_persistence", checked["unsafe_reasons"])
        self.assertFalse(checked["passed"])

    def test_foreign_disclosure_and_missing_evidence_are_failures(self):
        response = dict(status="resolution", facts={"accounts": [{"product_id": "EVAL-OTHER-PRODUCT"}]}, evidence=[])
        checked = check_step(dict(status_in=["resolution"], require_evidence=True, no_private=True), response)
        self.assertIn("other_customer_disclosure", checked["unsafe_reasons"])
        self.assertIn("missing_grounding_evidence", checked["failures"])

    def test_fixture_invalid_data_is_quarantined_and_raw_fixture_retained(self):
        store = FixtureStore(scenario="invalid_product")
        self.assertEqual([], store.get_accounts("EVAL-SELF"))
        self.assertTrue(store.source_snapshot()["accounts"]["EVAL-SELF"][0]["quality_flags"])
        self.assertIsNone(FixtureStore(scenario="missing_customer").get_customer("EVAL-SELF"))
        self.assertEqual([], FixtureStore(scenario="missing_campaign").list_campaigns())

    def test_factual_rubric_checks_sources_and_wrong_information(self):
        case = dict(intent="account_info")
        step = dict(require_evidence=True)
        store = FixtureStore()
        checked = check_fixture_facts(case, step, dict(status="resolution", facts=dict(
            accounts=[dict(product_id="EVAL-SELF-PRODUCT", currency="USD")], coherent_account_count=99)), store)
        self.assertIn("materially_incorrect_account_fact:currency", checked["unsafe_reasons"])
        self.assertIn("materially_incorrect_account_count", checked["unsafe_reasons"])
        checked = check_fixture_facts(case, step, dict(status="resolution", facts=dict(campaigns=[])), store)
        self.assertIn("wrong_information_for_reference_intent:accounts", checked["failures"])


if __name__ == "__main__":
    unittest.main()
