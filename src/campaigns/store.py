"""Minimal reads from the prepared snapshot; authorization belongs to the service."""

from contextlib import contextmanager
from collections import Counter
import json
from pathlib import Path
import sqlite3

from .policy import campaign_reasons, timestamp


class DataStore:
    def __init__(self, path):
        self.path = Path(path).resolve()
        with self._connect() as conn:
            self.config = json.loads(conn.execute("SELECT value FROM metadata WHERE key='config'").fetchone()[0])
            self.source_version = json.loads(conn.execute("SELECT value FROM metadata WHERE key='source_version'").fetchone()[0])

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(f"{self.path.as_uri()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            if hasattr(self, "source_version"):
                version = json.loads(conn.execute("SELECT value FROM metadata WHERE key='source_version'").fetchone()[0])
                config = json.loads(conn.execute("SELECT value FROM metadata WHERE key='config'").fetchone()[0])
                if version != self.source_version or config != self.config:
                    raise RuntimeError("La base preparada cambió: reinicia el servicio para usar la nueva versión")
            yield conn
        finally:
            conn.close()

    @staticmethod
    def _row(row):
        if row is None:
            return None
        value = dict(row)
        if "quality_flags" in value:
            value["quality_flags"] = json.loads(value["quality_flags"])
        value["evidence"] = {"file": value.get("source_file"), "record": value.get("source_row"), "row_sha256": value.get("raw_row_sha256")}
        return value

    def get_customer(self, customer_id):
        with self._connect() as conn:
            value = self._row(conn.execute("SELECT * FROM customers WHERE customer_id=?", (customer_id,)).fetchone())
        if value:
            at = timestamp(self.config["demo_at"])
            registered, updated = timestamp(value["registration_date"]), timestamp(value["last_updated"])
            value["profile_available"] = bool(not value["quality_flags"] and registered and updated and registered <= updated <= at)
            value["historical_snapshot_assumption"] = True
        return value

    def get_accounts(self, customer_id):
        with self._connect() as conn:
            result = [self._row(r) for r in conn.execute("SELECT * FROM accounts WHERE customer_id=? AND quality_flags='[]' ORDER BY product_id", (customer_id,))]
        for row in result:
            row["historical_snapshot_assumption"] = True
        return result

    def get_activity(self, customer_id):
        with self._connect() as conn:
            rows = [dict(r) for r in conn.execute("""SELECT a.* FROM activity a JOIN accounts p USING(product_id)
              WHERE a.customer_id=? AND p.quality_flags='[]'""", (customer_id,))]
            evidence = [dict(r) for r in conn.execute("""SELECT e.* FROM activity_evidence e JOIN accounts p USING(product_id)
              WHERE e.customer_id=? AND p.quality_flags='[]' ORDER BY transaction_date DESC,transaction_id LIMIT 5""", (customer_id,))]
        known = sum(r["valid_known_transactions"] for r in rows)
        unreliable = sum(r["unreliable_recent_records"] for r in rows)
        unobservable = sum(r["unobservable_recent_records"] for r in rows)
        caveats = ["source_snapshot_and_processing_day_assumptions", "absence_of_records_is_not_proof_of_inactivity"]
        if unreliable:
            caveats.append("recent_records_quarantined")
        if unobservable:
            caveats.append("recent_records_not_available_by_demo")
        return {"valid_known_transactions": known,
                "last_observed_transaction": max((r["last_observed_transaction"] for r in rows if r["last_observed_transaction"]), default=None),
                "recent_30d_count": sum(r["recent_30d_count"] for r in rows),
                "unreliable_recent_records": unreliable, "unobservable_recent_records": unobservable,
                "quality_caveats": caveats, "evidence": evidence, "analysis_at": self.config["demo_at"], "demo_at": self.config["demo_at"],
                "activity_known": known > 0, "financial_benefit_verified": False}

    def list_campaigns(self):
        with self._connect() as conn:
            values = [self._row(r) for r in conn.execute("SELECT * FROM campaigns WHERE promoted_product=? AND target_country=? ORDER BY campaign_id", (self.config["product"], self.config["country"]))]
        for row in values:
            row.update(selection_reasons=campaign_reasons(row, self.config), financial_terms_available=False,
                       historical_replay_assumption=row["campaign_status"] == "Completed", delivery_mode="simulation_only")
        return values

    def campaign_matches(self, customer_id):
        campaign_map = {r["campaign_id"]: r for r in self.list_campaigns()}
        with self._connect() as conn:
            rows = [dict(r) for r in conn.execute("SELECT * FROM decisions WHERE customer_id=? ORDER BY campaign_id", (customer_id,))]
        for row in rows:
            row.update(eligible=bool(row["eligible"]), reasons=json.loads(row["reasons"]), account_ids=json.loads(row["account_ids"]), explanation=json.loads(row["explanation"]))
            row.update(campaign_map.get(row["campaign_id"], {}))
        return rows

    def audience(self, limit=100):
        limit = max(1, min(int(limit), 1000))
        with self._connect() as conn:
            return [dict(r) for r in conn.execute("SELECT campaign_id,customer_id FROM decisions WHERE eligible=1 ORDER BY campaign_id,customer_id LIMIT ?", (limit,))]

    def selection(self, campaign_id=None, decision="eligible", reason=None, offset=0, limit=25, excluded_customer_ids=()):
        """Decision projection for the operator, not unrestricted customer context.

        A temporary table applies the current consent overlay before counting,
        filtering or pagination. It never writes to the prepared source database.
        """
        campaigns = self.list_campaigns()
        if campaign_id is None:
            campaign_id = next((c["campaign_id"] for c in campaigns if not c["selection_reasons"]), None)
        campaign = next((c for c in campaigns if c["campaign_id"] == campaign_id), None)
        if campaign is None or decision not in {"eligible", "excluded", "all"}:
            raise ValueError("Campaña o filtro de selección inválido")
        if reason is not None and (not isinstance(reason, str) or len(reason) > 100):
            raise ValueError("Motivo inválido")
        offset, limit = int(offset), int(limit)
        if offset < 0 or not 1 <= limit <= 100:
            raise ValueError("Paginación inválida")
        excluded_customer_ids = set(excluded_customer_ids)
        effective = "(d.eligible=1 AND o.customer_id IS NULL)"
        base = " FROM decisions d LEFT JOIN local_optouts o USING(customer_id) WHERE d.campaign_id=?"
        params, condition = [campaign_id], ""
        if decision == "eligible":
            condition += " AND " + effective
        elif decision == "excluded":
            condition += " AND NOT " + effective
        if reason == "local_marketing_optout":
            condition += " AND o.customer_id IS NOT NULL"
        elif reason:
            condition += " AND instr(d.reasons,?)>0"
            params.append(json.dumps(reason))
        with self._connect() as conn:
            conn.execute("CREATE TEMP TABLE local_optouts(customer_id TEXT PRIMARY KEY)")
            conn.executemany("INSERT INTO local_optouts VALUES(?)", ((i,) for i in sorted(set(excluded_customer_ids))))
            counts = conn.execute("SELECT COUNT(*),SUM(" + effective + "),SUM(o.customer_id IS NOT NULL)" + base, (campaign_id,)).fetchone()
            total = conn.execute("SELECT COUNT(*)" + base + condition, params).fetchone()[0]
            raw_rows = conn.execute("SELECT d.*,o.customer_id IS NOT NULL AS opted_out" + base + condition +
                                    " ORDER BY d.customer_id LIMIT ? OFFSET ?", params + [limit, offset]).fetchall()
            reasons = Counter()
            for row in conn.execute("SELECT d.reasons,o.customer_id IS NOT NULL AS opted_out" + base, (campaign_id,)):
                values = set(json.loads(row["reasons"]))
                if row["opted_out"]:
                    values.add("local_marketing_optout")
                reasons.update(values)
        rows = []
        for raw in raw_rows:
            row = dict(raw)
            opted_out = row.pop("opted_out")
            row.update(eligible=bool(row["eligible"] and not opted_out),
                       reasons=json.loads(row["reasons"]), account_ids=json.loads(row["account_ids"]),
                       explanation=json.loads(row["explanation"]))
            if row["customer_id"] in excluded_customer_ids:
                row["reasons"] = sorted(set(row["reasons"]) | {"local_marketing_optout"})
            rows.append(row)
        return dict(summary=dict(evaluated_pairs=counts[0], eligible_pairs=counts[1] or 0,
                                 excluded_pairs=counts[0] - (counts[1] or 0), local_optouts=counts[2] or 0,
                                 reason_counts=dict(sorted(reasons.items())), reasons_overlap=True),
                    rows=rows, total=total, offset=offset, limit=limit, campaign_id=campaign_id,
                    campaign=campaign, analysis_at=self.config["demo_at"], source_version=self.source_version)

    def eligible_pairs(self, campaign_id, excluded_customer_ids=()):
        campaign = next((c for c in self.list_campaigns() if c["campaign_id"] == campaign_id), None)
        if campaign is None or campaign["selection_reasons"]:
            raise ValueError("La campaña no está disponible en el corte de selección")
        excluded = set(excluded_customer_ids)
        with self._connect() as conn:
            return [dict(r) for r in conn.execute("SELECT campaign_id,customer_id FROM decisions WHERE campaign_id=? AND eligible=1 ORDER BY customer_id", (campaign_id,))
                    if r["customer_id"] not in excluded]

    def demo_customers(self, limit=5):
        """Select demo credentials locally; do not expose this method over HTTP."""
        with self._connect() as conn:
            return [r[0] for r in conn.execute("SELECT DISTINCT customer_id FROM decisions WHERE eligible=1 ORDER BY customer_id LIMIT ?", (limit,))]
