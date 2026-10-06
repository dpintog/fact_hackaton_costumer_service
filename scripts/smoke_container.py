"""Exercise the deployed app without printing passwords, tokens, or customer data."""

import argparse
import csv
import io
import json
from pathlib import Path
import sys
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--credentials", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=Path("outputs/deployment/smoke-results.json"))
    parser.add_argument("--confirm-batch", action="store_true",
                        help="Also confirm and export one local campaign list; no external delivery occurs.")
    args = parser.parse_args()
    base = args.url.rstrip("/")
    document = json.loads(args.credentials.read_text(encoding="utf-8"))
    users = document["users"]
    checks = []

    def request(path, body=None, token=None, *, expected=200, origin=None, html=False):
        headers = {}
        if body is not None:
            headers["Content-Type"] = "application/json"
            headers["Origin"] = origin or base
        if token:
            headers["Authorization"] = "Bearer " + token
        req = Request(base + path, json.dumps(body).encode() if body is not None else None, headers)
        try:
            response = urlopen(req, timeout=90)
        except HTTPError as error:
            response = error
        with response:
            status, raw = response.status, response.read()
        if status != expected:
            raise RuntimeError(f"{path} returned HTTP {status}; expected {expected}.")
        return raw if html else json.loads(raw)

    def passed(name):
        checks.append(dict(check=name, status="passed"))
        print(f"Passed: {name}")
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(dict(url=base, checks=checks), indent=2) + "\n", encoding="utf-8")

    if request("/healthz") != {"status": "ok"}:
        raise RuntimeError("Unexpected health response.")
    passed("container health")
    request("/", html=True)
    passed("web interface")
    if request("/api/info")["country"] != "Colombia":
        raise RuntimeError("Unexpected application configuration.")
    passed("application metadata")
    request("/api/audience", expected=403)
    passed("unauthenticated private data denied")
    request("/api/login", {}, expected=403, origin="https://untrusted.example")
    passed("cross-origin request denied")
    request("/.env", expected=404)
    request("/outputs/app/access_credentials.json", expected=404)
    passed("secret files unavailable over HTTP")

    def login(user):
        return request("/api/login", dict(username=user["username"], password=user["password"]))["token"]

    operator = next(user for user in users if user["role"] == "operator")
    operator_token = login(operator)
    passed("operator login")
    campaigns = request("/api/campaigns", token=operator_token)["campaigns"]
    if not campaigns:
        raise RuntimeError("The original dataset returned no campaigns.")
    selection = request("/api/selection", token=operator_token)
    if selection["total"] < 1:
        raise RuntimeError("The original dataset returned no eligible audience.")
    passed("original-data campaign selection")
    if not request("/api/scenarios", token=operator_token)["profiles"]:
        raise RuntimeError("No prepared scenarios are available.")
    passed("prepared scenario catalog")
    if args.confirm_batch:
        preview = request("/api/batches/preview", dict(campaign_id=selection["campaign_id"]), operator_token)
        if preview.get("status") != "confirmation_pending":
            raise RuntimeError("The campaign list preview did not request confirmation.")
        confirmation = dict(batch_action_id=preview["batch_action_id"],
                            idempotency_key=preview["idempotency_key"], confirmed=True)
        receipt = request("/api/batches/confirm", confirmation, operator_token)
        replay = request("/api/batches/confirm", confirmation, operator_token)
        if (receipt.get("status") != "prepared" or receipt.get("count") != preview["audience_count"]
                or receipt.get("external_deliveries") != 0
                or replay.get("batch_id") != receipt["batch_id"] or not replay.get("idempotent_replay")):
            raise RuntimeError("The confirmed campaign list or idempotent replay failed verification.")
        passed("confirmed local campaign list and idempotent replay")
        exported = request(f"/api/batches/{receipt['batch_id']}/export", token=operator_token, html=True)
        members = list(csv.DictReader(io.StringIO(exported.decode("utf-8"))))
        if len(members) != receipt["count"] or len({row["customer_id"] for row in members}) != receipt["count"]:
            raise RuntimeError("The exported list did not match the confirmed audience.")
        passed("complete authenticated campaign list export")
    customer = next((user for user in users if user["role"] == "customer"), None)
    if customer is None:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
        from campaigns.access import scenario_password
        customer = dict(username="escenario01", password=scenario_password(document["scenario_password_seed"], "escenario01"))
    customer_token = login(customer)
    request("/api/profile", token=customer_token)
    request("/api/selection", token=customer_token, expected=403)
    passed("customer login and role permissions")
    for language, message in (("es", "Quiero consultar mis cuentas de ahorro"),
                              ("pt", "Quero consultar minhas contas de poupança")):
        answer = request("/api/chat", dict(message=message, language=language), customer_token)
        if answer.get("status") != "resolution" or answer.get("prediction", {}).get("model_provider") != "clef":
            raise RuntimeError(f"The {language} chat did not resolve with Clef.")
        passed(f"{language} chat through Clef with vault credentials")
    request("/api/logout", {}, customer_token)
    request("/api/logout", {}, operator_token)
    passed("logout")
    print(f"All {len(checks)} deployment checks passed; report: {args.out.resolve()}")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, ValueError, KeyError) as error:
        # HTTP failures report only status and route, never request/response contents.
        print(f"Deployment check failed: {error}", file=sys.stderr)
        sys.exit(1)
