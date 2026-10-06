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
from campaigns.prepare import audit_and_prepare, load_config, CUSTOMER_FIELDS, CAMPAIGN_FIELDS, SEND_FIELDS, digest
from campaigns.phase2 import prepare, verify, PRODUCT_FIELDS, TRANSACTION_FIELDS
from campaigns.store import DataStore


def write(path, fields, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


class Phase2Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = load_config(ROOT / "config/project.json")
        self.out = self.root / "outputs/phase2"
        customers = [dict(customer_id=f"C{i}",country="Colombia",segment="Basic",accepts_marketing="True",
                          customer_status="Active",registration_date="2024-01-01",last_updated="2026-01-01") for i in range(1,8)]
        customers[5]["accepts_marketing"] = "False"
        write(self.root / "data/customers.csv", CUSTOMER_FIELDS, customers)
        campaign = dict(campaign_id="CMP-PHK8DTE4KLJO",campaign_name="Ahorro",description="Campaña de reactivation para Cuenta Ahorro",
                        campaign_type="Voice",campaign_objective="Reactivation",promoted_product="Cuenta Ahorro",target_country="Colombia",
                        target_segment="Basic",start_date="2026-02-22",end_date="2026-03-23",campaign_status="Completed")
        write(self.root / "data/marketing_campaigns.csv", CAMPAIGN_FIELDS, [campaign])
        send = dict(send_id="S1",campaign_id=campaign["campaign_id"],customer_id="C7",send_date="2026-02-27 12:00:00",
                    process_date="2026-02-28",send_channel="Voice",was_delivered="True",was_opened="",was_clicked="False",had_conversion="False",conversion_date="")
        write(self.root / "data/campaign_sends/one.csv", SEND_FIELDS, [send])
        self.products = [dict(product_id=f"P{i}",customer_id=f"C{i}",product_type="Cuenta Ahorro",currency="COP",product_status="Active",
                              opening_date="2025-01-01",last_updated="2026-01-01") for i in range(1,8)]
        self.products[4]["last_updated"] = "2026-03-02"
        write(self.root / "data/products.csv", PRODUCT_FIELDS + ["product_number","current_balance"],
              [{**p,"product_number":"private-account-number","current_balance":"9000"} for p in self.products])
        self.transactions = [self.tx(f"T{i}",f"P{i}",f"C{i}") for i in [1,2,4,5,6,7]]
        self.transactions += [self.tx("RECENT","P2","C2","2026-02-25"),
                              self.tx("BADRECENT","P4","C4","2026-02-25", "2026-02-24"),
                              self.tx("FUTURE","P1","C1","2026-03-15")]
        self.txpath = self.root / "data/transactions/one.csv"
        write(self.txpath, TRANSACTION_FIELDS, self.transactions)
        with contextlib.redirect_stdout(io.StringIO()):
            audit_and_prepare(self.root,self.root / "outputs/day1",load_config(ROOT / "config/day1.json"))

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def tx(key, product, customer, date="2026-01-05", processed=None):
        return dict(transaction_id=key,product_id=product,customer_id=customer,transaction_date=date,
                    process_date=processed or date,transaction_type="Deposit",transaction_status="Approved")

    def build(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return prepare(self.root,self.out,self.config)

    def test_selection_uses_account_and_activity_not_customer_inactive(self):
        summary = self.build()
        self.assertEqual(1,summary["selected_pairs"])
        rows = [json.loads(r) for r in (self.out / "audience.jsonl").read_text().splitlines()]
        self.assertEqual(["C1"],[r["customer_id"] for r in rows])
        self.assertEqual(["P1"],rows[0]["account_ids"])
        self.assertEqual("simulation_only",rows[0]["delivery_mode"])
        store = DataStore(self.out / "prepared.sqlite")
        self.assertTrue(store.get_customer("C1")["profile_available"])
        self.assertEqual(0,store.get_activity("C1")["recent_30d_count"])
        self.assertEqual(1,store.get_activity("C1")["valid_known_transactions"])
        self.assertIn("prior_activity_unknown",store.campaign_matches("C3")[0]["reasons"])
        self.assertIn("recent_activity_unreliable",store.campaign_matches("C4")[0]["reasons"])
        self.assertEqual([],store.get_accounts("C5"))
        self.assertIn("marketing_consent_not_true",store.campaign_matches("C6")[0]["reasons"])
        self.assertIn("frequency_limit_7d",store.campaign_matches("C7")[0]["reasons"])
        self.assertNotIn("current_balance",store.get_accounts("C1")[0])
        self.assertNotIn("product_number",store.get_accounts("C1")[0])
        self.assertEqual(1,verify(self.out,self.root)["selected_pairs_verified"])

    def test_repeat_and_late_arrival_changes_selection_without_accumulation(self):
        self.build()
        before = digest(self.out / "audience.jsonl")
        self.build()
        self.assertEqual(before,digest(self.out / "audience.jsonl"))
        self.transactions.append(self.tx("LATE","P1","C1","2026-02-26"))
        write(self.txpath,TRANSACTION_FIELDS,self.transactions)
        with self.assertRaisesRegex(ValueError,"Fuente modificada"):
            verify(self.out,self.root)
        self.assertEqual(0,self.build()["selected_pairs"])

    def test_duplicate_conflict_and_original_source_change_block_publish(self):
        self.build()
        before = digest(self.out / "prepared.sqlite")
        self.transactions.append(self.tx("T1","P1","C1","2026-01-06"))
        write(self.txpath,TRANSACTION_FIELDS,self.transactions)
        with self.assertRaisesRegex(ValueError,"duplicada contradictoria"):
            self.build()
        self.assertEqual(before,digest(self.out / "prepared.sqlite"))
        path = self.root / "data/customers.csv"
        path.write_text(path.read_text()+"\n",encoding="utf-8")
        with self.assertRaisesRegex(ValueError,"Fuente del día 1 modificada"):
            self.build()

    def test_identical_transaction_duplicate_not_counted_twice(self):
        self.transactions.append(copy.deepcopy(self.transactions[0]))
        write(self.txpath,TRANSACTION_FIELDS,self.transactions)
        self.build()
        self.assertEqual(1,DataStore(self.out / "prepared.sqlite").get_activity("C1")["valid_known_transactions"])

    def test_running_store_rejects_a_replaced_snapshot(self):
        self.build()
        store = DataStore(self.out / "prepared.sqlite")
        self.assertEqual(1, store.get_activity("C1")["valid_known_transactions"])
        self.transactions.append(self.tx("ARRIVED", "P1", "C1", "2026-02-26"))
        write(self.txpath, TRANSACTION_FIELDS, self.transactions)
        self.build()
        with self.assertRaisesRegex(RuntimeError, "reinicia el servicio"):
            store.get_accounts("C1")
        self.assertEqual(2, DataStore(self.out / "prepared.sqlite").get_activity("C1")["valid_known_transactions"])

    def test_added_or_removed_contact_partition_requires_day1_rebuild(self):
        self.build()
        before = digest(self.out / "prepared.sqlite")
        added = self.root / "data/campaign_sends/new.csv"
        send = dict(send_id="NEW-CONTACT", campaign_id="CMP-PHK8DTE4KLJO", customer_id="C1",
                    send_date="2026-02-28 12:00:00", process_date="2026-02-28", send_channel="Voice",
                    was_delivered="True", was_opened="", was_clicked="False", had_conversion="False", conversion_date="")
        write(added, SEND_FIELDS, [send])
        for operation in (lambda: verify(self.out, self.root), self.build):
            with self.assertRaisesRegex(ValueError, "Inventario de fuentes modificado"):
                operation()
        self.assertEqual(before, digest(self.out / "prepared.sqlite"))
        added.unlink()
        original = self.root / "data/campaign_sends/one.csv"
        original_bytes = original.read_bytes()
        original.unlink()
        for operation in (lambda: verify(self.out, self.root), self.build):
            with self.assertRaisesRegex(ValueError, "Inventario de fuentes modificado"):
                operation()
        self.assertEqual(before, digest(self.out / "prepared.sqlite"))
        original.write_bytes(original_bytes)
        # The new contact becomes effective only after rebuilding its upstream
        # projection: cached absence of contact is never considered fresh.
        write(added, SEND_FIELDS, [send])
        with contextlib.redirect_stdout(io.StringIO()):
            audit_and_prepare(self.root, self.root / "outputs/day1", load_config(ROOT / "config/day1.json"))
        self.assertEqual(0, self.build()["selected_pairs"])

    def test_added_or_removed_transaction_partition_is_detected(self):
        self.build()
        added = self.root / "data/transactions/new.csv"
        write(added, TRANSACTION_FIELDS, [self.tx("NEW", "P1", "C1", "2026-02-26")])
        with self.assertRaisesRegex(ValueError, "Inventario de fuentes modificado"):
            verify(self.out, self.root)
        self.assertEqual(0, self.build()["selected_pairs"])
        added.unlink()
        with self.assertRaisesRegex(ValueError, "Inventario de fuentes modificado"):
            verify(self.out, self.root)
        self.assertEqual(1, self.build()["selected_pairs"])

    def test_global_product_key_conflict_outside_scope_blocks_publish_in_both_orders(self):
        self.build()
        before = digest(self.out / "prepared.sqlite")
        conflict = copy.deepcopy(self.products[0])
        conflict["product_type"] = "Tarjeta Crédito"
        for rows in (self.products + [conflict], [conflict] + self.products):
            with self.subTest(conflict_first=rows[0] == conflict):
                write(self.root / "data/products.csv", PRODUCT_FIELDS, rows)
                with self.assertRaisesRegex(ValueError, "Producto duplicado contradictorio"):
                    self.build()
                self.assertEqual(before, digest(self.out / "prepared.sqlite"))

    def test_transaction_key_conflict_outside_scope_blocks_publish_in_both_orders(self):
        self.build()
        before = digest(self.out / "prepared.sqlite")
        conflict = self.tx("T1", "P-OUTSIDE", "C1", "2026-02-26")
        for rows in (self.transactions + [conflict], [conflict] + self.transactions):
            with self.subTest(conflict_first=rows[0] == conflict):
                write(self.txpath, TRANSACTION_FIELDS, rows)
                with self.assertRaisesRegex(ValueError, "Transacción duplicada contradictoria fuera de alcance"):
                    self.build()
                self.assertEqual(before, digest(self.out / "prepared.sqlite"))

    def test_unrelated_transaction_conflicts_do_not_claim_scoped_evidence(self):
        # Two unrelated keys need not occupy the scoped hash table. They are
        # outside the projection and cannot change or become its evidence.
        unrelated = [self.tx("OUT", "P-OUTSIDE", "C-OUTSIDE", "2026-02-25"),
                     self.tx("OUT", "P-OUTSIDE", "C-OUTSIDE", "2026-02-26")]
        write(self.txpath, TRANSACTION_FIELDS, self.transactions + unrelated)
        self.assertEqual(1, self.build()["selected_pairs"])
        store = DataStore(self.out / "prepared.sqlite")
        self.assertEqual(1, store.get_activity("C1")["valid_known_transactions"])
        self.assertNotIn("OUT", [row["transaction_id"] for row in store.get_activity("C1")["evidence"]])


if __name__ == "__main__":
    unittest.main()
