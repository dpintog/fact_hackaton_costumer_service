"""Provider selection, root-relative paths and credential isolation."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from campaigns.clef import ClefModel
from campaigns.configuration import create_intent_router, load_intent_config
from campaigns.secrets import KeyVaultError


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = self.root / "intent.yaml"

    def write_config(self, provider="tfidf", **fields):
        self.config.write_text(yaml.safe_dump(dict(schema_version=1,
                                                  intent_classifier=dict(provider=provider, **fields))),
                               encoding="utf-8")

    def test_repository_default_preserves_local_hybrid(self):
        config = load_intent_config()
        self.assertEqual((config["provider"], config["router"]), ("tfidf", "hybrid"))

    def test_tfidf_loads_root_relative_artifact_without_reading_secrets(self):
        self.write_config(tfidf=dict(model_path="models/frozen.json"))
        with patch("campaigns.configuration.IntentModel.load") as load, \
                patch("campaigns.configuration.dotenv_values") as env:
            router = create_intent_router(self.config, root=self.root)
        load.assert_called_once_with(self.root / "models/frozen.json")
        env.assert_not_called()
        self.assertEqual(router.mode, "hybrid")

    def test_cli_overrides_model_and_mode(self):
        self.write_config(router="hybrid")
        model_path = self.root / "override.json"
        with patch("campaigns.configuration.IntentModel.load") as load:
            router = create_intent_router(self.config, root=self.root, model_path=model_path, mode="learned")
        load.assert_called_once_with(model_path)
        self.assertEqual(router.mode, "learned")

    def test_clef_loads_exact_dotenv_names_without_local_model(self):
        self.write_config("clef", router="learned", clef=dict(timeout_seconds=9))
        (self.root / ".env").write_text('clef_api_token="fake-token"\nclef_Account_ID=fake-account\n',
                                        encoding="utf-8")
        with patch.dict(os.environ, {}, clear=True), patch("campaigns.configuration.IntentModel.load") as load:
            router = create_intent_router(self.config, root=self.root)
        self.assertIsInstance(router.model, ClefModel)
        self.assertEqual(router.model.timeout_seconds, 9)
        load.assert_not_called()

    def test_environment_overrides_dotenv_credentials(self):
        self.write_config("clef")
        (self.root / ".env").write_text("clef_api_token=file-token\nclef_Account_ID=file-account\n", encoding="utf-8")
        with patch.dict(os.environ, {"clef_api_token": "env-token", "clef_Account_ID": "env-account"}), \
                patch("campaigns.configuration.ClefModel") as model:
            create_intent_router(self.config, root=self.root)
        self.assertEqual(model.call_args.args, ("env-token", "env-account"))

    def test_baseline_needs_no_credentials_or_artifact(self):
        self.write_config("clef")
        with patch("campaigns.configuration.dotenv_values") as env, \
                patch("campaigns.configuration.IntentModel.load") as load:
            router = create_intent_router(self.config, root=self.root, mode="baseline")
            self.assertEqual(router.predict("¿Cuál es mi saldo?")["model_provider"], "baseline")
        env.assert_not_called()
        load.assert_not_called()

    def test_clef_accepts_container_environment_without_a_dotenv_file(self):
        self.write_config("clef")
        with patch.dict(os.environ, {"clef_api_token": "injected-token", "clef_Account_ID": "injected-account"}), \
                patch("campaigns.configuration.dotenv_values") as dotenv, \
                patch("campaigns.configuration.ClefModel") as model:
            create_intent_router(self.config, root=self.root)
        dotenv.assert_not_called()
        self.assertEqual(model.call_args.args, ("injected-token", "injected-account"))

    def test_clef_uses_keyvault_in_cloud_without_reading_local_credentials(self):
        self.write_config("clef")
        (self.root / ".env").write_text("clef_api_token=local-token\nclef_Account_ID=local-account\n", encoding="utf-8")
        with patch.dict(os.environ, {"AZURE_KEY_VAULT_URL": "https://testvault.vault.azure.net/"}), \
                patch("campaigns.configuration.read_keyvault_secrets", return_value={
                    "clef_api_token": "vault-token", "clef_Account_ID": "vault-account"}) as vault, \
                patch("campaigns.configuration.dotenv_values") as dotenv, \
                patch("campaigns.configuration.ClefModel") as model:
            create_intent_router(self.config, root=self.root)
        vault.assert_called_once_with(("clef_api_token", "clef_Account_ID"))
        dotenv.assert_not_called()
        self.assertEqual(model.call_args.args, ("vault-token", "vault-account"))

    def test_keyvault_failure_does_not_fall_back_to_local_credentials(self):
        self.write_config("clef")
        with patch.dict(os.environ, {"AZURE_KEY_VAULT_URL": "https://testvault.vault.azure.net/"}), \
                patch("campaigns.configuration.read_keyvault_secrets", side_effect=KeyVaultError("Access failed")), \
                patch("campaigns.configuration.dotenv_values") as dotenv:
            with self.assertRaises(KeyVaultError):
                create_intent_router(self.config, root=self.root)
        dotenv.assert_not_called()

    def test_baseline_and_tfidf_do_not_contact_keyvault(self):
        with patch.dict(os.environ, {"AZURE_KEY_VAULT_URL": "https://testvault.vault.azure.net/"}), \
                patch("campaigns.configuration.read_keyvault_secrets") as vault, \
                patch("campaigns.configuration.IntentModel.load"):
            self.write_config("clef", router="baseline")
            create_intent_router(self.config, root=self.root)
            self.write_config("tfidf")
            create_intent_router(self.config, root=self.root)
        vault.assert_not_called()

    def test_missing_credentials_and_inapplicable_model_override_are_clear_errors(self):
        self.write_config("clef")
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "clef_api_token"):
                create_intent_router(self.config, root=self.root)
            (self.root / ".env").write_text("clef_api_token=fake-token\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "clef_Account_ID"):
                create_intent_router(self.config, root=self.root)
        with self.assertRaisesRegex(ValueError, "--model"):
            create_intent_router(self.config, root=self.root, model_path="ignored.json")

    def test_invalid_configurations_fail_before_loading_a_model(self):
        for fields in (dict(provider="clfe"), dict(router="bad"), dict(clef=dict(timeout_seconds=0)),
                       dict(clef=dict(timeout_seconds=True)), dict(clef=dict(timeout_seconds=float("nan"))),
                       dict(clef=dict(confidence_threshold=1.1)), dict(clef=dict(margin_threshold=-.1)),
                       dict(tfidf=dict(model_path="")), dict(tfidf=[]), dict(typo="ignored")):
            with self.subTest(fields=fields):
                provider = fields.pop("provider", "tfidf")
                self.write_config(provider, **fields)
                with self.assertRaises(ValueError):
                    load_intent_config(self.config)
        for content in ("[]", "schema_version: true\nintent_classifier: {}", "intent_classifier: [",
                        "!!python/object/apply:builtins.print ['unsafe']"):
            with self.subTest(content=content):
                self.config.write_text(content, encoding="utf-8")
                with self.assertRaises(ValueError):
                    load_intent_config(self.config)


if __name__ == "__main__":
    unittest.main()
