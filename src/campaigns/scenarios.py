"""Reproducible catalog of profiles present in the project snapshot.

Profiles broaden manual inspection; they do not replace held-out evaluation
or represent a statistical sample of all customers.
"""

from collections import defaultdict
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import sqlite3

from .policy import timestamp
from .store import DataStore


SCENARIOS = (
    ("selected_reactivation", None, "Seleccionado para reactivación", "Selecionado para reativação"),
    ("recent_activity", "recent_activity_observed", "Movimiento conocido en los últimos 30 días", "Movimento conhecido nos últimos 30 dias"),
    ("without_consent", "marketing_consent_not_true", "Sin autorización de publicidad", "Sem autorização de publicidade"),
    ("segment_mismatch", "target_segment_mismatch", "Segmento diferente al de la campaña", "Segmento diferente ao da campanha"),
    ("frequency_7d", "frequency_limit_7d", "Límite de contactos de 7 días", "Limite de contatos de 7 dias"),
    ("frequency_30d", "frequency_limit_30d", "Límite de contactos de 30 días", "Limite de contatos de 30 dias"),
    ("no_savings_account", "savings_account_not_found", "Sin cuenta de ahorro registrada", "Sem conta poupança registrada"),
    ("unreliable_account", "account_data_unreliable", "Cuenta con fechas incoherentes", "Conta com datas incoerentes"),
    ("future_profile", "profile_after_demo", "Perfil actualizado después de la fecha de análisis", "Perfil atualizado após a data de análise"),
    ("prior_activity_unknown", "prior_activity_unknown", "Actividad previa no verificable", "Atividade anterior não verificável"),
    ("account_not_active", "account_not_active_in_snapshot", "Cuenta no activa en la fuente", "Conta não ativa na fonte"),
    ("unreliable_recent_activity", "recent_activity_unreliable", "Movimiento reciente en cuarentena", "Movimento recente em quarentena"),
    ("confirmed_marketing_optout", None, "Retiro confirmado del permiso de publicidad", "Retirada confirmada da autorização de publicidade"),
)

REASONS = {reason: {"es": es, "pt": pt} for _, reason, es, pt in SCENARIOS if reason}
REASONS.update({
    "customer_status_not_allowed_for_objective": {"es": "Estado de cliente no permitido", "pt": "Estado de cliente não permitido"},
    "registration_not_known_by_demo": {"es": "Registro posterior o desconocido", "pt": "Registro posterior ou desconhecido"},
    "profile_after_dataset_cutoff": {"es": "Perfil posterior al corte de datos", "pt": "Perfil posterior ao corte dos dados"},
    "customer_data_invalid": {"es": "Datos del cliente no confiables", "pt": "Dados do cliente não confiáveis"},
    "profile_timestamp_invalid": {"es": "Fechas del perfil inválidas", "pt": "Datas do perfil inválidas"},
    "recent_activity_not_observable_by_demo": {"es": "Movimiento reciente aún no disponible a la fecha de análisis", "pt": "Movimento recente ainda indisponível na data de análise"},
})


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def _write(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def _questions(scenario):
    if scenario == "confirmed_marketing_optout":
        return {"es": ["No quiero recibir publicidad", "Confirmar"],
                "pt": ["Não quero receber publicidade", "Confirmar"]}
    return {"es": ["¿Por qué me corresponde o no esta campaña?", "¿Cuáles son mis últimos movimientos registrados?"],
            "pt": ["Por que esta campanha corresponde ou não ao meu perfil?", "Quais são meus últimos movimentos registrados?"]}


def build_catalog(prepared_path, out):
    """Choose distinct original customers without changing data or service state."""
    prepared_path, out = Path(prepared_path).resolve(), Path(out).resolve()
    if prepared_path == out or prepared_path in out.parents:
        raise ValueError("La salida debe ser un directorio separado de la base")
    snapshot_hash = digest(prepared_path)
    store = DataStore(prepared_path)
    aggregate = defaultdict(lambda: {"valid_known_transactions": 0, "recent_30d_count": 0,
                                     "unreliable_recent_records": 0, "unobservable_recent_records": 0})
    customers, pool, original_accounts = {}, [], defaultdict(list)
    conn = sqlite3.connect(f"{prepared_path.as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        for row in conn.execute("SELECT * FROM customers"):
            customers[row["customer_id"]] = dict(row)
        for row in conn.execute("SELECT * FROM accounts ORDER BY product_id"):
            original_accounts[row["customer_id"]].append(dict(row))
        for row in conn.execute("""SELECT v.* FROM activity v JOIN accounts a USING(product_id)
                                   WHERE a.quality_flags='[]'"""):
            totals = aggregate[row["customer_id"]]
            for field in totals:
                totals[field] += row[field]
        for row in conn.execute("SELECT * FROM decisions ORDER BY campaign_id,customer_id"):
            value = dict(row)
            value.update(reasons=json.loads(row["reasons"]), eligible=bool(row["eligible"]),
                         account_ids=json.loads(row["account_ids"]))
            if value["eligible"] != (not value["reasons"]):
                raise ValueError("Decisión incoherente: elegibilidad y motivos contradictorios")
            value["activity"] = dict(aggregate[row["customer_id"]])
            pool.append(value)
    finally:
        conn.close()
    # One customer can have several decisions. Count category coverage by unique
    # customer and select one concrete customer/campaign pair per scenario.
    used, profiles, categories = set(), [], []
    at = timestamp(store.config["demo_at"])
    if at is None:
        raise ValueError("Fecha de análisis inválida")
    recent_boundary = at - timedelta(days=30)
    for scenario, reason, label_es, label_pt in SCENARIOS:
        matching = ([row for row in pool if row["eligible"]] if reason is None
                    else [row for row in pool if reason in row["reasons"]])
        categories.append({"scenario": scenario, "label": {"es": label_es, "pt": label_pt},
                           "available_customers": len({r["customer_id"] for r in matching}),
                           "selected_customer_id": None})
        candidates = [row for row in matching if row["customer_id"] not in used]
        # Prefer a clear exclusion with few simultaneous reasons. For activity
        # examples, prefer a meaningful history while keeping ordering stable.
        def preference(row):
            customer = customers[row["customer_id"]]
            registered, updated = timestamp(customer["registration_date"]), timestamp(customer["last_updated"])
            usable = bool(not json.loads(customer["quality_flags"]) and registered and updated and registered <= updated <= at)
            return (0 if usable else 1, len(row["reasons"]),
                    -row["activity"]["valid_known_transactions"], row["customer_id"], row["campaign_id"])
        if not candidates:
            categories[-1]["status"] = "not_available_in_source" if not matching else "no_unused_customer_available"
            continue
        chosen = min(candidates, key=preference)
        customer_id = chosen["customer_id"]
        used.add(customer_id)
        customer = store.get_customer(customer_id)
        accounts, activity = store.get_accounts(customer_id), store.get_activity(customer_id)
        for row in activity["evidence"]:
            date = timestamp(row["transaction_date"])
            if date is None or date > at or len(row["raw_row_sha256"] or "") != 64:
                raise ValueError("Evidencia de actividad inválida o posterior a la fecha de análisis")
        if scenario == "recent_activity" and not any(timestamp(e["transaction_date"]) >= recent_boundary for e in activity["evidence"]):
            raise ValueError("La categoría de actividad reciente no tiene evidencia accesible")
        expected = {"selection": "selected" if chosen["eligible"] else "excluded",
                    "profile_available": customer["profile_available"],
                    "account_count_reliable": len(accounts),
                    "last_known_transaction": activity["last_observed_transaction"],
                    "recent_30d_count": activity["recent_30d_count"],
                    "valid_known_transactions": activity["valid_known_transactions"],
                    "service": "resolve_from_snapshot" if customer["profile_available"] else "deny_unavailable_historical_profile"}
        if scenario == "confirmed_marketing_optout":
            expected.update(action="marketing_optout", requires_confirmation=True,
                            eligible_after_confirmed_action=False, original_data_modified=False,
                            overlay_scope="service_state_only", reset_requires_fresh_service_state=True)
        profiles.append({
            "username": f"escenario{len(profiles) + 1:02d}", "customer_id": customer_id,
            "campaign_id": chosen["campaign_id"], "scenario": scenario,
            "label": {"es": label_es, "pt": label_pt},
            "expected_eligible": chosen["eligible"], "expected_reasons": chosen["reasons"],
            "reason_labels": {code: REASONS.get(code, {"es": code, "pt": code}) for code in chosen["reasons"]},
            "questions": _questions(scenario), "expected": expected,
            "customer_evidence": customer["evidence"],
            "accounts": [{"product_id": a["product_id"], "product_status": a["product_status"],
                          "opening_date": a["opening_date"], "evidence": a["evidence"]} for a in accounts],
            "quarantined_accounts": [{"product_id": a["product_id"], "quality_flags": json.loads(a["quality_flags"]),
                                      "evidence": {"file": a["source_file"], "record": a["source_row"],
                                                   "row_sha256": a["raw_row_sha256"]}}
                                     for a in original_accounts[customer_id] if a["quality_flags"] != "[]"],
            "latest_known_transactions": activity["evidence"],
            "activity_caveats": activity["quality_caveats"],
        })
        categories[-1].update(selected_customer_id=customer_id, status="covered")
    if digest(prepared_path) != snapshot_hash:
        raise RuntimeError("La base cambió durante la generación; vuelve a construir el catálogo")
    catalog = {
        "schema_version": 1, "analysis_at": store.config["demo_at"],
        "analysis_timezone_assumption": store.config.get("demo_timezone_assumption"),
        "country": store.config["country"], "product": store.config["product"],
        "source_version": store.source_version, "prepared_sha256": snapshot_hash,
        "policy_version": store.config.get("policy_version"),
        "profile_origin": "original_organizer_synthetic_records",
        "profiles": profiles,
        "coverage": {"evaluated_customer_campaign_pairs": len(pool),
                     "evaluated_unique_customers": len({row["customer_id"] for row in pool}),
                     "selected_unique_customers": len(used), "requested_categories": len(SCENARIOS),
                     "covered_categories": sum(c["status"] == "covered" for c in categories),
                     "categories": categories, "category_counts_overlap": True,
                     "not_statistical_sample": True, "reserved_evaluation_replaced": False},
        "limits": ["Snapshot fijo: últimos movimientos significa últimos registros confiables conocidos a analysis_at.",
                   "La ausencia de movimientos no demuestra inactividad real.",
                   "Estos perfiles cubren ramas de reglas, no todos los clientes ni todos los fallos.",
                   "El retiro de publicidad es una acción confirmada en el estado local; el catálogo no la ejecuta.",
                   "No se inventan cuentas, movimientos ni clientes para completar categorías ausentes."],
    }
    out.mkdir(parents=True, exist_ok=True)
    _write(out / "catalog.json", catalog)
    rows = ["# Perfiles de inspección", "", f"Fecha de análisis: {catalog['analysis_at']}.", "",
            "Los perfiles proceden de la base preparada. La selección es determinista y no modifica fuentes ni permisos.", "",
            "| Usuario | Cliente | Escenario | Elegible | Movimientos conocidos |", "| --- | --- | --- | --- | --- |"]
    for profile in profiles:
        rows.append(f"| {profile['username']} | {profile['customer_id']} | {profile['label']['es']} | {'Sí' if profile['expected_eligible'] else 'No'} | {profile['expected']['valid_known_transactions']} |")
    rows.extend(["", f"Cobertura: {catalog['coverage']['covered_categories']}/{len(SCENARIOS)} categorías; {len(used)} clientes distintos.", ""])
    for category in categories:
        if category["status"] != "covered":
            rows.append(f"- {category['label']['es']}: {category['status']}.")
    rows.extend(["", *[f"- {limit}" for limit in catalog["limits"]], ""])
    (out / "report.md").write_text("\n".join(rows), encoding="utf-8")
    _write(out / "artifact_hashes.json", {name: digest(out / name) for name in ("catalog.json", "report.md")})
    return catalog
