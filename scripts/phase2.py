"""Preparar, seleccionar y verificar el alcance de la fase 2."""

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from campaigns.phase2 import prepare, verify
from campaigns.prepare import load_config

if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["build", "verify"])
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--out", type=Path, default=ROOT / "outputs/phase2")
    parser.add_argument("--config", type=Path, default=ROOT / "config/project.json")
    args = parser.parse_args()
    result = prepare(args.root, args.out, load_config(args.config)) if args.command == "build" else verify(args.out, args.root)
    print(json.dumps(result, ensure_ascii=False, indent=2))
