"""Train the authored local intent component and show its development comparison."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

# Tiny dense matrices are faster and predictable with a bounded BLAS thread count.
# Respect an explicit user environment choice rather than replacing it.
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from campaigns.intents import IntentModel  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=ROOT / "datasets/intent_training.jsonl")
    parser.add_argument("--model", type=Path, default=ROOT / "outputs/models/intent.json")
    options = parser.parse_args()
    model = IntentModel.train(options.data, options.model)
    comparisons = {variant: {key: value for key, value in metrics.items()
                             if key in ("n", "correct", "accuracy", "macro_f1", "by_language",
                                        "classification_coverage", "in_scope_classification_coverage",
                                        "unknown_false_accepts")}
                   for variant, metrics in model.artifact["development"].items()
                   if variant in ("learned", "baseline", "hybrid")}
    print(json.dumps({"model": str(options.model), "model_version": model.artifact["model_version"],
                      "sha256": hashlib.sha256(options.model.read_bytes()).hexdigest(),
                      "training_rows": model.artifact["training"]["rows"],
                      "training_families": len(model.artifact["training"]["family_ids"]),
                      "vocabulary_size": len(model.vocabulary),
                      "data_sha256": model.artifact["training"]["data_sha256"],
                      "development": comparisons}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
