"""Operator workflows verify complete audiences and persisted safe outcomes."""

from contextlib import closing
import csv
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from campaigns.operations import CampaignOperations
from campaigns.phase2 import schema
from campaigns.prepare import load_config
from campaigns.scenarios import build_catalog, digest
from campaigns.service import ChatService
from campaigns.server import make_server
from campaigns.store import DataStore


CAMPAIGN = "CMP-PHK8DTE4KLJO"


class OptoutRouter:
    def predict(self, _text):
        return dict(intent="marketing_optout", confidence=1, ambiguous=False)


class OperationsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.prepared = self.directory / "prepared.sqlite"
        self.state = self.directory / "state.sqlite"
        conn = sqlite3.connect(self.prepared)
        schema(conn)
        cfg = load_config(ROOT / "config/project.json")
        conn.executemany("INSERT INTO metadata VALUES (?,?)", (("config", json.dumps(cfg)), ("source_version", json.dumps("v1"))))
        campaign = (CAMPAIGN, "Ahorro", "Campaña histórica de ahorro", "Voice", "Reactivation", "Cuenta Ahorro",
                    "Basic", "Colombia", "2026-02-22", "2026-03-23", "Completed", "[]", "data/marketing_campaigns.csv", 2, "d" * 64)
        conn.execute("INSERT INTO campaigns VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", campaign)
        paused = ("CMP-PAUSED", *campaign[1:10], "Paused", *campaign[11:])
        conn.execute("INSERT INTO campaigns VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", paused)
        for index in range(130):
            key = f"C{index:03d}"
            reason = [] if index < 125 else ["target_segment_mismatch"] if index < 128 else ["marketing_consent_not_true"]
            conn.execute("INSERT INTO customers VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         (key, "Colombia", "Basic", int(index < 128), "Active", "2024-01-01", "2026-01-01", "[]",
                          "data/customers.csv", index + 2, "e" * 64))
            conn.execute("INSERT INTO decisions VALUES (?,?,?,?,?,?)",
                         (CAMPAIGN, key, int(not reason), json.dumps(reason), "[]", "{}"))
        conn.commit()
        conn.close()
        self.store = DataStore(self.prepared)
        self.service = ChatService(self.store, OptoutRouter(), self.state)
        self.operations = CampaignOperations(self.service)
        self.operator = self.service.issue_test_session(None, role="operator")
        self.other_operator = self.service.issue_test_session("OTHER", role="operator")
        self.customer = self.service.issue_test_session("C000")
        self.advisor = self.service.issue_test_session(None, role="advisor", assigned_customer_ids=["C000"])

    def tearDown(self):
        self.temp.cleanup()

    def count_state(self, table):
        with closing(sqlite3.connect(self.state)) as conn, conn:
            return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def confirm(self, pending, token=None, confirmed=True, key=None):
        return self.operations.confirm(token or self.operator, pending["batch_action_id"],
                                       key or pending["idempotency_key"], confirmed)

    def optout(self, key="C000"):
        token = self.customer if key == "C000" else self.service.issue_test_session(key)
        pending = self.service.chat(token, "No quiero recibir publicidad")
        self.assertEqual("confirmation_pending", pending["status"])
        result = self.service.chat(token, "Confirmar", pending["conversation_id"], confirmed=True,
                                   idempotency_key=pending["pending_action"]["idempotency_key"])
        self.assertEqual("resolution", result["status"])
        self.assertFalse(result["facts"]["accepts_marketing"])

    def test_operator_permissions_and_denial_before_selection_reads(self):
        original = self.store.selection
        def forbidden_read(**_kwargs):
            raise AssertionError("Unauthorized selection reached the datastore")
        self.store.selection = forbidden_read
        tokens = ("C000", self.customer, self.advisor, self.service.issue_test_session(None, role="operator", expired=True))
        for token in tokens:
            with self.subTest(token_type=type(token).__name__):
                self.assertIsNone(self.operations.selection(token))
                self.assertEqual("denied", self.operations.preview(token, CAMPAIGN)["status"])
                self.assertIsNone(self.operations.scenarios(token))
        self.assertEqual(0, self.count_state("batch_actions"))
        self.store.selection = original
        self.assertEqual(125, self.operations.selection(self.operator)["total"])

    def test_confirmation_is_literal_true_and_bound_to_owner_and_preview(self):
        pending = self.operations.preview(self.operator, CAMPAIGN)
        for value in (False, None, 0, 1, "true", "false", [], {}):
            with self.subTest(confirmed=value):
                self.assertEqual("denied", self.confirm(pending, confirmed=value)["status"])
        self.assertEqual("denied", self.confirm(pending, token=self.customer)["status"])
        self.assertEqual("denied", self.confirm(pending, token=self.other_operator)["status"])
        self.assertEqual("denied", self.confirm(pending, key="wrong-key")["status"])
        self.assertEqual("denied", self.operations.confirm(self.operator, "missing-action", "unknown", True)["status"])
        self.assertEqual(0, self.count_state("campaign_batches"))
        self.assertEqual("prepared", self.confirm(pending)["status"])

    def test_complete_batch_over_100_is_verified_and_idempotent(self):
        pending = self.operations.preview(self.operator, CAMPAIGN)
        self.assertEqual(125, pending["audience_count"])
        result = self.confirm(pending)
        self.assertEqual("prepared", result["status"])
        self.assertEqual(125, result["count"])
        self.assertEqual(0, result["external_deliveries"])
        batch = self.operations.get_batch(self.operator, result["batch_id"], include_members=True)
        self.assertEqual(125, len(batch["rows"]))
        self.assertEqual("C124", batch["rows"][-1]["customer_id"])
        self.assertNotIn("C125", {r["customer_id"] for r in batch["rows"]})
        retry = self.confirm(pending)
        self.assertEqual(result["batch_id"], retry["batch_id"])
        self.assertTrue(retry["idempotent_replay"])
        self.assertEqual(1, self.count_state("campaign_batches"))
        self.assertEqual(125, self.count_state("batch_members"))
        with closing(sqlite3.connect(self.state)) as conn, conn:
            audit = conn.execute("SELECT action,outcome,reference FROM audit").fetchall()
        self.assertEqual([("prepare_campaign_batch", "verified", result["batch_id"])], audit)
        self.assertIsNone(self.operations.get_batch(self.other_operator, result["batch_id"], True))
        self.assertIsNone(self.operations.get_batch(self.customer, result["batch_id"], True))

    def test_faults_rollback_all_members_audit_and_allow_retry(self):
        for stage in ("before_batch_write", "before_batch_readback"):
            with self.subTest(stage=stage):
                pending = self.operations.preview(self.operator, CAMPAIGN)
                def fail(actual, expected=stage):
                    if actual == expected:
                        raise RuntimeError("injected trusted failure")
                self.service.fault_injector = fail
                result = self.confirm(pending)
                self.assertEqual("tool_error", result["status"])
                self.assertIsNone(result["batch_id"])
                self.assertEqual(0, self.count_state("campaign_batches"))
                self.assertEqual(0, self.count_state("batch_members"))
                self.assertEqual(0, self.count_state("audit"))
                self.service.fault_injector = None
        result = self.confirm(pending)
        self.assertEqual("prepared", result["status"])
        self.assertEqual(125, self.count_state("batch_members"))

    def test_readback_detects_missing_persisted_member_and_rolls_back(self):
        with closing(sqlite3.connect(self.state)) as conn, conn:
            conn.execute("""CREATE TRIGGER ignore_member BEFORE INSERT ON batch_members
                            WHEN NEW.customer_id='C000' BEGIN SELECT RAISE(IGNORE); END""")
        pending = self.operations.preview(self.operator, CAMPAIGN)
        self.assertEqual("tool_error", self.confirm(pending)["status"])
        self.assertEqual(0, self.count_state("campaign_batches"))
        self.assertEqual(0, self.count_state("batch_members"))
        self.assertEqual(0, self.count_state("audit"))
        with closing(sqlite3.connect(self.state)) as conn, conn:
            conn.execute("DROP TRIGGER ignore_member")
        self.assertEqual("prepared", self.confirm(pending)["status"])

    def test_readback_detects_wrong_recipient_even_when_count_is_unchanged(self):
        # A corrupted tool/database result can contain the right number of rows
        # while substituting a recipient who did not consent to advertising.
        with closing(sqlite3.connect(self.state)) as conn, conn:
            conn.execute("""CREATE TRIGGER substitute_recipient AFTER INSERT ON batch_members
                            WHEN NEW.customer_id='C000' BEGIN
                            UPDATE batch_members SET customer_id='C129'
                            WHERE batch_id=NEW.batch_id AND customer_id='C000'; END""")
        pending = self.operations.preview(self.operator, CAMPAIGN)
        self.assertEqual("tool_error", self.confirm(pending)["status"])
        self.assertEqual(0, self.count_state("campaign_batches"))
        self.assertEqual(0, self.count_state("batch_members"))
        self.assertEqual(0, self.count_state("audit"))
        with closing(sqlite3.connect(self.state)) as conn, conn:
            conn.execute("DROP TRIGGER substitute_recipient")
        self.assertEqual("prepared", self.confirm(pending)["status"])

    def test_result_construction_failure_rolls_back_before_commit(self):
        pending = self.operations.preview(self.operator, CAMPAIGN)
        original = self.operations._batch_result
        def fail(*_args, **_kwargs):
            raise RuntimeError("readback result failed")
        self.operations._batch_result = fail
        self.assertEqual("tool_error", self.confirm(pending)["status"])
        self.assertEqual(0, self.count_state("campaign_batches"))
        self.assertEqual(0, self.count_state("batch_members"))
        self.assertEqual(0, self.count_state("audit"))
        self.operations._batch_result = original
        self.assertEqual("prepared", self.confirm(pending)["status"])

    def test_optout_after_preview_invalidates_confirmation_and_fresh_preview_excludes(self):
        before = digest(self.prepared)
        pending = self.operations.preview(self.operator, CAMPAIGN)
        self.optout()
        self.assertEqual("changed", self.confirm(pending)["status"])
        self.assertEqual(0, self.count_state("campaign_batches"))
        self.assertEqual(124, self.operations.selection(self.operator)["total"])
        fresh = self.operations.preview(self.operator, CAMPAIGN)
        self.assertEqual(124, fresh["audience_count"])
        result = self.confirm(fresh)
        batch = self.operations.get_batch(self.operator, result["batch_id"], True)
        self.assertNotIn("C000", {r["customer_id"] for r in batch["rows"]})
        self.assertEqual(before, digest(self.prepared))

    def test_optout_after_batch_requires_refresh_and_hides_obsolete_members(self):
        pending = self.operations.preview(self.operator, CAMPAIGN)
        result = self.confirm(pending)
        self.optout()
        stale = self.operations.get_batch(self.operator, result["batch_id"], True)
        self.assertEqual("needs_refresh", stale["status"])
        self.assertNotIn("rows", stale)
        self.assertEqual(0, stale["external_deliveries"])
        replay = self.confirm(pending)
        self.assertEqual("needs_refresh", replay["status"])
        self.assertEqual(result["batch_id"], replay["batch_id"])
        self.assertEqual(1, self.count_state("campaign_batches"))
        refreshed = self.confirm(self.operations.preview(self.operator, CAMPAIGN))
        self.assertEqual("prepared", refreshed["status"])
        self.assertEqual(124, refreshed["count"])

    def test_expired_action_and_expired_session_cannot_write(self):
        pending = self.operations.preview(self.operator, CAMPAIGN)
        expired_token = self.service.issue_test_session(None, role="operator", expired=True)
        self.assertEqual("denied", self.confirm(pending, token=expired_token)["status"])
        with closing(sqlite3.connect(self.state)) as conn, conn:
            conn.execute("UPDATE batch_actions SET expires_at='2000-01-01T00:00:00+00:00'")
        self.assertEqual("expired", self.confirm(pending)["status"])
        self.assertEqual(0, self.count_state("campaign_batches"))

    def test_new_source_version_invalidates_preview_and_existing_batch(self):
        prepared_batch = self.confirm(self.operations.preview(self.operator, CAMPAIGN))
        pending = self.operations.preview(self.operator, CAMPAIGN)
        with closing(sqlite3.connect(self.prepared)) as conn, conn:
            conn.execute("UPDATE metadata SET value=? WHERE key='source_version'", (json.dumps("v2"),))
        # A service that was not restarted refuses the changed projection.
        with self.assertRaisesRegex(RuntimeError, "reinicia el servicio"):
            self.operations.selection(self.operator)
        self.service.store = DataStore(self.prepared)
        self.assertEqual("changed", self.confirm(pending)["status"])
        stale = self.operations.get_batch(self.operator, prepared_batch["batch_id"], True)
        self.assertEqual("needs_refresh", stale["status"])
        self.assertNotIn("rows", stale)
        self.assertEqual(1, self.count_state("campaign_batches"))

    def test_changed_policy_invalidates_preview_and_existing_batch_without_source_change(self):
        prepared_batch = self.confirm(self.operations.preview(self.operator, CAMPAIGN))
        pending = self.operations.preview(self.operator, CAMPAIGN)
        config = dict(self.store.config, policy_version="different-policy")
        with closing(sqlite3.connect(self.prepared)) as conn, conn:
            conn.execute("UPDATE metadata SET value=? WHERE key='config'", (json.dumps(config),))
        self.service.store = DataStore(self.prepared)
        self.assertEqual("v1", self.service.store.source_version)
        self.assertEqual("changed", self.confirm(pending)["status"])
        stale = self.operations.get_batch(self.operator, prepared_batch["batch_id"], True)
        self.assertEqual("needs_refresh", stale["status"])
        self.assertNotIn("rows", stale)
        self.assertEqual(1, self.count_state("campaign_batches"))

    def test_sql_selection_overlay_filters_pagination_and_no_source_mutation(self):
        before = digest(self.prepared)
        overlay = {"C000", "C128"}
        first = self.store.selection(limit=100, excluded_customer_ids=overlay)
        second = self.store.selection(offset=100, limit=100, excluded_customer_ids=overlay)
        self.assertEqual(124, first["total"])
        self.assertEqual(100, len(first["rows"]))
        self.assertEqual(24, len(second["rows"]))
        all_ids = [r["customer_id"] for r in first["rows"] + second["rows"]]
        self.assertEqual(len(all_ids), len(set(all_ids)))
        self.assertNotIn("C000", all_ids)
        summary = first["summary"]
        self.assertEqual((130, 124, 6, 2), tuple(summary[k] for k in ("evaluated_pairs", "eligible_pairs", "excluded_pairs", "local_optouts")))
        excluded = self.store.selection(decision="excluded", reason="local_marketing_optout", excluded_customer_ids=overlay)
        self.assertEqual(["C000", "C128"], [r["customer_id"] for r in excluded["rows"]])
        self.assertTrue(all(not r["eligible"] and "local_marketing_optout" in r["reasons"] for r in excluded["rows"]))
        segment = self.store.selection(decision="all", reason="target_segment_mismatch")
        self.assertEqual(3, segment["total"])
        self.assertEqual(0, self.store.selection(decision="all", reason='x" OR 1=1 --')["total"])
        self.assertEqual(125, len(self.store.eligible_pairs(CAMPAIGN)))
        self.assertEqual(124, len(self.store.eligible_pairs(CAMPAIGN, overlay)))
        self.assertEqual(before, digest(self.prepared))
        for invalid in ({"decision": "unknown"}, {"campaign_id": "unknown"}, {"offset": -1}, {"limit": 0}, {"limit": 101}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.store.selection(**invalid)
        with self.assertRaises(ValueError):
            self.store.eligible_pairs("CMP-PAUSED")

    def test_scenario_catalog_uses_cutoff_and_does_not_leak_evidence_to_listing(self):
        catalog = build_catalog(self.prepared, self.directory / "scenarios")
        self.operations.scenarios_path = self.directory / "scenarios/catalog.json"
        listing = self.operations.scenarios(self.operator)
        self.assertEqual(self.store.config["demo_at"], listing["analysis_at"])
        self.assertEqual(catalog["source_version"], listing["source_version"])
        self.assertTrue(listing["profiles"])
        self.assertTrue(all("latest_known_transactions" not in p and "customer_evidence" not in p for p in listing["profiles"]))
        self.assertIsNone(self.operations.scenarios(self.customer))
        catalog["analysis_at"] = "2026-06-01T12:00:00"
        self.operations.scenarios_path.write_text(json.dumps(catalog), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "reconstrucción"):
            self.operations.scenarios(self.operator)


class OperationsHTTPTests(unittest.TestCase):
    """HTTP adapter tests reuse SQL data without inheriting duplicate test cases."""

    def setUp(self):
        self.fixture = OperationsTests("runTest")
        self.fixture.setUp()
        f = self.fixture
        with closing(sqlite3.connect(f.prepared)) as conn, conn:
            conn.execute("INSERT INTO accounts VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         ("P000", "C000", "Cuenta Ahorro", "COP", "Active", "2025-01-01", "2026-01-01", "[]",
                          "data/products.csv", 2, "a" * 64))
            conn.execute("INSERT INTO activity VALUES (?,?,?,?,?,?,?)", ("P000", "C000", 2, "2026-02-20", 1, 0, 0))
            conn.executemany("INSERT INTO activity_evidence VALUES (?,?,?,?,?,?,?,?)",
                             (("T-FIRST", "P000", "C000", "2026-01-05", "Deposit", "data/transactions/known.csv", 2, "b" * 64),
                              ("T-LAST", "P000", "C000", "2026-02-20", "Withdrawal", "data/transactions/known.csv", 3, "c" * 64)))
        build_catalog(f.prepared, f.directory / "scenarios")
        self.server = make_server(f.service, ROOT / "web", port=0, scenarios_path=f.directory / "scenarios/catalog.json")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.fixture.tearDown()

    def call(self, path, token=None, body=None, headers=None):
        request_headers = {"Authorization": "Bearer " + token} if token else {}
        request_headers.update(headers or {})
        raw = None
        if body is not None:
            raw = json.dumps(body).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        request = Request(self.url + path, data=raw, headers=request_headers)
        try:
            response = urlopen(request, timeout=5)
        except HTTPError as error:
            response = error
        with response:
            data = response.read().decode("utf-8")
            return response.status, (json.loads(data) if response.headers.get_content_type() == "application/json" else data), dict(response.headers)

    def prepare(self):
        f = self.fixture
        code, pending, _ = self.call("/api/batches/preview", f.operator, {"campaign_id": CAMPAIGN})
        self.assertEqual(200, code)
        confirmation = {k: pending[k] for k in ("batch_action_id", "idempotency_key")}
        confirmation["confirmed"] = True
        code, result, _ = self.call("/api/batches/confirm", f.operator, confirmation)
        self.assertEqual(200, code)
        return result, confirmation

    def test_http_permissions_cover_operator_reads_customer_profile_and_post_actions(self):
        f = self.fixture
        for token in (None, f.customer, f.advisor):
            for path in ("/api/selection", "/api/scenarios", "/api/audience"):
                self.assertEqual(403, self.call(path, token)[0])
            self.assertEqual(403, self.call("/api/batches/preview", token, {"campaign_id": CAMPAIGN})[0])
        for path in ("/api/selection", "/api/scenarios", "/api/audience", "/api/campaigns"):
            self.assertEqual(200, self.call(path, f.operator)[0])
        for token in (None, f.operator, f.advisor):
            self.assertEqual(403, self.call("/api/profile", token)[0])
        self.assertEqual(403, self.call("/api/profile", f.service.issue_test_session("C000", expired=True))[0])
        self.assertEqual(0, f.count_state("batch_actions"))

    def test_http_endpoint_cannot_be_shared_by_a_second_server(self):
        with self.assertRaises(OSError):
            make_server(self.fixture.service, ROOT / "web", port=self.server.server_port)

    def test_http_rejects_extra_fields_duplicate_filters_and_non_boolean_confirmation(self):
        f = self.fixture
        invalid_queries = ("limit=101", "offset=-1", "limit=1&limit=2", "decision=unknown", "customer_id=C000", "campaign_id=missing")
        for query in invalid_queries:
            self.assertEqual(400, self.call("/api/selection?" + query, f.operator)[0])
        for body in ({}, {"campaign_id": CAMPAIGN, "customer_id": "C000"}, {"campaign_id": []}):
            self.assertEqual(400, self.call("/api/batches/preview", f.operator, body)[0])
        _, pending, _ = self.call("/api/batches/preview", f.operator, {"campaign_id": CAMPAIGN})
        body = {k: pending[k] for k in ("batch_action_id", "idempotency_key")}
        for value in (1, "true", None):
            self.assertEqual(400, self.call("/api/batches/confirm", f.operator, dict(body, confirmed=value))[0])
        self.assertEqual(403, self.call("/api/batches/confirm", f.operator, dict(body, confirmed=False))[0])
        self.assertEqual(400, self.call("/api/batches/confirm", f.operator, dict(body, confirmed=True, role="operator"))[0])
        self.assertEqual(400, self.call("/api/chat", f.customer, {"message": "hola", "customer_id": "C001"})[0])
        self.assertEqual(400, self.call("/api/login", body={"username": "x", "password": "y", "role": "operator"})[0])
        self.assertEqual(0, f.count_state("campaign_batches"))

    def test_http_export_has_all_members_then_blocks_stale_audience_and_owner(self):
        f = self.fixture
        result, confirmation = self.prepare()
        export_url = "/api/batches/" + result["batch_id"] + "/export"
        code, raw, headers = self.call(export_url, f.operator)
        self.assertEqual(200, code)
        members = list(csv.DictReader(io.StringIO(raw)))
        self.assertEqual(125, len(members))
        self.assertEqual("C124", members[-1]["customer_id"])
        self.assertIn("text/csv", headers["Content-Type"])
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertEqual("no-store", headers["Cache-Control"])
        for token in (None, f.customer, f.other_operator):
            self.assertEqual(403, self.call(export_url, token)[0])
        f.optout()
        self.assertEqual(409, self.call(export_url, f.operator)[0])
        code, stale, _ = self.call("/api/batches/confirm", f.operator, confirmation)
        self.assertEqual(409, code)
        self.assertEqual("needs_refresh", stale["status"])
        _, selection, _ = self.call("/api/selection", f.operator)
        self.assertEqual(124, selection["total"])
        fresh, _ = self.prepare()
        _, raw, _ = self.call("/api/batches/" + fresh["batch_id"] + "/export", f.operator)
        members = list(csv.DictReader(io.StringIO(raw)))
        self.assertEqual(124, len(members))
        self.assertNotIn("C000", {r["customer_id"] for r in members})

    def test_http_profile_reads_own_snapshot_evidence_without_live_date_claim(self):
        f = self.fixture
        code, profile, _ = self.call("/api/profile?customer_id=C001", f.customer)
        self.assertEqual(200, code)
        self.assertEqual("C000", profile["customer_id"])
        self.assertEqual("2026-03-01T12:00:00", profile["analysis_at"])
        self.assertEqual("P000", profile["accounts"][0]["product_id"])
        self.assertEqual("2026-02-20", profile["activity"]["last_observed_transaction"])
        self.assertEqual(["T-LAST", "T-FIRST"], [row["transaction_id"] for row in profile["activity"]["evidence"]])
        self.assertTrue(all(row["customer_id"] == "C000" and len(row["raw_row_sha256"]) == 64 for row in profile["activity"]["evidence"]))
        self.assertNotIn("balance", profile["accounts"][0])
        f.optout()
        _, updated, _ = self.call("/api/profile", f.customer)
        self.assertTrue(updated["marketing_optout"])
        self.assertFalse(updated["campaign_matches"][0]["eligible"])

    def test_http_tool_failure_and_source_change_do_not_export_or_claim_success(self):
        f = self.fixture
        _, pending, _ = self.call("/api/batches/preview", f.operator, {"campaign_id": CAMPAIGN})
        body = {k: pending[k] for k in ("batch_action_id", "idempotency_key")}
        body["confirmed"] = True
        def fail(stage):
            if stage == "before_batch_readback":
                raise RuntimeError("trusted readback failure")
        f.service.fault_injector = fail
        code, failure, _ = self.call("/api/batches/confirm", f.operator, body)
        self.assertEqual(503, code)
        self.assertEqual("tool_error", failure["status"])
        self.assertEqual(0, f.count_state("campaign_batches"))
        f.service.fault_injector = None
        with closing(sqlite3.connect(f.prepared)) as conn, conn:
            conn.execute("UPDATE metadata SET value=? WHERE key='source_version'", (json.dumps("v2"),))
        self.assertEqual(503, self.call("/api/selection", f.operator)[0])
        f.service.store = DataStore(f.prepared)
        code, changed, _ = self.call("/api/batches/confirm", f.operator, body)
        self.assertEqual(409, code)
        self.assertEqual("changed", changed["status"])
        self.assertEqual(503, self.call("/api/scenarios", f.operator)[0])


if __name__ == "__main__":
    unittest.main()
