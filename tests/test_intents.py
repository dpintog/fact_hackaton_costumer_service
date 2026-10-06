"""Development checks for local intent data integrity and reproducible inference."""

from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from campaigns.intents import (INTENTS, IntentModel, baseline_predict, hybrid_predict, normalize,
                              read_examples)  # noqa: E402


class IntentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.path = Path(cls.temporary.name) / "model.json"
        cls.dataset = ROOT / "datasets/intent_training.jsonl"
        cls.model = IntentModel.train(cls.dataset, cls.path)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def test_bilingual_families_never_cross_splits(self):
        examples = read_examples(self.dataset)
        families = {}
        for row in examples:
            families.setdefault(row["family_id"], []).append(row)
        for rows in families.values():
            self.assertEqual({row["language"] for row in rows}, {"es", "pt"})
            self.assertEqual(len({row["split"] for row in rows}), 1)
        self.assertEqual(set(row["intent"] for row in examples), set(INTENTS))
        self.assertEqual(set(row["source"] for row in examples), {"team_authored"})

    def test_cross_split_family_and_duplicate_text_are_rejected(self):
        rows = read_examples(self.dataset)
        invalid = deepcopy(rows)
        invalid[0]["split"] = "development"
        path = Path(self.temporary.name) / "invalid.jsonl"
        path.write_text("\n".join(json.dumps(row) for row in invalid), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "family"):
            read_examples(path)
        invalid = deepcopy(rows)
        invalid[1]["text"] = invalid[0]["text"]
        path.write_text("\n".join(json.dumps(row) for row in invalid), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "duplicate"):
            read_examples(path)

    def test_training_and_load_are_byte_reproducible(self):
        second_path = Path(self.temporary.name) / "second_model.json"
        IntentModel.train(self.dataset, second_path)
        self.assertEqual(self.path.read_bytes(), second_path.read_bytes())
        loaded = IntentModel.load(self.path)
        for text in ("¿Cuál es mi saldo?", "Quero falar com um assessor.", "xzywvqrst"):
            self.assertEqual(self.model.predict(text), loaded.predict(text))

    def test_development_is_not_fitted_into_vocabulary(self):
        # A perturbation of every development text changes evaluation/manifest,
        # never vocabulary, IDF or fitted coefficients.
        rows = read_examples(self.dataset)
        for row in rows:
            if row["split"] == "development":
                row["text"] += " zzunseenfixturetoken " + row["id"]
        changed_data = Path(self.temporary.name) / "changed_dev.jsonl"
        changed_data.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
        changed_model = IntentModel.train(changed_data, Path(self.temporary.name) / "changed.json")
        self.assertNotIn("w:zzunseenfixturetoken", changed_model.vocabulary)
        for key in ("vocabulary", "idf", "weights", "bias"):
            self.assertEqual(self.model.artifact[key], changed_model.artifact[key])
        train_families = set(self.model.artifact["training"]["family_ids"])
        dev_families = set(self.model.artifact["development"]["family_ids"])
        self.assertFalse(train_families & dev_families)
        self.assertEqual(self.model.artifact["development"]["rows"], 36)

    def test_oov_and_empty_input_abstain(self):
        for text in ("", "   ", "zxqwv zqwxv", "💜💥"):
            for predictor in (self.model.predict, baseline_predict):
                result = predictor(text)
                self.assertEqual(result["intent"], "unknown")
                self.assertTrue(result["ambiguous"])
        with self.assertRaises(TypeError):
            self.model.predict(None)

    def test_explicit_queries_have_bilingual_contract(self):
        examples = (
            ("¿Cuál es el saldo de mi cuenta de ahorro?", "account_info"),
            ("Qual é o saldo da minha conta de poupança?", "account_info"),
            ("Quiero hablar con un asesor sobre mi ahorro.", "advisor_request"),
            ("Quero falar com um assessor sobre minha poupança.", "advisor_request"),
            ("No quiero recibir más publicidad del banco.", "marketing_optout"),
            ("Não quero receber mais publicidade do banco.", "marketing_optout"),
            ("¿Cuál es la tasa de interés de esta oferta?", "commercial_terms"),
            ("Qual é a taxa de juros dessa oferta?", "commercial_terms"),
        )
        for text, expected in examples:
            for predictor in (self.model.predict, baseline_predict):
                with self.subTest(text=text, predictor=predictor):
                    result = predictor(text)
                    self.assertEqual(result["intent"], expected)
                    self.assertFalse(result["ambiguous"])
                    self.assertGreaterEqual(result["confidence"], 0)
                    self.assertLessEqual(result["confidence"], 1)

    def test_multitopic_requires_clarification_and_optout_has_precedence(self):
        for predictor in (self.model.predict, baseline_predict):
            result = predictor("Necesito mi saldo y la campaña de ahorro.")
            self.assertTrue(result["ambiguous"])
        self.assertEqual(baseline_predict("No quiero recibir esta campaña de ahorro.")["intent"],
                         "marketing_optout")
        self.assertEqual(baseline_predict("Quiero recibir información de la campaña.")["intent"],
                         "campaign_info")
        self.assertEqual(baseline_predict("Tengo interés en la campaña.")["intent"],
                         "commercial_terms")  # deliberately documented lexical ambiguity
        self.assertNotEqual(baseline_predict("Ya no me interesan sus anuncios.")["intent"],
                            "commercial_terms")

    def test_invalid_model_dimensions_and_thresholds_are_rejected(self):
        artifact = deepcopy(self.model.artifact)
        artifact["weights"] = [[0.0]]
        with self.assertRaises(ValueError):
            IntentModel(artifact)
        artifact = deepcopy(self.model.artifact)
        artifact["weights"][0][0] = float("inf")
        with self.assertRaises(ValueError):
            IntentModel(artifact)
        artifact = deepcopy(self.model.artifact)
        artifact["thresholds"]["confidence"] = 0.0
        with self.assertRaisesRegex(ValueError, "threshold"):
            IntentModel(artifact)

    def test_hybrid_reports_rule_fallback_and_never_hides_disagreement(self):
        class Stub:
            def __init__(self, result):
                self.result = result
            def predict(self, _):
                return self.result

        unknown = Stub({"intent": "unknown", "confidence": 0.2, "ambiguous": True})
        result = hybrid_predict("¿Cuál es mi saldo?", unknown)
        self.assertEqual(result["intent"], "account_info")
        self.assertEqual(result["routing_source"], "baseline_fallback")
        wrong = Stub({"intent": "campaign_info", "confidence": 0.8, "ambiguous": False})
        result = hybrid_predict("¿Cuál es mi saldo?", wrong)
        self.assertTrue(result["ambiguous"])
        self.assertEqual(result["routing_source"], "disagreement_clarification")
        result = hybrid_predict("No quiero recibir publicidad.", wrong)
        self.assertEqual(result["intent"], "marketing_optout")
        self.assertEqual(result["routing_source"], "explicit_policy_route")

    def test_normalization_preserves_negation_and_bounds_input(self):
        self.assertEqual(normalize("NÃO, no, sí."), "nao no si")
        self.assertLessEqual(len(normalize("saldo " * 10000)), 2000)


if __name__ == "__main__":
    unittest.main()
