"""Campaign selection and confirmed local preparation, independent of chat.

No external delivery adapter is configured. A prepared batch is a persisted
recipient list, never a claim that advertisements have been delivered.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import secrets
import sqlite3


class CampaignOperations:
    def __init__(self, service, scenarios_path=None):
        self.service = service
        self.scenarios_path = Path(scenarios_path) if scenarios_path else None
        with service._lock, service._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS batch_actions(
                    action_id TEXT PRIMARY KEY,owner TEXT NOT NULL,idempotency_key TEXT NOT NULL,
                    campaign_id TEXT NOT NULL,source_version TEXT NOT NULL,signature TEXT NOT NULL,
                    expected_count INTEGER NOT NULL,expires_at TEXT NOT NULL,created_at TEXT NOT NULL,
                    UNIQUE(owner,idempotency_key));
                CREATE TABLE IF NOT EXISTS campaign_batches(
                    batch_id TEXT PRIMARY KEY,owner TEXT NOT NULL,idempotency_key TEXT NOT NULL,
                    campaign_id TEXT NOT NULL,source_version TEXT NOT NULL,analysis_at TEXT NOT NULL,
                    signature TEXT NOT NULL,member_count INTEGER NOT NULL,status TEXT NOT NULL,
                    created_at TEXT NOT NULL,UNIQUE(owner,idempotency_key));
                CREATE TABLE IF NOT EXISTS batch_members(
                    batch_id TEXT NOT NULL,campaign_id TEXT NOT NULL,customer_id TEXT NOT NULL,
                    PRIMARY KEY(batch_id,customer_id));
            """)

    @staticmethod
    def _now():
        return datetime.now(timezone.utc)

    def _identity(self, conn, token, action, confirmed=False):
        owner, principal = self.service._principal(conn, token)
        if not self.service._allowed(principal, action, confirmed=confirmed):
            return None
        return owner

    @staticmethod
    def _optouts(conn):
        return {r[0] for r in conn.execute("SELECT customer_id FROM consent_overrides WHERE accepts_marketing=0")}

    def campaigns(self, token):
        with self.service._lock, self.service._connect() as conn:
            if self._identity(conn, token, "view_public_campaign") is None:
                return None
            return dict(campaigns=self.service.store.list_campaigns(),
                        analysis_at=self.service.store.config["demo_at"],
                        source_version=self.service.store.source_version)

    def selection(self, token, **filters):
        with self.service._lock, self.service._connect() as conn:
            if self._identity(conn, token, "view_selection") is None:
                return None
            return self.service.store.selection(excluded_customer_ids=self._optouts(conn), **filters)

    def scenarios(self, token):
        with self.service._lock, self.service._connect() as conn:
            if self._identity(conn, token, "view_selection") is None:
                return None
            if not self.scenarios_path or not self.scenarios_path.exists():
                raise RuntimeError("Construye el catálogo de escenarios antes de iniciar")
            catalog = json.loads(self.scenarios_path.read_text(encoding="utf-8"))
            store = self.service.store
            if catalog.get("source_version") != store.source_version or catalog.get("analysis_at") != store.config["demo_at"] or catalog.get("policy_version") != store.config["policy_version"]:
                raise RuntimeError("El catálogo de escenarios requiere reconstrucción")
            fields = ("username", "customer_id", "scenario", "label", "expected_eligible", "expected_reasons", "questions")
            return dict(profiles=[{k: p[k] for k in fields if k in p} for p in catalog["profiles"]],
                        coverage=catalog["coverage"], analysis_at=catalog["analysis_at"],
                        source_version=catalog["source_version"])

    def _pairs(self, conn, campaign_id):
        return self.service.store.eligible_pairs(campaign_id, self._optouts(conn))

    def _signature(self, campaign_id, pairs):
        payload = dict(campaign_id=campaign_id, source_version=self.service.store.source_version,
                       configuration=self.service.store.config,
                       pairs=pairs)
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    def preview(self, token, campaign_id):
        with self.service._lock, self.service._connect() as conn:
            owner = self._identity(conn, token, "preview_campaign_batch")
            if owner is None:
                return dict(status="denied")
            pairs = self._pairs(conn, campaign_id)
            if not pairs:
                return dict(status="empty", audience_count=0)
            action, key = "BATCH-ACTION-" + secrets.token_hex(12), secrets.token_hex(16)
            expires = self._now() + timedelta(minutes=5)
            conn.execute("INSERT INTO batch_actions VALUES(?,?,?,?,?,?,?,?,?)",
                         (action, owner, key, campaign_id, self.service.store.source_version,
                          self._signature(campaign_id, pairs), len(pairs), expires.isoformat(), self._now().isoformat()))
            return dict(status="confirmation_pending", batch_action_id=action, idempotency_key=key,
                        campaign_id=campaign_id, audience_count=len(pairs), expires_at=expires.isoformat(),
                        analysis_at=self.service.store.config["demo_at"], source_version=self.service.store.source_version,
                        delivery_mode="local_preparation_only")

    def confirm(self, token, batch_action_id, idempotency_key, confirmed=False):
        with self.service._lock, self.service._connect() as conn:
            owner = self._identity(conn, token, "prepare_campaign_batch", confirmed)
            if owner is None or not isinstance(batch_action_id, str) or not isinstance(idempotency_key, str):
                return dict(status="denied")
            action = conn.execute("SELECT * FROM batch_actions WHERE action_id=? AND owner=? AND idempotency_key=?",
                                  (batch_action_id, owner, idempotency_key)).fetchone()
            if action is None:
                return dict(status="denied")
            existing = conn.execute("SELECT * FROM campaign_batches WHERE owner=? AND idempotency_key=?", (owner, idempotency_key)).fetchone()
            if existing:
                return self._batch_result(conn, existing, replay=True)
            if datetime.fromisoformat(action["expires_at"]) <= self._now():
                return dict(status="expired")
            if action["source_version"] != self.service.store.source_version:
                return dict(status="changed")
            try:
                conn.execute("BEGIN IMMEDIATE")
                pairs = self._pairs(conn, action["campaign_id"])
                if self._signature(action["campaign_id"], pairs) != action["signature"]:
                    conn.rollback()
                    return dict(status="changed")
                batch_id = "BATCH-" + secrets.token_hex(12)
                self.service._fault("before_batch_write")
                conn.execute("INSERT INTO campaign_batches VALUES(?,?,?,?,?,?,?,?,?,?)",
                             (batch_id, owner, idempotency_key, action["campaign_id"], self.service.store.source_version,
                              self.service.store.config["demo_at"], action["signature"], len(pairs), "prepared", self._now().isoformat()))
                conn.executemany("INSERT INTO batch_members VALUES(?,?,?)", ((batch_id, p["campaign_id"], p["customer_id"]) for p in pairs))
                self.service._fault("before_batch_readback")
                persisted = [dict(r) for r in conn.execute("SELECT campaign_id,customer_id FROM batch_members WHERE batch_id=? ORDER BY customer_id", (batch_id,))]
                observed = conn.execute("SELECT * FROM campaign_batches WHERE batch_id=? AND owner=?", (batch_id, owner)).fetchone()
                if not observed or persisted != pairs or observed["member_count"] != len(pairs) or observed["signature"] != action["signature"] or observed["campaign_id"] != action["campaign_id"] or observed["source_version"] != action["source_version"]:
                    raise RuntimeError("El lote no concilia")
                conn.execute("INSERT INTO audit(owner,action,outcome,reference,created_at) VALUES(?,?,?,?,?)",
                             (owner, "prepare_campaign_batch", "verified", batch_id, self._now().isoformat()))
                result = self._batch_result(conn, observed)
                if result["status"] != "prepared":
                    raise RuntimeError("La audiencia cambió antes de publicar el lote")
                conn.commit()
                return result
            except (sqlite3.Error, OSError, RuntimeError, ValueError):
                conn.rollback()
                return dict(status="tool_error", batch_id=None)

    def _batch_result(self, conn, row, replay=False):
        current = row["source_version"] == self.service.store.source_version
        if current:
            current = row["signature"] == self._signature(row["campaign_id"], self._pairs(conn, row["campaign_id"]))
        return dict(batch_id=row["batch_id"], campaign_id=row["campaign_id"], count=row["member_count"],
                    status="prepared" if current else "needs_refresh", analysis_at=row["analysis_at"],
                    source_version=row["source_version"], delivery_mode="local_preparation_only",
                    external_deliveries=0, idempotent_replay=replay, created_at=row["created_at"])

    def get_batch(self, token, batch_id, include_members=False):
        with self.service._lock, self.service._connect() as conn:
            owner = self._identity(conn, token, "view_selection")
            if owner is None:
                return None
            row = conn.execute("SELECT * FROM campaign_batches WHERE batch_id=? AND owner=?", (batch_id, owner)).fetchone()
            if row is None:
                return None
            result = self._batch_result(conn, row)
            if include_members and result["status"] == "prepared":
                result["rows"] = [dict(r) for r in conn.execute("SELECT campaign_id,customer_id FROM batch_members WHERE batch_id=? ORDER BY customer_id", (batch_id,))]
            return result
