"""Start the local banking service after phase 2 data and model preparation."""
from pathlib import Path
import argparse
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main():
    from campaigns.intents import IntentModel
    from campaigns.service import ChatService, IntentRouter
    from campaigns.server import bootstrap_demo_users, make_server
    from campaigns.store import DataStore
    parser = argparse.ArgumentParser(description="Campañas y atención de ahorro")
    parser.add_argument("--port", type=int, default=8002)
    parser.add_argument("--database", type=Path, default=ROOT / "outputs/phase2/prepared.sqlite")
    parser.add_argument("--model", type=Path, default=ROOT / "outputs/models/intent.json")
    parser.add_argument("--state", type=Path, default=ROOT / "outputs/app/state.sqlite")
    parser.add_argument("--credentials", type=Path, default=ROOT / "outputs/app/access_credentials.json")
    parser.add_argument("--scenarios", type=Path, default=ROOT / "outputs/scenarios/catalog.json")
    parser.add_argument("--router", choices=("baseline", "learned", "hybrid"), default="hybrid")
    args = parser.parse_args()
    store = DataStore(args.database)
    service = ChatService(store, IntentRouter(IntentModel.load(args.model), args.router), args.state)
    audience = store.audience(limit=3)
    rows = audience if isinstance(audience, list) else audience.get("rows", audience.get("audience", []))
    customer_ids = list(dict.fromkeys(row["customer_id"] for row in rows))
    if not args.scenarios.exists():
        raise ValueError("Construye los escenarios con scripts/build_scenarios.py")
    catalog = json.loads(args.scenarios.read_text(encoding="utf-8"))
    legacy = ROOT / "outputs/app/demo_credentials.json" if args.credentials == ROOT / "outputs/app/access_credentials.json" else None
    credential_path = bootstrap_demo_users(service, args.credentials, customer_ids, catalog, legacy)
    server = make_server(service, ROOT / "web", port=args.port, scenarios_path=args.scenarios)
    print(f"Aplicación: http://127.0.0.1:{server.server_port}")
    print(f"Credenciales locales: {credential_path.resolve()}")
    print("El archivo de credenciales es privado local; no lo compartas ni publiques.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
