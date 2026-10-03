"""Reglas de demo. No representan elegibilidad financiera ni autenticación real."""

from datetime import datetime, timedelta
import unicodedata


POLICY_FIELDS = {
    "campaign": ("campaign_id", "description", "promoted_product", "target_country",
                 "target_segment", "start_date", "end_date", "campaign_status",
                 "campaign_objective"),
    "customer": ("customer_id", "country", "segment", "accepts_marketing",
                 "customer_status", "registration_date", "last_updated"),
}


def country_name(value):
    plain = "".join(c for c in unicodedata.normalize("NFD", value or "")
                    if unicodedata.category(c) != "Mn").casefold().strip()
    return {"colombia": "Colombia", "mexico": "México", "argentina": "Argentina"}.get(plain)


def timestamp(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
        return parsed if parsed.tzinfo is None else None
    except (ValueError, TypeError):
        return None


def boolean(value):
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int) and value in (0, 1):
        return value
    if isinstance(value, str) and value.strip().casefold() in ("true", "false"):
        return int(value.strip().casefold() == "true")
    return None


def campaign_reasons(campaign, config):
    reasons = []
    at = timestamp(config["demo_at"])
    start, end = timestamp(campaign.get("start_date")), timestamp(campaign.get("end_date"))
    if campaign.get("quality_flags"):
        reasons.append("campaign_data_invalid")
    if campaign.get("promoted_product") != config["product"]:
        reasons.append("product_out_of_scope")
    target = country_name(campaign.get("target_country"))
    if config["require_explicit_country"] and not target:
        reasons.append("target_country_missing_or_unknown")
    elif target and target != country_name(config["country"]):
        reasons.append("country_out_of_scope")
    if config["require_explicit_segment"] and not campaign.get("target_segment"):
        reasons.append("target_segment_missing")
    if config["require_description"] and not campaign.get("description"):
        reasons.append("description_missing")
    if start is None or end is None or start.date() > end.date():
        reasons.append("campaign_window_invalid")
    elif not start.date() <= at.date() <= end.date():
        reasons.append("outside_campaign_window")
    status = campaign.get("campaign_status")
    if status == "Paused":
        reasons.append("campaign_paused")
    elif status == "Completed":
        if not (config["mode"] == "historical_replay" and
                campaign.get("campaign_id") in config["historical_replay_campaign_ids"]):
            reasons.append("completed_campaign_not_authorized_for_replay")
    elif status != "Active":
        reasons.append("campaign_status_unknown")
    if campaign.get("campaign_objective") not in config["customer_statuses_by_objective"]:
        reasons.append("campaign_objective_unknown")
    return reasons


def customer_reasons(customer, campaign, config, counts):
    reasons = []
    at, cutoff = timestamp(config["demo_at"]), timestamp(config["dataset_cutoff"])
    if customer.get("quality_flags"):
        reasons.append("customer_data_invalid")
    if country_name(customer.get("country")) != country_name(config["country"]):
        reasons.append("customer_country_out_of_scope")
    target_country = country_name(campaign.get("target_country"))
    if target_country and country_name(customer.get("country")) != target_country:
        reasons.append("target_country_mismatch")
    if campaign.get("target_segment") and customer.get("segment") != campaign["target_segment"]:
        reasons.append("target_segment_mismatch")
    if config["require_marketing_consent"] and boolean(customer.get("accepts_marketing")) != 1:
        reasons.append("marketing_consent_not_true")
    allowed = config["customer_statuses_by_objective"].get(campaign.get("campaign_objective"), [])
    if customer.get("customer_status") not in allowed:
        reasons.append("customer_status_not_allowed_for_objective")
    registered, updated = timestamp(customer.get("registration_date")), timestamp(customer.get("last_updated"))
    if registered is None or registered > at:
        reasons.append("registration_not_known_by_demo")
    if updated is None or (registered and updated < registered):
        reasons.append("profile_timestamp_invalid")
    elif updated > cutoff:
        reasons.append("profile_after_dataset_cutoff")
    elif config["require_profile_not_after_demo"] and updated > at:
        reasons.append("profile_after_demo")
    for limit in config["frequency_limits"]:
        if counts.get(limit["days"], 0) >= limit["max_delivered_messages"]:
            reasons.append(f"frequency_limit_{limit['days']}d")
    return reasons


def known_contact(send, config):
    """Un contacto cuenta solo si era conocido, válido y entregado antes de la demo."""
    at = timestamp(config["demo_at"])
    sent, processed = timestamp(send.get("send_date")), timestamp(send.get("process_date"))
    return (not send.get("contact_quality_flags", send.get("quality_flags")) and boolean(send.get("was_delivered")) == 1
            and sent is not None and processed is not None and sent <= at and processed.date() <= at.date())


def permitted(principal, action, customer_id=None, assigned_customer_ids=(), confirmed=False):
    """principal procede de un contexto de prueba confiable, nunca de un ID aportado como autenticación."""
    if principal.get("authenticated") is not True or principal.get("expired") is not False:
        return False
    role = principal.get("role")
    if action in ("view_audience", "rebuild_data"):
        return role == "operator"
    if action == "view_public_campaign":
        return role in ("customer", "advisor", "operator")
    owns = role == "customer" and principal.get("customer_id") == customer_id and customer_id is not None
    assigned = role == "advisor" and customer_id in assigned_customer_ids
    if action == "view_customer_context":
        return owns or assigned
    if action == "request_advisor":
        return owns and confirmed is True
    return False


def contact_count(sends, config, days):
    lower = timestamp(config["demo_at"]) - timedelta(days=days)
    return sum(known_contact(s, config) and timestamp(s["send_date"]) >= lower for s in sends)
