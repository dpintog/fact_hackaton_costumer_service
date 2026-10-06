"""Clef REST contract and conservative behavior on ambiguous/failed inference."""
from copy import deepcopy
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from campaigns.clef import ClefError, ClefModel, MODEL_ID
from campaigns.intents import INTENTS
from campaigns.service import IntentRouter


def response_document(intent="account_info", probability=.92):
    probabilities = {name: (1 - probability) / (len(INTENTS) - 1) for name in INTENTS}
    probabilities[intent] = probability
    return dict(success=True, result=dict(answers={
        "intent": dict(choice=intent, confidence=probability, probabilities=probabilities)}))


class ClefTests(unittest.TestCase):
    def setUp(self):
        self.model = ClefModel("fake-secret-token", "fake-account", timeout_seconds=7)

    def predict_with_response(self, document, text="Consulta de prueba"):
        with patch("campaigns.clef.urlopen", return_value=io.BytesIO(json.dumps(document).encode())):
            return self.model.predict(text)

    def test_request_uses_documented_schema_and_only_message_state(self):
        for message, intent in (("¿Cuál es mi saldo?", "account_info"),
                                ("Quero falar com um assessor.", "advisor_request")):
            with self.subTest(message=message), patch("campaigns.clef.urlopen", return_value=io.BytesIO(
                    json.dumps(response_document(intent)).encode())) as call:
                result = IntentRouter(self.model, "learned").predict(message)
                request = call.call_args.args[0]
                payload = json.loads(request.data)
                self.assertEqual(request.full_url, "https://api.cloudflare.com/client/v4/accounts/fake-account/ai/run/" + MODEL_ID)
                self.assertEqual(request.get_method(), "POST")
                self.assertEqual(request.get_header("Authorization"), "Bearer fake-secret-token")
                self.assertEqual(call.call_args.kwargs["timeout"], 7)
                self.assertEqual(payload["model"], "clef")
                self.assertEqual(payload["state"], message)
                self.assertEqual(set(payload), {"model", "state", "questions"})
                self.assertEqual(payload["questions"]["intent"]["type"], "choice")
                self.assertEqual(set(payload["questions"]["intent"]["criteria"]), set(INTENTS))
                self.assertNotIn("fake-secret-token", request.data.decode())
                self.assertEqual(result["intent"], intent)
                self.assertFalse(result["ambiguous"])
                self.assertEqual(result["model_provider"], "clef")
                self.assertEqual(result["model_version"], MODEL_ID)

    def test_unknown_low_confidence_and_small_margin_abstain(self):
        low = response_document(probability=.4)
        tie = response_document(probability=.5)
        tie["result"]["answers"]["intent"]["probabilities"] = dict.fromkeys(INTENTS, 0.0)
        tie["result"]["answers"]["intent"]["probabilities"].update(account_info=.5, campaign_info=.5)
        for document in (response_document("unknown"), low, tie):
            with self.subTest(document=document):
                prediction = self.predict_with_response(document)
                self.assertEqual(prediction["intent"], "unknown")
                self.assertTrue(prediction["ambiguous"])

    def test_configured_threshold_changes_acceptance(self):
        self.model.confidence_threshold = .35
        self.assertEqual(self.predict_with_response(response_document(probability=.4))["intent"], "account_info")

    def test_invalid_or_inconsistent_probabilities_raise_sanitized_error(self):
        original = response_document()
        documents = [dict(success=False, errors=[dict(message="fake-secret-token")]),
                     dict(success=True, result={}), [], None]
        for change in (dict(probabilities={"account_info": .9}), dict(choice="campaign_info"),
                       dict(probabilities=dict.fromkeys(INTENTS, .9)),
                       dict(probabilities={**original["result"]["answers"]["intent"]["probabilities"],
                                           "account_info": float("nan")}),
                       dict(probabilities={**original["result"]["answers"]["intent"]["probabilities"],
                                           "account_info": True})):
            changed = deepcopy(original)
            changed["result"]["answers"]["intent"].update(change)
            documents.append(changed)
        for document in documents:
            with self.subTest(document=document), self.assertRaises(ClefError) as error:
                self.predict_with_response(document)
            self.assertNotIn("fake-secret-token", str(error.exception))

    def test_timeout_http_error_and_malformed_json_do_not_retry_or_fallback(self):
        failures = (TimeoutError("fake-secret-token"), URLError("fake-secret-token"),
                    HTTPError("https://example.invalid", 429, "fake-secret-token", {}, io.BytesIO()))
        for failure in failures:
            with self.subTest(failure=type(failure)), patch("campaigns.clef.urlopen", side_effect=failure) as call:
                with self.assertRaises(ClefError) as error:
                    IntentRouter(self.model).predict("¿Cuál es mi saldo?")
                self.assertNotIn("fake-secret-token", str(error.exception))
                self.assertEqual(call.call_count, 1)
        with patch("campaigns.clef.urlopen", return_value=io.BytesIO(b"not JSON")):
            with self.assertRaises(ClefError):
                self.model.predict("hola")

    def test_empty_and_oversized_messages_never_call_api(self):
        with patch("campaigns.clef.urlopen") as call:
            for text in ("", "  ", "x" * 2001):
                self.assertTrue(self.model.predict(text)["ambiguous"])
            with self.assertRaises(TypeError):
                self.model.predict(None)
        call.assert_not_called()

    def test_hybrid_keeps_provider_when_rule_overrides_prediction(self):
        with patch("campaigns.clef.urlopen", return_value=io.BytesIO(json.dumps(response_document()).encode())):
            result = IntentRouter(self.model).predict("No quiero recibir más publicidad.")
        self.assertEqual(result["intent"], "marketing_optout")
        self.assertEqual(result["model_provider"], "clef")
        self.assertEqual(result["routing_source"], "explicit_policy_route")


if __name__ == "__main__":
    unittest.main()
