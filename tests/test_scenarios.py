import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from campaigns.phase2 import schema
from campaigns.prepare import load_config
from campaigns.scenarios import build_catalog, digest, SCENARIOS


class ScenarioTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.prepared = self.root / "prepared.sqlite"
        self.out = self.root / "scenarios"
        conn = sqlite3.connect(self.prepared)
        schema(conn)
        cfg = load_config(ROOT / "config/project.json")
        for key, value in (("config", cfg), ("source_version", "fixed-source-version")):
            conn.execute("INSERT INTO metadata VALUES (?,?)", (key, json.dumps(value)))
        # Separate original customers for every branch, including two eligible
        # customers so the consent action cannot reuse the selected example.
        for index, (scenario, reason, _, _) in enumerate(SCENARIOS, 1):
            customer_id = f"C{index:02d}"
            conn.execute("INSERT INTO customers VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         (customer_id, "Colombia", "Basic", 0 if scenario == "without_consent" else 1,
                          "Active", "2024-01-01", "2026-04-01" if scenario == "future_profile" else "2026-01-01",
                          "[]", "data/customers.csv", index + 1, str(index % 10) * 64))
            reasons = [reason] if reason else []
            has_account = scenario != "no_savings_account"
            product_id = f"P{index:02d}"
            conn.execute("INSERT INTO decisions VALUES (?,?,?,?,?,?)",
                         ("CMP", customer_id, int(not reasons), json.dumps(reasons),
                          json.dumps([product_id] if has_account and not reasons else []), "{}"))
            if has_account:
                flags = ["opening_before_customer_registration"] if scenario == "unreliable_account" else []
                conn.execute("INSERT INTO accounts VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                             (product_id, customer_id, "Cuenta Ahorro", "COP", "Closed" if scenario == "account_not_active" else "Active",
                              "2025-01-01", "2026-01-01", json.dumps(flags), "data/products.csv", index + 1, "a" * 64))
                known = 0 if scenario == "prior_activity_unknown" else 1
                recent = int(scenario == "recent_activity")
                conn.execute("INSERT INTO activity VALUES (?,?,?,?,?,?,?)",
                             (product_id, customer_id, known, "2026-02-25" if recent else "2026-01-05" if known else None,
                              recent, int(scenario == "unreliable_recent_activity"), 0))
                if known and not flags:
                    conn.execute("INSERT INTO activity_evidence VALUES (?,?,?,?,?,?,?,?)",
                                 (f"T{index:02d}", product_id, customer_id, "2026-02-25" if recent else "2026-01-05",
                                  "Deposit", "data/transactions/known.csv", index + 1, "b" * 64))
        conn.commit()
        conn.close()

    def tearDown(self):
        self.temp.cleanup()

    def test_distinct_original_profiles_and_fixed_snapshot_evidence(self):
        source_hash = digest(self.prepared)
        catalog = build_catalog(self.prepared, self.out)
        profiles = catalog["profiles"]
        self.assertEqual(13, len(profiles))
        self.assertEqual(13, len({p["customer_id"] for p in profiles}))
        self.assertEqual(13, catalog["coverage"]["covered_categories"])
        self.assertEqual(source_hash, digest(self.prepared))
        recent = next(p for p in profiles if p["scenario"] == "recent_activity")
        self.assertFalse(recent["expected_eligible"])
        self.assertEqual("T02", recent["latest_known_transactions"][0]["transaction_id"])
        self.assertEqual("2026-02-25", recent["expected"]["last_known_transaction"])
        future = next(p for p in profiles if p["scenario"] == "future_profile")
        self.assertFalse(future["expected"]["profile_available"])
        self.assertEqual(["profile_after_demo"], future["expected_reasons"])
        invalid = next(p for p in profiles if p["scenario"] == "unreliable_account")
        self.assertEqual([], invalid["accounts"])
        self.assertEqual(["opening_before_customer_registration"], invalid["quarantined_accounts"][0]["quality_flags"])
        optout = next(p for p in profiles if p["scenario"] == "confirmed_marketing_optout")
        self.assertTrue(optout["expected_eligible"])
        self.assertFalse(optout["expected"]["eligible_after_confirmed_action"])
        self.assertTrue(optout["expected"]["requires_confirmation"])
        self.assertEqual("service_state_only", optout["expected"]["overlay_scope"])
        self.assertTrue(all("es" in p["questions"] and "pt" in p["questions"] for p in profiles))

    def test_repeat_is_byte_identical_and_has_verified_artifacts(self):
        build_catalog(self.prepared, self.out)
        before = (self.out / "catalog.json").read_bytes()
        before_report = (self.out / "report.md").read_bytes()
        build_catalog(self.prepared, self.out)
        self.assertEqual(before, (self.out / "catalog.json").read_bytes())
        self.assertEqual(before_report, (self.out / "report.md").read_bytes())
        hashes = json.loads((self.out / "artifact_hashes.json").read_text(encoding="utf-8"))
        for name, value in hashes.items():
            self.assertEqual(value, digest(self.out / name))

    def test_sparse_source_reports_gaps_without_fabricating_people(self):
        conn = sqlite3.connect(self.prepared)
        conn.execute("DELETE FROM decisions WHERE customer_id!='C01'")
        conn.commit()
        conn.close()
        catalog = build_catalog(self.prepared, self.out)
        self.assertEqual(["C01"], [p["customer_id"] for p in catalog["profiles"]])
        self.assertEqual(1, catalog["coverage"]["covered_categories"])
        missing = {c["scenario"]: c for c in catalog["coverage"]["categories"]}
        self.assertEqual("not_available_in_source", missing["recent_activity"]["status"])
        self.assertEqual("no_unused_customer_available", missing["confirmed_marketing_optout"]["status"])

    def test_contradictory_decisions_are_rejected(self):
        conn = sqlite3.connect(self.prepared)
        conn.execute("UPDATE decisions SET eligible=1 WHERE customer_id='C02'")
        conn.commit()
        conn.close()
        with self.assertRaisesRegex(ValueError, "contradictorios"):
            build_catalog(self.prepared, self.out)
        self.assertFalse((self.out / "catalog.json").exists())

    def test_future_transaction_is_rejected_instead_of_used_as_latest(self):
        conn = sqlite3.connect(self.prepared)
        conn.execute("UPDATE activity_evidence SET transaction_date='2026-03-15' WHERE customer_id='C02'")
        conn.commit()
        conn.close()
        with self.assertRaisesRegex(ValueError, "posterior"):
            build_catalog(self.prepared, self.out)
        self.assertFalse((self.out / "catalog.json").exists())


if __name__ == "__main__":
    unittest.main()
