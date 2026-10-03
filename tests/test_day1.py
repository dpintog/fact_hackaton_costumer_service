"""Pruebas de límites temporales, permisos y reconstrucción con fixtures pequeñas."""

import contextlib
import copy
import csv
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from campaigns.policy import (campaign_reasons, contact_count, customer_reasons,
                              permitted)
from campaigns.prepare import (CAMPAIGN_FIELDS, CUSTOMER_FIELDS, SEND_FIELDS,
                               audit_and_prepare, connect, digest, load_config,
                               reports, seal_artifacts, select, verify)


def campaign(identifier="CMP-PHK8DTE4KLJO", **changes):
    return dict(campaign_id=identifier, campaign_name="Reactivación ahorro",
                description="Campaña de reactivation para Cuenta Ahorro",
                campaign_type="Voice", campaign_objective="Reactivation",
                promoted_product="Cuenta Ahorro", target_segment="Basic",
                target_country="Colombia", start_date="2026-02-22",
                end_date="2026-03-23", campaign_status="Completed",
                quality_flags=[], **changes)


def customer(identifier="C001"):
    return dict(customer_id=identifier, country="Colombia", segment="Basic",
                accepts_marketing="True", customer_status="Inactive",
                registration_date="2025-01-01", last_updated="2026-02-01",
                quality_flags=[])


def send(identifier="S001", **changes):
    row = dict(send_id=identifier, send_date="2026-02-27 12:00:00",
               process_date="2026-02-28", campaign_id="CMP-PHK8DTE4KLJO",
               customer_id="C002", send_channel="Voice", was_delivered="True",
               was_opened="False", was_clicked="False", had_conversion="False",
               conversion_date="", quality_flags=[])
    row.update(changes)
    return row


def write_csv(path, fields, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config(ROOT / "config/day1.json")
        self.campaign = campaign()

    def test_missing_targets_are_not_wildcards(self):
        for field, reason in [("target_country", "target_country_missing_or_unknown"),
                              ("target_segment", "target_segment_missing"),
                              ("description", "description_missing")]:
            changed = {**self.campaign, field: ""}
            self.assertIn(reason, campaign_reasons(changed, self.config))

    def test_completed_is_explicit_replay_and_original_status_is_preserved(self):
        self.assertEqual([], campaign_reasons(self.campaign, self.config))
        self.assertEqual("Completed", self.campaign["campaign_status"])
        self.assertIn("completed_campaign_not_authorized_for_replay",
                      campaign_reasons({**self.campaign, "campaign_id": "OTHER"}, self.config))
        self.assertIn("campaign_paused", campaign_reasons(
            {**self.campaign, "campaign_status": "Paused"}, self.config))

    def test_campaign_date_boundaries_inclusive(self):
        for date in ["2026-02-22T00:00:00", "2026-03-23T23:59:59"]:
            self.assertEqual([], campaign_reasons(self.campaign, {**self.config, "demo_at": date}))
        self.assertIn("outside_campaign_window", campaign_reasons(
            self.campaign, {**self.config, "demo_at": "2026-03-24T00:00:00"}))

    def test_consent_customer_state_and_time(self):
        base = customer()
        self.assertEqual([], customer_reasons(base, self.campaign, self.config, {}))
        for value in ["False", "", None, "yes"]:
            self.assertIn("marketing_consent_not_true", customer_reasons(
                {**base, "accepts_marketing": value}, self.campaign, self.config, {}))
        for state in ["Active", "Suspended", "Closed"]:
            self.assertIn("customer_status_not_allowed_for_objective", customer_reasons(
                {**base, "customer_status": state}, self.campaign, self.config, {}))
        for date, reason in [("2026-03-02", "profile_after_demo"),
                             ("2027-01-01", "profile_after_dataset_cutoff"),
                             ("2024-01-01", "profile_timestamp_invalid")]:
            self.assertIn(reason, customer_reasons(
                {**base, "last_updated": date}, self.campaign, self.config, {}))

    def test_frequency_only_known_deliveries_and_inclusive_boundary(self):
        rows = [send(), send("S002", send_date="2026-03-02"),
                send("S003", process_date="2026-03-02"),
                send("S004", was_delivered="False"),
                send("S005", send_date="2026-02-22 12:00:00"),
                send("S006", send_date="2026-02-22 11:59:59")]
        self.assertEqual(2, contact_count(rows, self.config, 7))
        self.assertIn("frequency_limit_7d", customer_reasons(
            customer(), self.campaign, self.config, {7: 1}))
        self.assertIn("frequency_limit_30d", customer_reasons(
            customer(), self.campaign, self.config, {30: 3}))
        # Una etiqueta de conversión futura o inválida no cambia la frecuencia.
        row = send(quality_flags=["conversion_not_after_send"], contact_quality_flags=[])
        self.assertEqual(1, contact_count([row], self.config, 7))

    def test_permissions_require_trusted_session_ownership_and_confirmation(self):
        principal = dict(authenticated=True, expired=False, role="customer", customer_id="C001")
        self.assertTrue(permitted(principal, "view_customer_context", "C001"))
        self.assertFalse(permitted(principal, "view_customer_context", "C002"))
        self.assertFalse(permitted(principal, "request_advisor", "C001"))
        self.assertTrue(permitted(principal, "request_advisor", "C001", confirmed=True))
        self.assertFalse(permitted(principal, "request_advisor", "C001", confirmed="false"))
        for changes in [dict(authenticated=False), dict(authenticated="True"), dict(expired=True)]:
            self.assertFalse(permitted({**principal, **changes}, "view_customer_context", "C001"))
        self.assertFalse(permitted({"customer_id": "C001"}, "view_customer_context", "C001"))
        self.assertFalse(permitted(principal, "send_campaign", "C001", confirmed=True))
        advisor = dict(authenticated=True, expired=False, role="advisor")
        self.assertTrue(permitted(advisor, "view_customer_context", "C001", ["C001"]))
        self.assertFalse(permitted(advisor, "view_customer_context", "C002", ["C001"]))
        operator = dict(authenticated=True, expired=False, role="operator")
        self.assertTrue(permitted(operator, "view_audience"))
        self.assertFalse(permitted(operator, "view_customer_context", "C001"))


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.out = self.root / "outputs/day1"
        self.config = copy.deepcopy(load_config(ROOT / "config/day1.json"))
        customers = [customer(f"C00{i}") for i in range(1, 7)]
        customers[2]["accepts_marketing"] = "False"
        customers[3]["last_updated"] = "2026-03-02"
        customers += [customer("C001"), {**customer("C005"), "segment": "Premium"}]
        write_csv(self.root / "data/customers.csv", CUSTOMER_FIELDS + ["email"],
                  [{**c, "email": "private@example.test"} for c in customers])
        write_csv(self.root / "data/marketing_campaigns.csv", CAMPAIGN_FIELDS,
                  [campaign(), {**campaign("PAUSED"), "campaign_status": "Paused"}])
        self.send_path = self.root / "data/campaign_sends/part.csv"
        self.sends = [send(was_opened=""), send("ORPHAN", customer_id="NOT_FOUND"),
                      send("BAD_CONVERSION", customer_id="C006", had_conversion="True",
                           conversion_date="2026-02-26")]
        write_csv(self.send_path, SEND_FIELDS, self.sends)

    def tearDown(self):
        self.temp.cleanup()

    def build(self):
        with contextlib.redirect_stdout(io.StringIO()):
            quality = audit_and_prepare(self.root, self.out, self.config)
        summary = select(self.out, self.config)
        reports(self.out, quality, summary)
        seal_artifacts(self.out)
        return quality, summary

    def audience(self):
        return [json.loads(line) for line in (self.out / "baseline_audience.jsonl").read_text(encoding="utf-8").splitlines()]

    def test_pipeline_selects_complete_expected_audience_and_preserves_sources(self):
        original = digest(self.root / "data/customers.csv")
        quality, summary = self.build()
        self.assertEqual(["C001"], [r["customer_id"] for r in self.audience()])
        self.assertEqual(1, summary["audience_pairs"])
        self.assertEqual(1, quality["core_tables"]["customers"]["duplicate_identical"])
        self.assertEqual(1, quality["core_tables"]["customers"]["duplicate_conflicting"])
        self.assertEqual(1, quality["core_tables"]["sends"]["validation_issues"]["customer_foreign_key_missing"])
        self.assertEqual(original, digest(self.root / "data/customers.csv"))
        with contextlib.closing(connect(self.out / "prepared.sqlite")) as conn:
            fields = [r[1] for r in conn.execute("PRAGMA table_info(customers)")]
            self.assertNotIn("email", fields)
            optional = conn.execute("SELECT was_opened,quality_flags FROM sends WHERE send_id='S001'").fetchone()
            self.assertIsNone(optional["was_opened"])
            self.assertEqual("[]", optional["quality_flags"])
            row = conn.execute("SELECT * FROM sends WHERE send_id='BAD_CONVERSION'").fetchone()
            self.assertIn("conversion_not_after_send", row["quality_flags"])
            self.assertEqual("[]", row["contact_quality_flags"])
        self.assertTrue(verify(self.root, self.out)["all_selected_records_satisfy_rules"])
        self.assertNotIn("private@example.test", (self.out / "baseline_audience.jsonl").read_text())
        self.assertIn("Campos ausentes", (self.out / "quality_report.md").read_text(encoding="utf-8"))
        (self.out / "catalog.json").write_text("[]", encoding="utf-8")
        with self.assertRaisesRegex(AssertionError, "artefacto cambió"):
            verify(self.root, self.out)

    def test_rebuild_is_idempotent_and_incorporates_late_arrival(self):
        self.build()
        original = digest(self.out / "baseline_audience.jsonl")
        self.build()
        self.assertEqual(original, digest(self.out / "baseline_audience.jsonl"))
        self.sends.append(send("LATE", customer_id="C001"))
        write_csv(self.send_path, SEND_FIELDS, self.sends)
        with self.assertRaisesRegex(AssertionError, "fuente cambió"):
            verify(self.root, self.out)
        self.build()
        self.assertEqual([], self.audience())
        with contextlib.closing(connect(self.out / "prepared.sqlite")) as conn:
            self.assertEqual(4, conn.execute("SELECT count(*) FROM sends").fetchone()[0])

    def test_missing_column_and_changed_policy_require_failure_or_rebuild(self):
        write_csv(self.root / "data/customers.csv", ["customer_id"], [customer()])
        with self.assertRaisesRegex(ValueError, "Contrato"):
            self.build()
        write_csv(self.root / "data/customers.csv", CUSTOMER_FIELDS, [customer()])
        self.build()
        with self.assertRaisesRegex(ValueError, "configuración cambió"):
            select(self.out, {**self.config, "demo_at": "2026-03-02T12:00:00"})


if __name__ == "__main__":
    unittest.main()
