"""Compare baseline, learned-only and hybrid on the same frozen local cases."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import platform
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from campaigns.evaluation import (BaselineModel, file_hash, HybridModel, load_cases,
                                  run_cases, ServiceAdapter, validate_split_isolation,
                                  write_report)
from campaigns.intents import BASELINE_VERSION, IntentModel


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, default=ROOT / "outputs/models/intent.json")
    parser.add_argument("--cases", type=Path, default=ROOT / "datasets/evaluation_cases.jsonl")
    parser.add_argument("--manifest", type=Path, default=ROOT / "datasets/evaluation_manifest.json")
    parser.add_argument("--training", type=Path, default=ROOT / "datasets/intent_training.jsonl")
    parser.add_argument("--out", type=Path, default=ROOT / "outputs/evaluation")
    parser.add_argument("--repeat", type=int, default=2)
    parser.add_argument("--regression-run", action="store_true",
                        help="Explicitly label runs after workflow defects were repaired against the reserve")
    args = parser.parse_args()
    if not 1 <= args.repeat <= 10:
        parser.error("repeat must be between 1 and 10")
    cases = load_cases(args.cases, args.manifest)
    isolation = validate_split_isolation(cases, args.training)
    model = IntentModel.load(args.model)
    if model.artifact.get("training", {}).get("data_sha256") != file_hash(args.training):
        raise ValueError("Model training-data hash differs from the supplied train/development corpus")
    modes = {"baseline": BaselineModel(), "learned": model, "hybrid": HybridModel(model)}
    rows = []
    for mode, predictor in modes.items():
        print(f"Evaluating {mode}: {len(cases)} cases x {args.repeat} repeat(s)", flush=True)
        rows.extend(run_cases(cases, ServiceAdapter, predictor, mode, args.repeat))
    code_files = ("src/campaigns/evaluation.py", "src/campaigns/service.py", "src/campaigns/intents.py", "scripts/evaluate.py",
                  "src/campaigns/policy.py", "src/campaigns/store.py", "src/campaigns/server.py", "src/campaigns/operations.py",
                  "src/campaigns/scenarios.py", "scripts/serve.py", "web/index.html", "config/project.json")
    metadata = dict(dataset_sha256=file_hash(args.cases), model_sha256=file_hash(args.model),
                    training_sha256=file_hash(args.training), repetitions=args.repeat,
                    fixture_version="evaluation-fixture-v1", fixture_config_sha256=file_hash(ROOT / "config/day1.json"),
                    fixture_data_classification="team_generated_synthetic; independent of organizer customer records",
                    labels="independently team authored; no bank-domain review", split_isolation=isolation,
                    model_version=model.artifact.get("model_version"), baseline_version=BASELINE_VERSION,
                    thresholds=model.artifact.get("thresholds", {}),
                    prompt_version="deterministic response templates in service source hash",
                    code_sha256={name: file_hash(ROOT / name) for name in code_files},
                    python=platform.python_version(), platform=platform.platform(),
                    executed_at_utc=datetime.now(timezone.utc).isoformat(),
                    cost_assumptions="No network/model API calls; only local compute, unpriced.",
                    evaluation_use=("Regression after reserved workflow cases were exposed; classifier frozen before first evaluation."
                                    if args.regression_run else "First frozen-component comparison; later workflow repair reruns are regression evidence. Never tune learned model on heldout."))
    report = write_report(args.out, cases, rows, metadata)
    for mode, metrics in report["modes"].items():
        print(json.dumps(dict(mode=mode, n=metrics["n"], passed=metrics["passed"], unsafe=metrics["unsafe"],
                              classification=metrics["classification"]["accuracy"]), ensure_ascii=False))
    print(f"Report: {args.out / 'report.md'}")


if __name__ == "__main__":
    main()
