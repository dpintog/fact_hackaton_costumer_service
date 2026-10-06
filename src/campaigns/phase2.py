"""Proyección de cuentas/actividad y selección explicable sin etiquetas futuras."""

from collections import Counter
import csv
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile

from .policy import campaign_reasons, customer_reasons, timestamp
from .prepare import connect, digest, load_config, row_dict, write_json

PRODUCT_FIELDS = ["product_id", "customer_id", "product_type", "currency", "product_status", "opening_date", "last_updated"]
TRANSACTION_FIELDS = ["transaction_id", "product_id", "customer_id", "transaction_date", "process_date", "transaction_type", "transaction_status"]
PRODUCT_STATUSES = {"Active", "Closed", "Blocked", "Suspended"}
TRANSACTION_STATUSES = {"Approved", "Declined", "Pending", "Reversed"}


def clean(value):
    value = (value or "").strip()
    return "" if value.casefold() in {"nan", "null", "none", "nat"} else value


def row_hash(raw):
    return hashlib.sha256(json.dumps(raw, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def source_inventory(root, include_phase2=False):
    """Files actually consumed by the preparation, including new/deleted partitions."""
    root = Path(root)
    paths = [root / "data/customers.csv", root / "data/marketing_campaigns.csv"]
    paths.extend((root / "data/campaign_sends").rglob("*.csv"))
    if include_phase2:
        paths.append(root / "data/products.csv")
        paths.extend((root / "data/transactions").rglob("*.csv"))
        paths.extend((root / "docs").glob("*.pdf"))
    return {path.relative_to(root).as_posix() for path in paths if path.is_file()}


def check_source_inventory(root, manifest, include_phase2=False):
    expected = {item["path"] for item in manifest}
    if not include_phase2:
        expected = {path for path in expected if path in {"data/customers.csv", "data/marketing_campaigns.csv"}
                    or path.startswith("data/campaign_sends/") and path.endswith(".csv")}
    current = source_inventory(root, include_phase2)
    if current != expected:
        added, removed = sorted(current - expected), sorted(expected - current)
        details = "; ".join(["añadidos: " + ", ".join(added)] if added else [])
        if removed:
            details += ("; " if details else "") + "eliminados: " + ", ".join(removed)
        stage = "las fases 1 y 2" if not include_phase2 else "la fase 2 (y la fase 1 si cambian sus fuentes)"
        raise ValueError("Inventario de fuentes modificado; reconstruir " + stage + ": " + details)


def read_csv(path, fields):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        header = reader.fieldnames or []
        if len(set(header)) != len(header) or not set(fields).issubset(header):
            raise ValueError(f"Contrato CSV inválido: {path.name}")
        for index, row in enumerate(reader, 2):
            if None in row:
                raise ValueError(f"CSV mal formado: {path.name}, registro {index}")
            yield index, row


def schema(conn):
    conn.executescript("""
      CREATE TABLE customers(customer_id TEXT PRIMARY KEY, country TEXT, segment TEXT,
        accepts_marketing INTEGER, customer_status TEXT, registration_date TEXT, last_updated TEXT,
        quality_flags TEXT, source_file TEXT, source_row INTEGER, raw_row_sha256 TEXT);
      CREATE TABLE campaigns(campaign_id TEXT PRIMARY KEY, campaign_name TEXT, description TEXT,
        campaign_type TEXT,campaign_objective TEXT,promoted_product TEXT,target_segment TEXT,
        target_country TEXT,start_date TEXT,end_date TEXT,campaign_status TEXT,quality_flags TEXT,
        source_file TEXT,source_row INTEGER,raw_row_sha256 TEXT);
      CREATE TABLE accounts(product_id TEXT PRIMARY KEY,customer_id TEXT,product_type TEXT,currency TEXT,
        product_status TEXT,opening_date TEXT,last_updated TEXT,quality_flags TEXT,
        source_file TEXT,source_row INTEGER,raw_row_sha256 TEXT);
      CREATE TABLE activity(product_id TEXT PRIMARY KEY,customer_id TEXT,valid_known_transactions INTEGER,
        last_observed_transaction TEXT,recent_30d_count INTEGER,unreliable_recent_records INTEGER,
        unobservable_recent_records INTEGER);
      CREATE TABLE activity_evidence(transaction_id TEXT PRIMARY KEY,product_id TEXT,customer_id TEXT,
        transaction_date TEXT,transaction_type TEXT,source_file TEXT,source_row INTEGER,raw_row_sha256 TEXT);
      CREATE TABLE contact_counts(customer_id TEXT,days INTEGER,n INTEGER,PRIMARY KEY(customer_id,days));
      CREATE TABLE decisions(campaign_id TEXT,customer_id TEXT,eligible INTEGER,reasons TEXT,
        account_ids TEXT,explanation TEXT,PRIMARY KEY(campaign_id,customer_id));
      CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);
    """)


def prepare(root, out, config, day1=None):
    """Reconstrucción completa en archivo temporal; nunca edita los CSV ni día 1."""
    root, out = Path(root).resolve(), Path(out).resolve()
    if out == root or out == root / "data" or root / "data" in out.parents:
        raise ValueError("La salida debe estar fuera de data y de la raíz")
    day1 = Path(day1 or root / "outputs/day1/prepared.sqlite").resolve()
    source = sqlite3.connect(f"{day1.as_uri()}?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    stored_config = json.loads(source.execute("SELECT value FROM metadata WHERE key='config'").fetchone()[0])
    for name in ("product", "country", "demo_at", "dataset_cutoff"):
        if stored_config[name] != config[name]:
            source.close()
            raise ValueError(f"El día 1 debe reconstruirse: cambió {name}")
    previous_manifest = json.loads(source.execute("SELECT value FROM metadata WHERE key='source_manifest'").fetchone()[0])
    try:
        check_source_inventory(root, previous_manifest)
    except Exception:
        source.close()
        raise
    for item in previous_manifest:
        if digest(root / item["path"]) != item["sha256"]:
            source.close()
            raise ValueError("Fuente del día 1 modificada; reconstruir: " + item["path"])
    out.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(prefix="phase2-", suffix=".sqlite", dir=out)
    os.close(handle)
    temp = Path(name)
    conn = connect(temp)
    conn.execute("PRAGMA cache_size=-131072")
    schema(conn)
    at, cutoff = timestamp(config["demo_at"]), timestamp(config["dataset_cutoff"])
    if config["reactivation_observation_days"] != 30 or config["reactivation_requires_prior_known_activity"] is not True or config["reactivation_blocks_unreliable_recent_records"] is not True:
        source.close()
        conn.close()
        raise ValueError("La política v1 requiere ventana de 30 días, actividad previa conocida y bloqueo de registros recientes inciertos")
    lower = at - timedelta(days=config["reactivation_observation_days"])
    stats, manifest = {"products": Counter(), "transactions": Counter()}, []
    customers, campaigns, accounts = {}, [], {}
    try:
        for raw in source.execute("SELECT * FROM customers ORDER BY customer_id"):
            conn.execute("INSERT INTO customers VALUES (?,?,?,?,?,?,?,?,?,?,?)", tuple(raw))
            customers[raw["customer_id"]] = row_dict(raw)
        for raw in source.execute("SELECT * FROM campaigns ORDER BY campaign_id"):
            conn.execute("INSERT INTO campaigns VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", tuple(raw))
            campaigns.append(row_dict(raw))
        source_accounts = root / "data/products.csv"
        before = source_accounts.stat()
        seen_products = {}
        for index, raw in read_csv(source_accounts, PRODUCT_FIELDS):
            stats["products"]["raw_rows"] += 1
            # product_id is global, so a conflicting owner/type/country outside
            # the projection still invalidates the claimed in-scope account.
            product_id, product_hash = clean(raw["product_id"]), row_hash(raw)
            if product_id:
                if product_id in seen_products:
                    if seen_products[product_id] != product_hash:
                        raise ValueError("Producto duplicado contradictorio: " + product_id)
                    stats["products"]["duplicate_identical"] += 1
                    continue
                seen_products[product_id] = product_hash
            customer = customers.get(clean(raw["customer_id"]))
            if clean(raw["product_type"]) != config["product"] or not customer or customer["country"] != config["country"]:
                continue
            row = {f: clean(raw[f]) for f in PRODUCT_FIELDS}
            flags = []
            opened, updated = timestamp(row["opening_date"]), timestamp(row["last_updated"])
            registered = timestamp(customer["registration_date"])
            if not row["product_id"]:
                flags.append("product_id_missing")
            if opened is None or updated is None:
                flags.append("account_date_invalid")
            if opened and registered and opened < registered:
                flags.append("opening_before_customer_registration")
            if opened and opened > at:
                flags.append("account_opened_after_demo")
            if opened and updated and updated < opened:
                flags.append("account_updated_before_opening")
            if updated and updated > at:
                flags.append("account_profile_after_demo")
            if updated and updated > cutoff:
                flags.append("account_profile_after_cutoff")
            if row["product_status"] not in PRODUCT_STATUSES:
                flags.append("account_status_unknown")
            if row["currency"] not in {"COP", "USD", "MXN", "ARS"}:
                flags.append("account_currency_unknown")
            row.update(quality_flags=flags, source_file="data/products.csv", source_row=index, raw_row_sha256=product_hash)
            stats["products"]["colombia_savings_rows"] += 1
            stats["products"].update(flags)
            if not row["product_id"]:
                continue
            accounts[row["product_id"]] = row
            conn.execute("INSERT INTO accounts VALUES (?,?,?,?,?,?,?,?,?,?,?)", tuple(row[f] for f in PRODUCT_FIELDS) +
                         (json.dumps(flags), row["source_file"], index, row["raw_row_sha256"]))
        if (before.st_size, before.st_mtime_ns) != (source_accounts.stat().st_size, source_accounts.stat().st_mtime_ns):
            raise ValueError("products.csv cambió durante la lectura")
        manifest.append({"path": "data/products.csv", "sha256": digest(source_accounts), "bytes": before.st_size})
        del seen_products
        aggregate = {key: {"count": 0, "last": None, "recent": 0, "unreliable": 0, "unobservable": 0}
                     for key, account in accounts.items() if not account["quality_flags"]}
        tx_files = sorted((root / "data/transactions").rglob("*.csv"))
        if not tx_files:
            raise ValueError("Faltan archivos transactions")
        # Sólo guardamos evidencias de los cinco eventos aprobados más recientes por cuenta.
        samples, seen = {}, {}
        for file_index, path in enumerate(tx_files, 1):
            before = path.stat()
            relative = path.relative_to(root).as_posix()
            file_rows = 0
            for index, raw in read_csv(path, TRANSACTION_FIELDS):
                file_rows += 1
                stats["transactions"]["raw_rows"] += 1
                account_id = clean(raw["product_id"])
                if account_id not in aggregate:
                    continue
                account, agg = accounts[account_id], aggregate[account_id]
                stats["transactions"]["scoped_rows"] += 1
                sent, processed = timestamp(clean(raw["transaction_date"])), timestamp(clean(raw["process_date"]))
                is_recent = sent is None or lower <= sent <= at
                flags = []
                if clean(raw["customer_id"]) != account["customer_id"]:
                    flags.append("customer_product_mismatch")
                if sent is None or processed is None:
                    flags.append("transaction_date_invalid")
                if sent and sent < timestamp(account["opening_date"]):
                    flags.append("transaction_before_account_opening")
                if sent and processed and processed.date() < sent.date():
                    flags.append("process_before_transaction_day")
                if clean(raw["transaction_status"]) not in TRANSACTION_STATUSES:
                    flags.append("transaction_status_unknown")
                tx_id = clean(raw["transaction_id"])
                raw_hash = row_hash(raw)
                if not tx_id:
                    flags.append("transaction_key_missing")
                elif tx_id in seen:
                    if seen[tx_id] != raw_hash:
                        raise ValueError("Transacción duplicada contradictoria: " + tx_id)
                    stats["transactions"]["duplicate_identical"] += 1
                    continue
                else:
                    seen[tx_id] = raw_hash
                if flags:
                    stats["transactions"].update(flags)
                    if is_recent:
                        agg["unreliable"] += 1
                    continue
                if sent > at:
                    stats["transactions"]["future_events_excluded"] += 1
                    continue
                if processed.date() > at.date():
                    if is_recent:
                        agg["unobservable"] += 1
                    stats["transactions"]["not_available_by_demo"] += 1
                    continue
                if clean(raw["transaction_status"]) != "Approved":
                    continue
                agg["count"] += 1
                when = sent.isoformat(sep=" ")
                agg["last"] = max(agg["last"] or when, when)
                agg["recent"] += int(sent >= lower)
                evidence = (tx_id, account_id, account["customer_id"], when, clean(raw["transaction_type"]), relative, index, raw_hash)
                values = samples.setdefault(account_id, [])
                values.append(evidence)
                values.sort(key=lambda e: (e[3], e[0]), reverse=True)
                del values[5:]
            after = path.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise ValueError("Fuente cambió durante lectura: " + relative)
            manifest.append({"path": relative, "sha256": digest(path), "bytes": after.st_size, "rows": file_rows})
            if file_index % 250 == 0:
                print(f"transactions: {file_index}/{len(tx_files)} archivos", flush=True)
        # Keep hashes only for rows related to projected reliable accounts. The
        # second pass catches conflicting IDs in other accounts/countries/types,
        # including when the out-of-scope copy appeared first. It does not add
        # millions of unrelated hashes to memory or count events a second time.
        tx_manifest = {item["path"]: item for item in manifest if item["path"].startswith("data/transactions/")}
        for file_index, path in enumerate(tx_files, 1):
            before = path.stat()
            relative = path.relative_to(root).as_posix()
            for index, raw in read_csv(path, TRANSACTION_FIELDS):
                if clean(raw["product_id"]) in aggregate:
                    continue
                tx_id = clean(raw["transaction_id"])
                if tx_id in seen:
                    stats["transactions"]["cross_scope_key_candidates_checked"] += 1
                    if row_hash(raw) != seen[tx_id]:
                        raise ValueError("Transacción duplicada contradictoria fuera de alcance: " + tx_id)
            after = path.stat()
            if ((before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns)
                    or digest(path) != tx_manifest[relative]["sha256"]):
                raise ValueError("Fuente cambió durante la revisión de claves: " + relative)
            if file_index % 250 == 0:
                print(f"transaction keys: {file_index}/{len(tx_files)} archivos", flush=True)
        check_source_inventory(root, previous_manifest + manifest, include_phase2=True)
        for key, agg in aggregate.items():
            conn.execute("INSERT INTO activity VALUES (?,?,?,?,?,?,?)", (key, accounts[key]["customer_id"], agg["count"], agg["last"], agg["recent"], agg["unreliable"], agg["unobservable"]))
            conn.executemany("INSERT INTO activity_evidence VALUES (?,?,?,?,?,?,?,?)", samples.get(key, []))
        for limit in config["frequency_limits"]:
            rows = source.execute("""SELECT customer_id,count(*) FROM sends WHERE contact_quality_flags='[]'
              AND was_delivered=1 AND send_date>=? AND send_date<=? AND substr(process_date,1,10)<=?
              GROUP BY customer_id""", ((at - timedelta(days=limit["days"])).isoformat(sep=" "), at.isoformat(sep=" "), at.date().isoformat()))
            conn.executemany("INSERT INTO contact_counts VALUES (?,?,?)", ((r[0], limit["days"], r[1]) for r in rows))
        conn.executescript("""CREATE INDEX account_customer ON accounts(customer_id);
          CREATE INDEX activity_customer ON activity(customer_id);
          CREATE INDEX evidence_customer ON activity_evidence(customer_id);
          CREATE INDEX decision_customer ON decisions(customer_id);
        """)
        payload = {"config": config, "manifest": previous_manifest + manifest, "quality": {k: dict(v) for k, v in stats.items()}}
        payload["source_version"] = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        for key, value in payload.items():
            conn.execute("INSERT INTO metadata VALUES (?,?)", (key, json.dumps(value, ensure_ascii=False, sort_keys=True)))
        conn.commit()
        compute_decisions(conn, config)
        conn.commit()
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Integridad phase2 SQLite")
    finally:
        source.close()
        conn.close()
    os.replace(temp, out / "prepared.sqlite")
    write_json(out / "source_manifest.json", payload["manifest"])
    write_json(out / "quality.json", payload["quality"])
    write_json(out / "resolved_scope.json", config)
    return export(out)


def account_reasons(account, activity, config):
    reasons = []
    if account["quality_flags"]:
        reasons.append("account_data_unreliable")
    if account["product_status"] != "Active":
        reasons.append("account_not_active_in_snapshot")
    if activity is None or activity["valid_known_transactions"] == 0:
        reasons.append("prior_activity_unknown")
    else:
        if activity["recent_30d_count"]:
            reasons.append("recent_activity_observed")
        if activity["unreliable_recent_records"]:
            reasons.append("recent_activity_unreliable")
        if activity["unobservable_recent_records"]:
            reasons.append("recent_activity_not_observable_by_demo")
    return reasons


def compute_decisions(conn, config):
    conn.execute("DELETE FROM decisions")
    campaigns = [row_dict(r) for r in conn.execute("SELECT * FROM campaigns WHERE promoted_product=? ORDER BY campaign_id", (config["product"],))]
    available = [c for c in campaigns if not campaign_reasons(c, config)]
    for customer_raw in conn.execute("SELECT * FROM customers WHERE country=? ORDER BY customer_id", (config["country"],)):
        customer = row_dict(customer_raw)
        counts = {r[0]: r[1] for r in conn.execute("SELECT days,n FROM contact_counts WHERE customer_id=?", (customer["customer_id"],))}
        accounts = [row_dict(r) for r in conn.execute("SELECT * FROM accounts WHERE customer_id=? ORDER BY product_id", (customer["customer_id"],))]
        eligible_accounts = []
        reasons_account = set()
        for account in accounts:
            activity = conn.execute("SELECT * FROM activity WHERE product_id=?", (account["product_id"],)).fetchone()
            reasons = account_reasons(account, activity, config)
            reasons_account.update(reasons)
            if not reasons:
                eligible_accounts.append(account["product_id"])
        for campaign in available:
            reasons = customer_reasons(customer, campaign, config, counts)
            if not accounts:
                reasons.append("savings_account_not_found")
            elif not eligible_accounts:
                reasons.extend(sorted(reasons_account))
            explanation = {"selection_basis": "demo_marketing_match_with_prior_activity_and_no_recent_activity_observed",
                           "not_financial_eligibility": True, "not_verified_inactivity": True,
                           "historical_snapshot_assumption": True, "known_delivered_contacts": counts,
                           "policy_version": config["policy_version"], "demo_at": config["demo_at"]}
            conn.execute("INSERT INTO decisions VALUES (?,?,?,?,?,?)", (campaign["campaign_id"], customer["customer_id"], int(not reasons), json.dumps(sorted(set(reasons))), json.dumps(eligible_accounts), json.dumps(explanation)))


def export(out):
    out = Path(out)
    conn = connect(out / "prepared.sqlite")
    totals = conn.execute("SELECT count(*),sum(eligible) FROM decisions").fetchone()
    excluded = Counter()
    with (out / "audience.jsonl").open("w", encoding="utf-8", newline="\n") as stream:
        for row in conn.execute("SELECT * FROM decisions ORDER BY campaign_id,customer_id"):
            reasons = json.loads(row["reasons"])
            excluded.update(reasons)
            if row["eligible"]:
                value = {"campaign_id": row["campaign_id"], "customer_id": row["customer_id"], "account_ids": json.loads(row["account_ids"]), **json.loads(row["explanation"]), "delivery_mode": "simulation_only"}
                stream.write(json.dumps(value, sort_keys=True, ensure_ascii=False) + "\n")
    summary = {"evaluated_pairs": totals[0], "selected_pairs": totals[1] or 0,
               "excluded_reason_counts_overlap": dict(excluded), "selection_is_offline_demo": True,
               "conversion_model_trained": False, "financial_benefit_verified": False}
    write_json(out / "selection_summary.json", summary)
    conn.close()
    write_json(out / "artifact_hashes.json", {p.name: digest(p) for p in sorted(out.iterdir()) if p.is_file() and p.name not in {"artifact_hashes.json", "verification.json"} and not p.name.startswith("phase2-")})
    return summary


def verify(out, root=None):
    out = Path(out)
    for name, expected in json.loads((out / "artifact_hashes.json").read_text(encoding="utf-8")).items():
        if digest(out / name) != expected:
            raise ValueError("Artefacto modificado: " + name)
    conn = connect(out / "prepared.sqlite")
    sources_checked = False
    try:
        conn.execute("PRAGMA cache_size=-131072")
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Integridad de la base")
        config = json.loads(conn.execute("SELECT value FROM metadata WHERE key='config'").fetchone()[0])
        rows = [dict(r) for r in conn.execute("SELECT * FROM decisions ORDER BY campaign_id,customer_id")]
        # Recalcular toda la selección dentro de una transacción reversible y conciliar, no sólo los elegidos.
        conn.execute("BEGIN")
        compute_decisions(conn, config)
        current = [dict(r) for r in conn.execute("SELECT * FROM decisions ORDER BY campaign_id,customer_id")]
        conn.rollback()
        if rows != current:
            raise ValueError("La selección completa no concilia con las reglas")
        count = sum(r["eligible"] for r in rows)
        exported = [json.loads(l) for l in (out / "audience.jsonl").read_text(encoding="utf-8").splitlines()]
        expected = [(r["campaign_id"], r["customer_id"]) for r in rows if r["eligible"]]
        if [(r["campaign_id"], r["customer_id"]) for r in exported] != expected:
            raise ValueError("Exportación incompleta, duplicada o sin orden")
        if root is not None:
            manifest = json.loads(conn.execute("SELECT value FROM metadata WHERE key='manifest'").fetchone()[0])
            check_source_inventory(root, manifest, include_phase2=True)
            for source in manifest:
                if digest(Path(root) / source["path"]) != source["sha256"]:
                    raise ValueError("Fuente modificada: " + source["path"])
            sources_checked = True
    finally:
        conn.close()
    result = {"all_decisions_recomputed": len(rows), "selected_pairs_verified": count,
              "sources_checked": sources_checked, "sqlite_integrity": "ok"}
    write_json(out / "verification.json", result)
    return result
