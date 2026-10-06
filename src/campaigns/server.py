"""HTTP adapter with explicit origin checks and no credential or fixture APIs."""
from __future__ import annotations

import json
import csv
import io
import secrets
import socket
import sqlite3
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

from .operations import CampaignOperations


class LocalHTTPServer(ThreadingHTTPServer):
    # Windows SO_REUSEADDR can otherwise admit two listeners on one endpoint.
    allow_reuse_address = not hasattr(socket, "SO_EXCLUSIVEADDRUSE")

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def bootstrap_demo_users(service, credentials_path, customer_ids, scenario_catalog=None, legacy_path=None):
    path = Path(credentials_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        document = json.loads(path.read_text(encoding="utf-8"))
    elif legacy_path and Path(legacy_path).exists():
        document = json.loads(Path(legacy_path).read_text(encoding="utf-8"))
    else:
        if not customer_ids:
            raise ValueError("Prepara una audiencia válida antes de iniciar")
        users = [dict(username=f"cliente{index + 1}", password=secrets.token_urlsafe(18),
                      customer_id=customer_id, role="customer") for index, customer_id in enumerate(customer_ids[:3])]
        users.append(dict(username="operador", password=secrets.token_urlsafe(18), customer_id=None, role="operator"))
        document = dict(scope="local_dataset_access", users=users)
    if scenario_catalog:
        store = service.store
        if scenario_catalog.get("source_version") != store.source_version or scenario_catalog.get("analysis_at") != store.config["demo_at"] or scenario_catalog.get("policy_version") != store.config["policy_version"]:
            raise ValueError("Reconstruye el catálogo de escenarios para esta versión de datos")
        by_username = {user["username"]: user for user in document["users"]}
        for profile in scenario_catalog["profiles"]:
            previous = by_username.get(profile["username"])
            if previous and (previous.get("customer_id") != profile["customer_id"] or previous.get("role") != "customer"):
                raise ValueError("El usuario de escenario tiene otra identidad; utiliza un archivo de acceso y estado nuevos")
            if not previous:
                document["users"].append(dict(username=profile["username"], password=secrets.token_urlsafe(18), customer_id=profile["customer_id"], role="customer", scenario=profile["scenario"]))
    document["scope"] = "local_dataset_access"
    for user in document["users"]:
        if user["role"] == "customer" and service.store.get_customer(user["customer_id"]) is None:
            raise ValueError("La identidad local no existe en los datos preparados")
        service.register_demo_user(user["username"], user["password"], user.get("customer_id"), user["role"])
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def make_server(service, web_root, host="127.0.0.1", port=8002, scenarios_path=None, public_origin=None):
    if host not in ("127.0.0.1", "0.0.0.0"):
        raise ValueError("Utiliza 127.0.0.1 o 0.0.0.0 como host")
    if public_origin is not None:
        parsed_origin = urlsplit(public_origin)
        if (parsed_origin.scheme != "https" or not parsed_origin.hostname
                or parsed_origin.username or parsed_origin.password
                or parsed_origin.path not in ("", "/") or parsed_origin.query or parsed_origin.fragment):
            raise ValueError("El origen público debe ser una URL HTTPS sin ruta ni credenciales")
        public_origin = "https://" + parsed_origin.netloc
    if host == "0.0.0.0" and public_origin is None:
        raise ValueError("Configura un origen público HTTPS para escuchar fuera de loopback")
    root = Path(web_root).resolve()
    attempts = {}
    operations = CampaignOperations(service, scenarios_path)

    class Handler(BaseHTTPRequestHandler):
        server_version = "Ahorro/2.0"

        def log_message(self, format, *args):
            # Never log credentials, messages, tokens or query strings.
            return

        def _headers(self, status, mime="application/json; charset=utf-8", filename=None):
            self.send_response(status)
            self.send_header("Content-Type", mime)
            if filename:
                self.send_header("Content-Disposition", 'attachment; filename="' + filename + '"')
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.end_headers()

        def _json(self, value, status=200):
            self._headers(status)
            self.wfile.write(json.dumps(value, ensure_ascii=False).encode("utf-8"))

        def _origin_allowed(self):
            expected = urlsplit(public_origin).netloc if public_origin else f"127.0.0.1:{self.server.server_port}"
            if self.headers.get("Host") != expected:
                return False
            origin = self.headers.get("Origin")
            return origin is None or origin == (public_origin or "http://" + expected)

        def _token(self):
            authorization = self.headers.get("Authorization", "")
            return authorization[7:] if authorization.startswith("Bearer ") else None

        def do_GET(self):
            try:
                self._get()
            except (RuntimeError, ValueError, OSError, sqlite3.Error):
                self._json(dict(error="Service unavailable; no operation is confirmed"), 503)

        def _get(self):
            parsed = urlsplit(self.path)
            if parsed.path == "/healthz":
                return self._json(dict(status="ok"))
            if not self._origin_allowed():
                return self._json(dict(error="Forbidden origin"), 403)
            path = parsed.path
            if path == "/":
                self._headers(200, "text/html; charset=utf-8")
                self.wfile.write((root / "index.html").read_bytes())
            elif path == "/api/info":
                self._json(dict(product="Cuenta de Ahorro", country="Colombia", analysis_at=service.store.config.get("demo_at"),
                                available_languages=["es", "pt"], languages=["es", "pt"], campaign_delivery="local_preparation_only"))
            elif path in ("/api/campaigns", "/api/profile", "/api/scenarios"):
                fn = {"/api/campaigns": operations.campaigns, "/api/profile": service.get_profile, "/api/scenarios": operations.scenarios}[path]
                value = fn(self._token())
                self._json(value if value is not None else dict(error="Access denied"), 200 if value is not None else 403)
            elif path == "/api/selection":
                query = parse_qs(parsed.query, keep_blank_values=True)
                if set(query) - {"campaign_id", "decision", "reason", "offset", "limit"} or any(len(v) != 1 for v in query.values()):
                    return self._json(dict(error="Invalid filters"), 400)
                filters = {k: v[0] for k, v in query.items() if v[0] != ""}
                try:
                    value = operations.selection(self._token(), **filters)
                except ValueError:
                    return self._json(dict(error="Invalid filters"), 400)
                self._json(value if value is not None else dict(error="Access denied"), 200 if value is not None else 403)
            elif path.startswith("/api/batches/"):
                parts = path.removeprefix("/api/batches/").split("/")
                export = len(parts) == 2 and parts[1] == "export"
                if len(parts) > 1 and not export:
                    return self._json(dict(error="Not found"), 404)
                value = operations.get_batch(self._token(), parts[0], include_members=export)
                if value is None:
                    return self._json(dict(error="Batch unavailable"), 403)
                if export:
                    if value["status"] != "prepared":
                        return self._json(dict(error="La audiencia cambió; prepara una lista nueva"), 409)
                    stream = io.StringIO(newline="")
                    writer = csv.DictWriter(stream, fieldnames=("campaign_id", "customer_id"))
                    writer.writeheader()
                    writer.writerows(value["rows"])
                    self._headers(200, "text/csv; charset=utf-8", value["batch_id"] + ".csv")
                    self.wfile.write(stream.getvalue().encode("utf-8"))
                else:
                    self._json(value)
            elif path.startswith("/api/requests/"):
                request_id = path.removeprefix("/api/requests/")
                request = service.get_request(self._token(), request_id)
                self._json(request if request else dict(error="Request unavailable"), 200 if request else 403)
            elif path == "/api/audience":
                audience = service.get_audience(self._token())
                self._json(dict(rows=audience) if audience is not None else dict(error="Access denied"), 200 if audience is not None else 403)
            else:
                self._json(dict(error="Not found"), 404)

        def do_POST(self):
            if not self._origin_allowed():
                return self._json(dict(error="Forbidden origin"), 403)
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                return self._json(dict(error="JSON required"), 415)
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 1 or length > 12000:
                    return self._json(dict(error="Invalid body size"), 413)
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError("Object required")
            except (ValueError, UnicodeDecodeError):
                return self._json(dict(error="Invalid JSON"), 400)
            path = urlsplit(self.path).path
            try:
                if path == "/api/login":
                    if set(body) - {"username", "password"}:
                        return self._json(dict(error="Unknown fields"), 400)
                    now = time.monotonic()
                    recent = [at for at in attempts.get(self.client_address[0], []) if now - at < 60]
                    attempts[self.client_address[0]] = recent
                    if len(recent) >= 8:
                        return self._json(dict(error="Try again later"), 429)
                    session = service.login(body.get("username"), body.get("password"))
                    if session is None:
                        recent.append(now)
                    return self._json(session if session else dict(error="Invalid credentials"), 200 if session else 401)
                if path == "/api/logout":
                    service.logout(self._token())
                    return self._json(dict(logged_out=True))
                if path == "/api/chat":
                    allowed = {"message", "conversation_id", "language", "confirmed", "idempotency_key"}
                    if set(body) - allowed or ("confirmed" in body and not isinstance(body["confirmed"], bool)):
                        return self._json(dict(error="Unknown fields or invalid confirmation"), 400)
                    result = service.chat(self._token(), body.get("message"), body.get("conversation_id"),
                                          body.get("language", "es"), confirmed=body.get("confirmed", False),
                                          idempotency_key=body.get("idempotency_key"))
                    return self._json(result, 403 if result["status"] == "denied" else 200)
                if path == "/api/batches/preview":
                    if set(body) != {"campaign_id"} or not isinstance(body["campaign_id"], str):
                        return self._json(dict(error="Campaign required"), 400)
                    try:
                        result = operations.preview(self._token(), body["campaign_id"])
                    except ValueError:
                        return self._json(dict(error="Campaign unavailable"), 400)
                    return self._json(result, 403 if result["status"] == "denied" else 200)
                if path == "/api/batches/confirm":
                    if set(body) != {"batch_action_id", "idempotency_key", "confirmed"} or not isinstance(body["confirmed"], bool):
                        return self._json(dict(error="Invalid confirmation"), 400)
                    result = operations.confirm(self._token(), **body)
                    status = result["status"]
                    code = 403 if status == "denied" else 409 if status in {"expired", "changed", "needs_refresh"} else 503 if status == "tool_error" else 200
                    return self._json(result, code)
                self._json(dict(error="Not found"), 404)
            except (RuntimeError, ValueError, OSError, sqlite3.Error):
                self._json(dict(error="Service unavailable; no operation is confirmed"), 503)

    server = LocalHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server
