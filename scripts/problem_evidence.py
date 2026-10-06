"""Demand aggregates and transcript limitations; never modifies the sources."""

import argparse
from collections import Counter
import csv
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import sys
import unicodedata

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from campaigns.policy import boolean, country_name, timestamp  # noqa: E402

INTERACTION_FIELDS = {"interaction_id", "interaction_date", "process_date", "customer_id", "agent_id",
                      "channel", "contact_reason", "reason_category", "was_resolved", "was_escalated",
                      "requires_followup", "mentioned_products"}
TRANSCRIPT_FIELDS = {"transcript_id", "interaction_id", "process_date", "customer_id", "agent_id",
                     "full_text", "detected_language"}


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1_048_576), b""):
            result.update(block)
    return result.hexdigest()


def plain(value):
    value = unicodedata.normalize("NFKD", value or "").casefold()
    return " ".join("".join(char for char in value if not unicodedata.combining(char)).split())


def clean(value):
    value = (value or "").strip()
    return "" if value.casefold() in {"nan", "none", "null", "nat"} else value


def read_rows(root, path, required, manifest):
    before = path.stat()
    count = 0
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        header = reader.fieldnames or []
        if len(header) != len(set(header)) or not required.issubset(header):
            raise ValueError("Contrato de evidencia inválido: " + str(path.relative_to(root)))
        for row in reader:
            if None in row:
                raise ValueError("Registro CSV mal formado")
            count += 1
            yield row
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError("Fuente cambió durante lectura")
    manifest.append({"path": path.relative_to(root).as_posix(), "rows": count,
                     "bytes": after.st_size, "sha256": digest(path), "columns": header})


def metrics():
    return {"n": 0, "reason_category": Counter(), "contact_reason": Counter(), "channel": Counter(),
            "was_resolved": Counter(), "was_escalated": Counter(), "requires_followup": Counter(),
            "derived_reason_groups": Counter(), "rows_with_product_references": 0,
            "savings_reference_same_customer_snapshot_rows": 0,
            "savings_reference_same_customer_coherent_demo_dates_rows": 0,
            "product_reference_count": 0, "product_reference_missing_count": 0,
            "product_reference_customer_mismatch_count": 0}


def add_metrics(target, row, products):
    target["n"] += 1
    for field in ("reason_category", "contact_reason", "channel"):
        target[field][clean(row[field]) or "<missing>"] += 1
    for field in ("was_resolved", "was_escalated", "requires_followup"):
        value = boolean(row[field])
        target[field]["true" if value == 1 else "false" if value == 0 else "missing_or_invalid"] += 1
    reason = plain(row["contact_reason"])
    if re.search(r"campan|publicid|promoc|marketing|ofert", reason):
        target["derived_reason_groups"]["campaign_marketing_reason_lexical"] += 1
    if re.search(r"saldo|cuenta|conta|ahorr|poupanca|movim|transac|extract|extrat", reason):
        target["derived_reason_groups"]["account_activity_reason_lexical"] += 1
    references = {clean(value) for value in clean(row["mentioned_products"]).split(",") if clean(value)}
    target["rows_with_product_references"] += bool(references)
    target["product_reference_count"] += len(references)
    matching_savings, coherent_savings = False, False
    for reference in references:
        product = products.get(reference)
        if product is None:
            target["product_reference_missing_count"] += 1
        elif product[0] != clean(row["customer_id"]):
            target["product_reference_customer_mismatch_count"] += 1
        elif product[1] == "Cuenta Ahorro":
            matching_savings = True
            coherent_savings |= product[2]
    target["savings_reference_same_customer_snapshot_rows"] += matching_savings
    target["savings_reference_same_customer_coherent_demo_dates_rows"] += coherent_savings


def finalized_metrics(value):
    output = {key: dict(item) if isinstance(item, Counter) else item for key, item in value.items()}
    output["observed_flag_rates"] = {}
    for field in ("was_resolved", "was_escalated", "requires_followup"):
        counter = value[field]
        valid = counter["true"] + counter["false"]
        output["observed_flag_rates"][field] = {
            "numerator": counter["true"], "valid_boolean_denominator": valid,
            "rate": counter["true"] / valid if valid else None,
            "missing_or_invalid": counter["missing_or_invalid"],
        }
    return output


def template_signature(text):
    # Author-defined surface normalization detects repetition, not semantic
    # equivalence. Signatures/texts are never exported; only distinct counts.
    value = plain(text)
    value = re.sub(r"[\w.+-]+@[\w.-]+\.[a-z]{2,}", "<email>", value)
    value = re.sub(r"\b(?:cus|agt|int|trn|prd|cmp)[-_][a-z0-9]+\b", "<id>", value)
    value = re.sub(r"\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b", "<id>", value)
    value = re.sub(r"<[^>]+>|\{[^}]+\}", "<placeholder>", value)
    value = re.sub(r"\b\d+(?:[.,]\d+)*\b", "<number>", value)
    return value


def file_inventory(root):
    result = {"data/customers.csv", "data/products.csv"}
    for table in ("call_center_interactions", "call_transcripts"):
        result.update(path.relative_to(root).as_posix() for path in (root / "data" / table).rglob("*.csv"))
    return result


def build(root, out, config, analysis_date):
    root, out = root.resolve(), out.resolve()
    if out == root or out == root / "data" or root / "data" in out.parents:
        raise ValueError("La evidencia debe quedar fuera de data y de la raíz")
    at, cutoff = timestamp(config["demo_at"]), timestamp(config["dataset_cutoff"])
    if at is None or cutoff is None or at > cutoff:
        raise ValueError("Fechas de demo y corte inválidas")
    datetime.fromisoformat(analysis_date)
    initial_inventory, manifest = file_inventory(root), []
    countries, registrations = {}, {}
    for row in read_rows(root, root / "data/customers.csv", {"customer_id", "country", "registration_date"}, manifest):
        identifier = clean(row["customer_id"])
        country = country_name(row["country"])
        if identifier in countries and countries[identifier] != country:
            raise ValueError("País contradictorio en customers")
        countries[identifier] = country
        registrations[identifier] = timestamp(clean(row["registration_date"]))

    products = {}
    for row in read_rows(root, root / "data/products.csv",
                         {"product_id", "customer_id", "product_type", "opening_date", "last_updated"}, manifest):
        identifier, customer = clean(row["product_id"]), clean(row["customer_id"])
        opened, updated, registered = (timestamp(clean(row["opening_date"])),
                                       timestamp(clean(row["last_updated"])), registrations.get(customer))
        coherent = bool(opened and updated and registered and registered <= opened <= updated <= at)
        metadata = (customer, clean(row["product_type"]), coherent)
        if identifier in products and products[identifier] != metadata:
            raise ValueError("Referencia de producto contradictoria")
        products[identifier] = metadata

    cohorts = {key: metrics() for key in ("all_rows", "known_by_demo", "colombia_all_rows", "colombia_known_by_demo")}
    interactions, date_quality = {}, Counter()
    interaction_paths = sorted((root / "data/call_center_interactions").rglob("*.csv"))
    if not interaction_paths:
        raise ValueError("Faltan call_center_interactions")
    first_date, last_date = None, None
    for file_index, path in enumerate(interaction_paths, 1):
        for row in read_rows(root, path, INTERACTION_FIELDS, manifest):
            interacted, processed = timestamp(clean(row["interaction_date"])), timestamp(clean(row["process_date"]))
            if interacted:
                first_date, last_date = min(first_date or interacted, interacted), max(last_date or interacted, interacted)
            identifier, customer = clean(row["interaction_id"]), clean(row["customer_id"])
            flags = []
            if not identifier:
                flags.append("interaction_id_missing")
            if interacted is None or processed is None:
                flags.append("interaction_or_processing_date_invalid")
            if interacted and processed and processed.date() < interacted.date():
                flags.append("processed_before_interaction_day")
            if interacted and interacted > cutoff:
                flags.append("interaction_after_dataset_cutoff")
            if processed and processed.date() > cutoff.date():
                flags.append("processing_after_dataset_cutoff")
            if customer not in countries:
                flags.append("customer_reference_missing")
            date_quality.update(flags)
            available = bool(not flags and interacted <= at and processed.date() <= at.date())
            metadata = (interacted, available, customer, clean(row["agent_id"]))
            if identifier:
                if identifier in interactions:
                    date_quality["duplicate_interaction_id"] += 1
                    if interactions[identifier] != metadata:
                        raise ValueError("Referencia de interacción contradictoria")
                else:
                    interactions[identifier] = metadata
            add_metrics(cohorts["all_rows"], row, products)
            if available:
                add_metrics(cohorts["known_by_demo"], row, products)
            if countries.get(customer) == config["country"]:
                add_metrics(cohorts["colombia_all_rows"], row, products)
                if available:
                    add_metrics(cohorts["colombia_known_by_demo"], row, products)
        if file_index % 300 == 0:
            print(f"interaction evidence: {file_index}/{len(interaction_paths)} archivos", flush=True)

    transcript_paths = sorted((root / "data/call_transcripts").rglob("*.csv"))
    if not transcript_paths:
        raise ValueError("Faltan call_transcripts")
    transcript_quality, languages, known_languages, exact_texts, templates = Counter(), Counter(), Counter(), Counter(), Counter()
    transcripts = {"rows": 0, "nonempty_full_text_rows": 0, "known_by_demo_rows": 0,
                   "colombia_known_by_demo_rows": 0, "rows_with_placeholder_markers": 0,
                   "rows_with_balance_keyword": 0}
    seen_transcripts = set()
    for file_index, path in enumerate(transcript_paths, 1):
        for row in read_rows(root, path, TRANSCRIPT_FIELDS, manifest):
            transcripts["rows"] += 1
            transcript_id, interaction_id = clean(row["transcript_id"]), clean(row["interaction_id"])
            if not transcript_id:
                transcript_quality["transcript_id_missing"] += 1
            elif transcript_id in seen_transcripts:
                transcript_quality["duplicate_transcript_id"] += 1
            seen_transcripts.add(transcript_id)
            language = clean(row["detected_language"]) or "<missing>"
            languages[language] += 1
            text = clean(row["full_text"])
            if text:
                transcripts["nonempty_full_text_rows"] += 1
                exact_texts[hashlib.sha256(text.encode("utf-8")).digest()] += 1
                templates[hashlib.sha256(template_signature(text).encode("utf-8")).digest()] += 1
                transcripts["rows_with_placeholder_markers"] += bool(re.search(r"<[^>]+>|\{[^}]+\}", text))
                transcripts["rows_with_balance_keyword"] += bool(re.search(r"\b(saldo|balance)\b", plain(text)))
            linked = interactions.get(interaction_id)
            processed = timestamp(clean(row["process_date"]))
            flags = []
            if linked is None:
                flags.append("interaction_reference_missing")
            if processed is None:
                flags.append("transcript_processing_date_invalid")
            if linked and (clean(row["customer_id"]) != linked[2] or clean(row["agent_id"]) != linked[3]):
                flags.append("transcript_customer_or_agent_mismatch")
            if linked and linked[0] and processed and processed.date() < linked[0].date():
                flags.append("transcript_processed_before_interaction_day")
            if processed and processed.date() > cutoff.date():
                flags.append("transcript_processing_after_dataset_cutoff")
            transcript_quality.update(flags)
            if not flags and linked[1] and processed.date() <= at.date():
                transcripts["known_by_demo_rows"] += 1
                known_languages[language] += 1
                if countries.get(linked[2]) == config["country"]:
                    transcripts["colombia_known_by_demo_rows"] += 1
        if file_index % 300 == 0:
            print(f"transcript evidence: {file_index}/{len(transcript_paths)} archivos", flush=True)
    if file_inventory(root) != initial_inventory or {item["path"] for item in manifest} != initial_inventory:
        raise ValueError("Inventario de evidencia modificado durante la lectura")
    for item in manifest:
        if digest(root / item["path"]) != item["sha256"]:
            raise ValueError("Fuente de evidencia modificada durante la revisión")
    transcripts.update(languages=dict(languages), known_by_demo_languages=dict(known_languages),
                       exact_distinct_full_texts=len(exact_texts), normalized_distinct_templates=len(templates),
                       repeated_full_text_rows=transcripts["nonempty_full_text_rows"] - len(exact_texts),
                       top_repeated_text_frequency_counts=sorted(exact_texts.values(), reverse=True)[:10],
                       template_normalization="lowercase_accent_whitespace_and_identifier_number_placeholder_replacement",
                       no_raw_text_or_text_signatures_exported=True, quality=dict(transcript_quality))
    summary = {
        "analysis_date": analysis_date, "demo_at": config["demo_at"], "dataset_cutoff": config["dataset_cutoff"],
        "source_classification": "organizer_synthetic", "country_from_customer_snapshot": config["country"],
        "interaction_files": len(interaction_paths), "transcript_files": len(transcript_paths),
        "interaction_distinct_ids": len(interactions),
        "interaction_date_range": {"min": first_date.isoformat() if first_date else None,
                                   "max": last_date.isoformat() if last_date else None},
        "interaction_quality": dict(date_quality),
        "interaction_cohorts": {key: finalized_metrics(value) for key, value in cohorts.items()},
        "transcripts": transcripts,
        "limitations": [
            "Source categories and resolution/escalation flags are observed synthetic labels, not verified service outcomes.",
            "All-row demand describes the full supplied period, not information available at the historical demo.",
            "known_by_demo excludes invalid chronology and assumes availability at processing-day granularity.",
            "Country uses an unversioned customer snapshot, not historical nationality/location reconstruction.",
            "Lexical reason groups and transcript template normalization are team-authored descriptive definitions.",
            "Product references use IDs matched to product type and customer snapshot; a mention does not prove the main request or benefit.",
            "The dataset does not establish unwanted advertising, campaign-induced complaints or causal business gains.",
            "Language/repetition counts do not validate labels for a general bilingual model; authored evaluation remains separate.",
        ],
        "verification": {"all_files_hashed_twice_and_unchanged": True, "source_inventory_unchanged": True,
                         "raw_text_exported": False, "customer_or_agent_records_exported": False},
    }
    for cohort in summary["interaction_cohorts"].values():
        for field in ("reason_category", "contact_reason", "channel", "was_resolved", "was_escalated", "requires_followup"):
            if sum(cohort[field].values()) != cohort["n"]:
                raise ValueError("Totales de interacción no concilian")
    if sum(languages.values()) != transcripts["rows"] or sum(known_languages.values()) != transcripts["known_by_demo_rows"]:
        raise ValueError("Totales de transcripción no concilian")
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (out / "source_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (out / "report.md").write_text(render_report(summary), encoding="utf-8")
    return summary


def render_report(summary):
    cohorts, transcript = summary["interaction_cohorts"], summary["transcripts"]
    all_rows, historical_co = cohorts["all_rows"], cohorts["colombia_known_by_demo"]
    rows = []
    for key, label in (("all_rows", "Todos los registros"), ("known_by_demo", "Disponibles en la demo"),
                       ("colombia_all_rows", "Colombia, todos"), ("colombia_known_by_demo", "Colombia, disponibles en la demo")):
        cohort = cohorts[key]
        flags = [cohort["observed_flag_rates"][field] for field in ("was_resolved", "was_escalated", "requires_followup")]
        rows.append(f"| {label} | {cohort['n']:,} | {flags[0]['numerator']:,} | {flags[1]['numerator']:,} | {flags[2]['numerator']:,} |")
    reason_rows = []
    for reason, count in sorted(all_rows["contact_reason"].items(), key=lambda item: (-item[1], item[0])):
        historical = historical_co["contact_reason"].get(reason, 0)
        reason_rows.append(f"| {reason.replace('|', '/')} | {count:,} | {historical:,} |")
    product_rows = []
    for key, label in (("all_rows", "Todos los registros"), ("colombia_known_by_demo", "Colombia, disponibles en la demo")):
        cohort = cohorts[key]
        product_rows.append(f"| {label} | {cohort['rows_with_product_references']:,} | {cohort['savings_reference_same_customer_snapshot_rows']:,} | {cohort['savings_reference_same_customer_coherent_demo_dates_rows']:,} |")
    language_rows = "\n".join(f"| {language} | {count:,} | {transcript['known_by_demo_languages'].get(language, 0):,} |"
                              for language, count in sorted(transcript["languages"].items()))
    return f"""# Demanda de atención y cobertura de transcripciones

Revisión agregada del dataset sintético suministrado, {summary['analysis_date']}. Demo histórica: {summary['demo_at']}. Corte declarado: {summary['dataset_cutoff']}. Se leyeron {summary['interaction_files']:,} archivos de interacciones y {summary['transcript_files']:,} de transcripciones completos. Las fuentes permanecen intactas y conservan hashes en source_manifest.json.

## Demanda observada

| Cohorte | Interacciones | Marcadas resueltas | Marcadas escaladas | Requieren seguimiento |
|---|---:|---:|---:|---:|
{chr(10).join(rows)}

Los indicadores son etiquetas de la fuente. Una interacción marcada resuelta no demuestra que el problema haya sido efectivamente resuelto. Los numeradores pueden superponerse. summary.json conserva las distribuciones completas y los denominadores válidos de cada indicador; valores ausentes o inválidos se contabilizan por separado.

| Motivo de contacto de la fuente | Todo el dataset | Colombia disponible en la demo |
|---|---:|---:|
{chr(10).join(reason_rows)}

Las categorías y canales completos están en summary.json. Los grupos derivados usan coincidencias de palabras en contact_reason, son definiciones del equipo y pueden superponerse. Las categorías amplias no equivalen a consultas sobre una campaña específica o sobre una cuenta de ahorro.

mentioned_products contiene identificadores de producto separados por comas. Se contrastan con products.csv y con el cliente de la interacción, sin exportar identificadores.

| Cohorte | Filas con referencias de producto | Refieren ahorro del mismo cliente en snapshot | También fechas de producto coherentes en demo |
|---|---:|---:|---:|
{chr(10).join(product_rows)}

Las fechas coherentes requieren registro del cliente anterior o igual a apertura, y apertura anterior o igual a actualización, ambas anteriores o iguales a la demo. Esto conserva la limitación de snapshot y no reconstruye tenencia histórica real. Una referencia a ahorro no prueba que esa cuenta fuese el motivo principal de contacto.

En todos los registros hay {all_rows['product_reference_count']:,} referencias de producto: {all_rows['product_reference_missing_count']:,} no existen en products.csv y {all_rows['product_reference_customer_mismatch_count']:,} pertenecen a otro cliente. Las referencias verificadas de ahorro del mismo cliente suman {all_rows['savings_reference_same_customer_snapshot_rows']:,}. Este campo no permite atribuir de forma fiable la demanda observada a Cuenta de Ahorro; summary.json conserva los conteos por cohorte.

## Qué respalda el alcance

La distribución de motivos respalda un flujo de consultas bancarias con contexto y derivación confirmada. Cuenta de Ahorro es el alcance elegido por el usuario y el equipo. Su viabilidad de demo utiliza las relaciones coherentes entre cliente y cuenta de las fuentes operacionales, no las referencias defectuosas de mentioned_products. Las categorías amplias no prueban demanda específica de ahorro. La selección de campañas aporta el contexto de una oferta dentro de ese flujo. Estos registros no prueban publicidad no deseada, campañas enviadas incorrectamente ni reclamos causados por una campaña. Tampoco vinculan una mejora de atención o comercial con el prototipo.

Se encontraron {summary['interaction_quality'].get('processed_before_interaction_day', 0):,} interacciones procesadas antes del día de interacción y {summary['interaction_quality'].get('interaction_after_dataset_cutoff', 0):,} posteriores al corte declarado. Los casos conocidos antes de la demo excluyen esas incoherencias, fechas inválidas y referencias de cliente ausentes. La disponibilidad se supone por día de procesamiento. Colombia utiliza el país de la instantánea del cliente, sin historial de cambios. El volumen de todo el dataset se usa como evidencia descriptiva y no como una característica histórica del modelo o un resultado de la demo.

## Transcripciones y límites de idioma

| Idioma detectado de la fuente | Todas las transcripciones | Disponibles en la demo |
|---|---:|---:|
{language_rows}

Hay {transcript['rows']:,} transcripciones, con {transcript['nonempty_full_text_rows']:,} textos completos no vacíos y {transcript['exact_distinct_full_texts']:,} textos exactos distintos. {transcript['repeated_full_text_rows']:,} filas repiten un texto ya presente. La normalización de superficie encuentra {transcript['normalized_distinct_templates']:,} plantillas distintas; reemplaza identificadores, números y marcadores sin interpretar equivalencia semántica.

{transcript['rows_with_placeholder_markers']:,} textos contienen marcadores de plantilla y {transcript['rows_with_balance_keyword']:,} mencionan saldo/balance. La repetición y la distribución de idioma limitan el uso de estos textos como etiquetas variadas para un asistente bilingüe. El entrenamiento y la evaluación de intención utilizan casos del equipo claramente identificados y separados de esta evidencia descriptiva.

Hay {transcript['quality'].get('transcript_processed_before_interaction_day', 0):,} transcripciones procesadas antes del día de interacción. summary.json detalla las demás incidencias de fechas y referencias. La disponibilidad de una transcripción requiere una interacción conocida y fechas coherentes de transcripción, además de coincidencia de cliente y agente. No se exportan conversaciones, identificadores individuales ni firmas de los textos.

## Reproducir y comprobar

Ejecutar `python scripts/problem_evidence.py`. El proceso comprueba el inventario de archivos, vuelve a verificar hashes y concilia cada distribución con sus filas. Los únicos entregables son agregados y un manifiesto de fuentes. Las incidencias son conteos superpuestos y no se suman como registros distintos.
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--out", type=Path, default=ROOT / "outputs/problem_evidence")
    parser.add_argument("--config", type=Path, default=ROOT / "config/project.json")
    parser.add_argument("--analysis-date", default="2026-10-04")
    options = parser.parse_args()
    summary = build(options.root, options.out, json.loads(options.config.read_text(encoding="utf-8")), options.analysis_date)
    print(json.dumps({"all_interactions": summary["interaction_cohorts"]["all_rows"]["n"],
                      "colombia_known_by_demo": summary["interaction_cohorts"]["colombia_known_by_demo"]["n"],
                      "transcripts": summary["transcripts"]["rows"],
                      "distinct_full_texts": summary["transcripts"]["exact_distinct_full_texts"],
                      "distinct_templates": summary["transcripts"]["normalized_distinct_templates"],
                      "languages": summary["transcripts"]["languages"],
                      "verification": summary["verification"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
