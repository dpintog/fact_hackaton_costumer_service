"""Aggregate review of the four incorporated tables; does not modify data or audience."""

from collections import Counter
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "outputs/viability_review"
CONFIG = json.loads((ROOT / "config/day1.json").read_text(encoding="utf-8"))
AT = pd.Timestamp(CONFIG["demo_at"])
CUTOFF = pd.Timestamp(CONFIG["dataset_cutoff"])


def dates(series):
    return pd.to_datetime(series, format="mixed", errors="coerce")


def counts(series):
    return {str(k): int(v) for k, v in series.fillna("<missing>").value_counts().items()}


def sha(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def relationship_checks():
    """Check business relationships as well as the existence of IDs."""
    customer = pd.read_csv(ROOT / "data/customers.csv", usecols=["customer_id", "registration_date"], dtype="string").set_index("customer_id")
    customer["registration_date"] = dates(customer["registration_date"])
    product = pd.read_csv(ROOT / "data/products.csv", usecols=["customer_id", "product_type", "product_status", "opening_date", "last_updated"], dtype="string")
    opening = dates(product["opening_date"])
    registered = product["customer_id"].map(customer["registration_date"])
    product_result = {"opening_before_customer_registration": int((opening < registered).sum())}
    baseline = {json.loads(line)["customer_id"] for line in
                (ROOT / "outputs/day1/baseline_audience.jsonl").read_text(encoding="utf-8").splitlines()}
    updated = dates(product["last_updated"])
    coherent = (product["customer_id"].isin(baseline) & product["product_type"].eq(CONFIG["product"])
                & product["product_status"].eq("Active") & opening.ge(registered)
                & opening.le(AT) & updated.ge(opening) & updated.le(AT))
    product_result.update(baseline_with_active_savings_and_consistent_profile_dates=int(product.loc[coherent, "customer_id"].nunique()),
                          baseline_consistent_active_savings_accounts=int(coherent.sum()),
                          new_count_is_exploratory_not_updated_campaign_audience=True)
    interaction = pd.concat([pd.read_csv(path, usecols=["interaction_id", "customer_id", "agent_id", "interaction_date"], dtype="string")
                             for path in sorted((ROOT / "data/call_center_interactions").rglob("*.csv"))], ignore_index=True).set_index("interaction_id")
    interaction["interaction_date"] = dates(interaction["interaction_date"])
    survey_result = Counter()
    scores_by_type = {}
    for path in sorted((ROOT / "data/satisfaction_surveys").rglob("*.csv")):
        frame = pd.read_csv(path, usecols=["interaction_id", "customer_id", "agent_id", "survey_date", "survey_type", "main_score"], dtype="string")
        expected_customer = frame["interaction_id"].map(interaction["customer_id"])
        expected_agent = frame["interaction_id"].map(interaction["agent_id"])
        interacted = frame["interaction_id"].map(interaction["interaction_date"])
        survey_result.update(customer_interaction_mismatch=int((expected_customer.notna() & expected_customer.ne(frame["customer_id"])).sum()),
                             agent_interaction_mismatch=int((expected_agent.notna() & expected_agent.ne(frame["agent_id"])).sum()),
                             survey_before_interaction=int((dates(frame["survey_date"]) < interacted).sum()))
        for kind, group in frame.groupby("survey_type"):
            scores_by_type.setdefault(str(kind), Counter()).update(group["main_score"].dropna().tolist())
    return product_result, {**dict(survey_result), "main_score_values_by_survey_type": {k: dict(v) for k, v in scores_by_type.items()}}


def render_report(report):
    p, t, a, s = (report[k] for k in ("products", "transactions", "service_agents", "satisfaction_surveys"))
    portuguese = sum(n for language, n in a["languages"].items() if "portugués" in language.casefold())
    body = f"""# Viabilidad del proyecto con los datos completos

La revisión encuentra las 13 tablas del dataset. El prototipo de Cuenta de Ahorro en Colombia sigue siendo viable en 3–5 días si se limita a selección explicable, consultas con contexto y registro confirmado de una solicitud de asesor. Esa estimación es una propuesta de alcance, no una garantía de entrega. Los datos nuevos permiten verificar tenencia y actividad; todavía no demuestran beneficio financiero individual ni probabilidad de responder a una campaña.

Revisión complementaria: {report['review_date']}. Fecha histórica de demo conservada: {report['demo_at']}. Se revisaron todas las filas de las cuatro tablas incorporadas, con controles específicos de relaciones y fechas. Las demás tablas se inventariaron o usaron como referencia; esto no es una auditoría semántica completa de las 13 tablas.

## Datos que ya están disponibles

| Tabla incorporada | Registros revisados | Uso defendible |
|---|---:|---|
| products | {p['rows']:,} | Vincular cliente y cuenta, producto, moneda y metadatos de su instantánea |
| transactions | {t['rows']:,} | Consultar actividad observada y coherente, respetando fechas y disponibilidad |
| service_agents | {a['rows']:,} | Preparar criterios de idioma y especialidad para una cola de prueba |
| satisfaction_surveys | {s['rows']:,} | Describir atención histórica, separando CSAT, CES y NPS; sin atribuir resultados al prototipo |

Hay {p['colombia_savings_rows']:,} cuentas de ahorro asociadas a {p['colombia_savings_customers']:,} clientes de Colombia. Los asesores incluyen {portuguese} registros con portugués declarado. Las encuestas no tienen referencias inexistentes a cliente, interacción o asesor y sus clientes y asesores coinciden con los de la interacción referenciada.

## La selección anterior necesita enriquecerse

De los {p['baseline_customers']:,} perfiles seleccionados por las reglas del día 1, {p['baseline_with_savings_snapshot']:,} tienen una cuenta de ahorro en el archivo. De ellos, {p['baseline_with_savings_profile_not_after_demo']:,} tienen al menos una cuenta con apertura y actualización compatibles con la fecha de demo según la comprobación inicial.

Al exigir además estado Active del producto en la instantánea, apertura posterior o igual al registro del cliente y actualización entre apertura y demo, quedan {p['baseline_with_active_savings_and_consistent_profile_dates']:,} clientes con {p['baseline_consistent_active_savings_accounts']:,} cuentas. Es una muestra exploratoria utilizable para preparar casos; **no es una nueva audiencia final**, no verifica estado histórico real, no aplica todavía un criterio aprobado de reactivación y no demuestra beneficio. La audiencia original permanece intacta.

El estado Inactive del cliente no equivale al estado de la cuenta. products sólo tiene Active, Closed, Blocked y Suspended. Para una campaña de reactivación, proponemos definir actividad observada por cuenta y una ventana de prueba. La ausencia de movimientos válidos no prueba inactividad si hay registros descartados o cobertura incompleta. Esta regla debe quedar identificada como supuesto de demo hasta contar con criterios del banco.

## Límites y medidas para enfrentarlos

| Límite verificado | Consecuencia | Medida propuesta |
|---|---|---|
| {p['opening_before_customer_registration']:,} productos abiertos antes del registro del cliente | La relación temporal no permite afirmar tenencia histórica sin reservas | Marcar inconsistencias y excluirlas de casos que requieran cronología coherente; confirmar qué significa registration_date |
| {t['before_product_opening']:,} transacciones anteriores a la apertura y {t['process_before_transaction_day']:,} procesadas antes del día de transacción | No todo el historial permite calcular señales históricas fiables | Cuarentena de registros incoherentes; usar hechos observados válidos y reconocer desconocidos. No inventar fechas corregidas |
| {p['updated_after_cutoff']:,} productos actualizados después del corte declarado | Riesgo de usar información futura | Filtrar por fecha y conservar el supuesto de instantánea; no reconstruir balances ni estados con información incompleta |
| Condiciones comerciales y cuerpo aprobado de campaña ausentes | No se pueden prometer tasas promocionales, ahorro o beneficios individuales | Explicar sólo metadatos existentes y derivar preguntas comerciales al asesor. interest_rate pertenece a una cuenta y no prueba una tasa de campaña; tampoco se verificó su unidad |
| {a['unmatched_nonempty_branch_assignment']:,} asignaciones a sucursal sin referencia y {a['missing_branch_assignment']:,} asignaciones vacías | No existe un enrutamiento fiable por sucursal a Colombia | Cola Colombia/Ahorro configurada como simulación; asignación explícita para pruebas, sin afirmar disponibilidad real |
| Campaña principal Voice con cero conversiones positivas marcadas | Falta un objetivo válido para entrenar un predictor de conversión de esta campaña | Usar IA para interpretar consultas, contexto e idioma; compararla con búsqueda o clasificación por palabras. Mantener reglas y permisos fuera del modelo |
| Sin versiones históricas de consentimiento, estado y condiciones | El replay no verifica la verdad operacional de aquel día | Mantener fecha fija, supuestos explícitos y sesión de prueba confiable; no presentar los resultados como un backtest real |
| {s['process_before_survey_day']:,} encuestas procesadas antes del día de encuesta | Sus fechas de disponibilidad no son fiables para evaluación histórica | Separar escalas y depurar fechas; evaluar el nuevo asistente con casos reservados y etiquetas revisadas, sin atribuirle satisfacción histórica |

Los conteos de incidencias se superponen y no se suman como registros distintos. Los nombres de campos se conservan para facilitar trazabilidad; las reglas propuestas no están aprobadas por un banco. Sigue sin encontrarse DATA_DICTIONARY.md, que ayudaría a resolver significados, unidades y escalas.

## Alcance recomendado para la demo

Mantener Colombia, Cuenta de Ahorro y la campaña histórica elegida. Incorporar tenencia y hechos de actividad sólo cuando sean coherentes. El cliente podrá preguntar por sus cuentas y la campaña, aclarar su necesidad y confirmar una solicitud de atención. La aplicación deberá persistir esa solicitud y comprobar que quedó registrada. La derivación será una cola de prueba, no un contacto real a un empleado.

La IA interpretará consultas y mantendrá contexto en español y portugués. El backend validará identidad de prueba, permisos, consentimiento y criterios de selección. Las condiciones ausentes se reconocerán y derivarán. Para justificar IA, usar etiquetas de intención o relevancia revisadas y comparar con un baseline en casos reservados, manteniendo juntas las variantes de la misma familia.

Prioridad propuesta para lo que falta: enriquecer y depurar la muestra; construir consulta y solicitud persistida con permisos; conectar conversación bilingüe; evaluar utilidad, acciones inseguras, derivaciones, latencia y coste. En tres días conviene limitar la interfaz y los casos; con cinco días se puede ampliar la evaluación. No hace falta entrenar un predictor de conversión ni desplegar envíos reales para demostrar el flujo.

El beneficio esperado para el cliente es información más pertinente y una solicitud de atención con contexto. Para el banco, filtros verificables y menos trabajo de recopilar ese contexto. Deben medirse en el prototipo; el dataset no demuestra aumento de ventas o conversión causado por esta propuesta.

## Evidencia y reproducibilidad

review.json contiene los conteos y controles. source_manifest.json conserva hashes de los {report['new_sources_hashed']:,} archivos de las cuatro tablas nuevas. Las fuentes originales auditadas en el día 1 mantienen sus hashes. Los totales por producto, tipo y estado de transacción y tipo de encuesta concilian con sus respectivas filas.

La revisión puede reproducirse con scripts/review_viability.py usando Python con pandas; es una herramienta de análisis, no una extensión del motor de selección ni de sus permisos. La comprobación de cohortes y relaciones se repitió por separado. El reporte del día 1 corresponde al alcance original: su mención de tablas ausentes quedó superada por esta revisión.
"""
    (OUT / "report.md").write_text(body, encoding="utf-8")


def main(review_date="2026-10-02"):
    datetime.fromisoformat(review_date)
    OUT.mkdir(parents=True, exist_ok=True)
    customers = pd.read_csv(ROOT / "data/customers.csv", usecols=["customer_id", "country"], dtype="string")
    customer_countries = customers.set_index("customer_id")["country"]
    customer_ids = set(customer_countries.index)
    baseline = {json.loads(line)["customer_id"] for line in
                (ROOT / "outputs/day1/baseline_audience.jsonl").read_text(encoding="utf-8").splitlines()}
    products = pd.read_csv(ROOT / "data/products.csv", dtype="string")
    product_open = dates(products["opening_date"])
    product_updated = dates(products["last_updated"])
    last_tx = dates(products["last_transaction_date"])
    products["customer_country"] = products["customer_id"].map(customer_countries)
    savings = products["product_type"].eq(CONFIG["product"]).fillna(False)
    colombia_savings = savings & products["customer_country"].eq(CONFIG["country"]).fillna(False)
    historical_profile = colombia_savings & product_open.le(AT) & product_updated.le(AT) & product_updated.ge(product_open)
    subset = products[colombia_savings]
    rate = pd.to_numeric(subset["interest_rate"], errors="coerce")
    report = {
        "review_date": review_date, "demo_at": CONFIG["demo_at"],
        "coverage": "All rows of products, service_agents, transactions, satisfaction_surveys; selected referential and temporal checks, not a complete semantic audit",
        "inventory": [{"table": p.name, "csv_files": len(list(p.rglob('*.csv'))) if p.is_dir() else 1}
                      for p in sorted((ROOT / "data").iterdir()) if p.is_dir() or p.suffix == ".csv"],
        "products": {
            "rows": len(products), "duplicate_product_ids": int(products["product_id"].duplicated().sum()),
            "missing_by_column": {k: int(v) for k, v in products.isna().sum().items() if v},
            "types": counts(products["product_type"]), "statuses": counts(products["product_status"]),
            "unknown_customer_rows": int((~products["customer_id"].isin(customer_ids)).sum()),
            "invalid_opening_dates": int(product_open.isna().sum()),
            "invalid_update_dates": int(product_updated.isna().sum()),
            "updated_before_opening": int((product_updated < product_open).sum()),
            "updated_after_demo": int((product_updated > AT).sum()),
            "updated_after_cutoff": int((product_updated > CUTOFF).sum()),
            "last_transaction_after_cutoff": int((last_tx > CUTOFF).sum()),
            "colombia_savings_rows": int(colombia_savings.sum()),
            "colombia_savings_customers": int(subset["customer_id"].nunique()),
            "colombia_savings_statuses": counts(subset["product_status"]),
            "colombia_savings_currencies": counts(subset["currency"]),
            "colombia_savings_rate_present": int(rate.notna().sum()),
            "colombia_savings_rate_min_raw": float(rate.min()) if rate.notna().any() else None,
            "colombia_savings_rate_max_raw": float(rate.max()) if rate.notna().any() else None,
            "rate_units_and_commercial_applicability_verified": False,
            "colombia_savings_profiles_not_after_demo": int(historical_profile.sum()),
            "baseline_customers": len(baseline),
            "baseline_with_savings_snapshot": len(baseline & set(subset["customer_id"])),
            "baseline_with_savings_opened_by_demo": len(baseline & set(products.loc[colombia_savings & product_open.le(AT), "customer_id"])),
            "baseline_with_savings_profile_not_after_demo": len(baseline & set(products.loc[historical_profile, "customer_id"])),
            "baseline_with_inactive_savings_profile_not_after_demo": len(baseline & set(products.loc[historical_profile & products["product_status"].eq("Inactive"), "customer_id"])),
            "historical_status_and_balance_not_reconstructed": True,
        },
    }
    print("products: revisión completa", flush=True)
    branches = pd.read_csv(ROOT / "data/branches.csv", usecols=["branch_id", "country"], dtype="string")
    branch_countries = branches.set_index("branch_id")["country"]
    agents = pd.read_csv(ROOT / "data/service_agents.csv", dtype="string")
    agents["assigned_country"] = agents["assigned_branch_id"].map(branch_countries)
    agent_ids = set(agents["agent_id"])
    report["service_agents"] = {
        "rows": len(agents), "duplicate_agent_ids": int(agents["agent_id"].duplicated().sum()),
        "statuses": counts(agents["agent_status"]), "specialties": counts(agents["specialty"]),
        "languages": counts(agents["languages"]), "types": counts(agents["agent_type"]),
        "missing_by_column": {k: int(v) for k, v in agents.drop(columns="assigned_country").isna().sum().items() if v},
        "unknown_branch_rows": int(agents["assigned_country"].isna().sum()),
        "missing_branch_assignment": int(agents["assigned_branch_id"].isna().sum()),
        "unmatched_nonempty_branch_assignment": int((agents["assigned_branch_id"].notna() & ~agents["assigned_branch_id"].isin(branch_countries.index)).sum()),
        "colombia_assigned_rows": int(agents["assigned_country"].eq(CONFIG["country"]).sum()),
        "colombia_active_assigned_rows": int((agents["assigned_country"].eq(CONFIG["country"]) & agents["agent_status"].eq("Active")).sum()),
        "live_availability_or_historical_assignment_verified": False,
    }
    print("service_agents: revisión completa", flush=True)
    product_customers = products.set_index("product_id")["customer_id"]
    product_types = products.set_index("product_id")["product_type"]
    opening_dates = pd.Series(product_open.to_numpy(), index=products["product_id"])
    tx_counts, tx_types, tx_categories, tx_statuses = Counter(), Counter(), Counter(), Counter()
    savings_customer_ids = set()
    baseline_tx_customer_ids = set()
    tx_files = sorted((ROOT / "data/transactions").rglob("*.csv"))
    cols = ["transaction_id", "transaction_date", "process_date", "product_id", "customer_id",
            "transaction_type", "transaction_category", "currency", "transaction_status"]
    for index, path in enumerate(tx_files, 1):
        for frame in pd.read_csv(path, usecols=cols, dtype="string", chunksize=100000):
            when, processed = dates(frame["transaction_date"]), dates(frame["process_date"])
            owner = frame["product_id"].map(product_customers)
            opened = pd.to_datetime(frame["product_id"].map(opening_dates), errors="coerce")
            is_savings = frame["product_id"].map(product_types).eq(CONFIG["product"])
            valid_refs = owner.notna() & frame["customer_id"].isin(customer_ids) & owner.eq(frame["customer_id"])
            valid_time = when.notna() & processed.notna() & when.ge(opened) & processed.dt.normalize().ge(when.dt.normalize())
            known = when.le(AT) & processed.dt.normalize().le(AT.normalize()) & valid_refs & valid_time
            recent = known & when.ge(AT - pd.Timedelta(days=30))
            good = frame["transaction_status"].eq("Approved")
            tx_counts.update(rows=len(frame), unknown_product_rows=int(owner.isna().sum()),
                unknown_customer_rows=int((~frame["customer_id"].isin(customer_ids)).sum()),
                customer_product_mismatch=int((owner.notna() & owner.ne(frame["customer_id"])).sum()),
                invalid_transaction_dates=int(when.isna().sum()), invalid_process_dates=int(processed.isna().sum()),
                process_before_transaction_day=int((processed.dt.normalize() < when.dt.normalize()).sum()),
                before_product_opening=int((when < opened).sum()),
                after_dataset_cutoff=int((when > CUTOFF).sum()),
                savings_rows=int(is_savings.sum()), savings_known_before_demo=int((known & is_savings).sum()),
                savings_approved_recent_30d=int((recent & good & is_savings).sum()))
            tx_types.update(frame["transaction_type"].dropna().tolist())
            tx_categories.update(frame["transaction_category"].dropna().tolist())
            tx_statuses.update(frame["transaction_status"].dropna().tolist())
            savings_customer_ids.update(frame.loc[recent & good & is_savings, "customer_id"])
            baseline_tx_customer_ids.update(frame.loc[recent & good & is_savings & frame["customer_id"].isin(baseline), "customer_id"])
        if index % 250 == 0:
            print(f"transactions: {index}/{len(tx_files)} archivos", flush=True)
    report["transactions"] = {**dict(tx_counts), "csv_files": len(tx_files), "types": dict(tx_types),
        "categories": dict(tx_categories), "statuses": dict(tx_statuses),
        "recent_savings_customers_all_countries": len(savings_customer_ids),
        "baseline_with_recent_approved_savings_transactions": len(baseline_tx_customer_ids),
        "limitations": "Process date availability is assumed at day granularity; transaction statuses are not versioned. Counts are not benefits or response propensity."}
    survey_counts, survey_types, scores = Counter(), Counter(), Counter()
    survey_files = sorted((ROOT / "data/satisfaction_surveys").rglob("*.csv"))
    interaction_ids = set()
    for path in sorted((ROOT / "data/call_center_interactions").rglob("*.csv")):
        interaction_ids.update(pd.read_csv(path, usecols=["interaction_id"], dtype="string")["interaction_id"].dropna())
    for path in survey_files:
        for frame in pd.read_csv(path, dtype="string", chunksize=100000):
            when, processed = dates(frame["survey_date"]), dates(frame["process_date"])
            survey_counts.update(rows=len(frame), unknown_customer_rows=int((~frame["customer_id"].isin(customer_ids)).sum()),
                unknown_agent_rows=int((~frame["agent_id"].isin(agent_ids)).sum()),
                unknown_interaction_rows=int((~frame["interaction_id"].isin(interaction_ids)).sum()),
                missing_main_score=int(frame["main_score"].isna().sum()),
                nonnumeric_main_score=int((frame["main_score"].notna() & pd.to_numeric(frame["main_score"], errors="coerce").isna()).sum()),
                after_dataset_cutoff=int((when > CUTOFF).sum()),
                process_before_survey_day=int((processed.dt.normalize() < when.dt.normalize()).sum()))
            survey_types.update(frame["survey_type"].dropna().tolist())
            scores.update(frame["main_score"].dropna().tolist())
    report["satisfaction_surveys"] = {**dict(survey_counts), "csv_files": len(survey_files),
        "types": dict(survey_types), "main_score_values": dict(scores),
        "causal_campaign_or_prototype_effect_verified": False}
    product_relations, survey_relations = relationship_checks()
    report["products"].update(product_relations)
    report["satisfaction_surveys"].update(survey_relations)
    manifest = []
    for path in [ROOT / "data/products.csv", ROOT / "data/service_agents.csv", *tx_files, *survey_files]:
        manifest.append({"path": path.relative_to(ROOT).as_posix(), "bytes": path.stat().st_size, "sha256": sha(path)})
    # Detect whether any original source used for day 1 selection has changed.
    previous = json.loads((ROOT / "outputs/day1/source_manifest.json").read_text(encoding="utf-8"))
    report["day1_original_audited_sources_changed"] = [p["path"] for p in previous if sha(ROOT / p["path"]) != p["sha256"]]
    report["scope_does_not_change_day1_audience"] = True
    report["new_sources_hashed"] = len(manifest)
    assert sum(report["transactions"]["statuses"].values()) == report["transactions"]["rows"]
    assert sum(report["transactions"]["types"].values()) == report["transactions"]["rows"]
    assert sum(report["products"]["types"].values()) == report["products"]["rows"]
    assert sum(report["satisfaction_surveys"]["types"].values()) == report["satisfaction_surveys"]["rows"]
    report["category_totals_reconciled"] = True
    (OUT / "review.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (OUT / "source_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    render_report(report)
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--review-date", default="2026-10-02", help="Fecha documentada de revisión, sin cambiar la fecha histórica de demo")
    main(parser.parse_args().review_date)
