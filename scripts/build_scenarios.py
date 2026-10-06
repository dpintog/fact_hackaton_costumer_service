"""Build verifiable profiles from original customers in the snapshot."""

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from campaigns.scenarios import build_catalog


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepared", type=Path, default=ROOT / "outputs/phase2/prepared.sqlite")
    parser.add_argument("--out", type=Path, default=ROOT / "outputs/scenarios")
    args = parser.parse_args()
    catalog = build_catalog(args.prepared, args.out)
    print(json.dumps({"catalog": str(args.out / "catalog.json"), "analysis_at": catalog["analysis_at"],
                      "source_version": catalog["source_version"], "coverage": catalog["coverage"]},
                     ensure_ascii=False, indent=2))
