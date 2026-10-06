"""Initialize read-only Blob data and the SQLite cache, then serve the cloud demo."""

import json
import os
from pathlib import Path
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from campaigns.secrets import SECRET_NAMES, read_keyvault_secret, read_keyvault_secrets
from campaigns.blob_data import download_source_data, prepare_application_cache
from campaigns.access import build_access_users


class InitializationHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        return

    def do_GET(self):
        health = urlsplit(self.path).path == "/healthz"
        body = json.dumps(dict(status="initializing") if health else dict(error="Application data is initializing; retry shortly")).encode()
        self.send_response(200 if health else 503)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Retry-After", "30")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_POST = do_GET


def main():
    origin = os.environ.get("APP_PUBLIC_ORIGIN")
    if not origin:
        raise ValueError("APP_PUBLIC_ORIGIN is required for container deployment.")
    # Keep probes responsive while the full source snapshot is prepared.
    initialization = ThreadingHTTPServer(("0.0.0.0", 8002), InitializationHandler)
    initialization.daemon_threads = True
    thread = threading.Thread(target=initialization.serve_forever, daemon=True)
    thread.start()
    try:
        application_secrets = read_keyvault_secrets(SECRET_NAMES)
        print("Managed identity Key Vault retrieval verified (3 application secrets).", flush=True)
        credentials = json.loads(read_keyvault_secret(os.environ.get("APP_ACCESS_CREDENTIALS_SECRET", "app-access-credentials")))
        source = download_source_data(application_secrets["storage_connection_string"],
                                      os.environ.get("APP_DATA_CONTAINER", "data"),
                                      os.environ.get("APP_DATA_PREFIX", ""), ROOT,
                                      account=os.environ.get("APP_STORAGE_ACCOUNT", "stlatambank"))
        print(f"Original Blob snapshot acquired: {source['blob_version']}.", flush=True)
        catalog = prepare_application_cache(ROOT)
        credentials = build_access_users(credentials, catalog)
        credentials_path = ROOT / "outputs/app/access_credentials.json"
        credentials_path.parent.mkdir(parents=True, exist_ok=True)
        with os.fdopen(os.open(credentials_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as handle:
            json.dump(credentials, handle)
    finally:
        initialization.shutdown()
        initialization.server_close()
        thread.join()
    print("Starting the customer service application.", flush=True)
    os.execv(sys.executable, [sys.executable, "-u", str(ROOT / "scripts/serve.py"),
                             "--host", "0.0.0.0", "--public-origin", origin,
                             "--config", str(ROOT / "config/intent.azure.yaml")])


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Initialization dependencies can include credentials in exception details.
        print("Container startup failed; inspect the last completed initialization stage.", file=sys.stderr)
        sys.exit(1)
