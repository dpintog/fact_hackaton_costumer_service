"""Secret handling and deployment wiring, using synthetic values and no Azure calls."""

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.sync_keyvault import AzureCli, AzureError, configure_app, load_secrets, upload_secrets
from campaigns.secrets import KeyVaultError, SECRET_NAMES, read_keyvault_secrets


TEMPLATE = ROOT / "infra/azure/containerapp.json"


class KeyVaultTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.env_file = Path(self.temporary.name) / ".env"
        self.env_file.write_text('clef_api_token="fake;$token=é"\n'
                                 'clef_Account_ID=fake-account\n'
                                 'storage_connection_string="Endpoint=x;Key=fake==;"\n',
                                 encoding="utf-8-sig")

    def test_upload_preserves_values_without_putting_them_in_arguments_or_output(self):
        mappings, values = load_secrets(self.env_file)
        calls, paths, stored = [], [], {}

        class FakeCli:
            def run(self, *arguments, json_output=False):
                calls.append(arguments)
                name = arguments[arguments.index("--name") + 1]
                if arguments[2] == "set":
                    path = Path(arguments[arguments.index("--file") + 1])
                    paths.append(path)
                    stored[name] = path.read_text(encoding="utf-8")
                else:
                    return stored[name]

        output = io.StringIO()
        with redirect_stdout(output):
            upload_secrets(FakeCli(), "testvault", mappings, values)
        self.assertEqual(set(stored), {mapping["secretName"] for mapping in mappings})
        for mapping in mappings:
            self.assertEqual(stored[mapping["secretName"]], values[mapping["environmentVariable"]])
        for value in values.values():
            self.assertNotIn(value, output.getvalue())
            self.assertFalse(any(value in argument for call in calls for argument in call))
        self.assertTrue(all(not path.exists() for path in paths))

    def test_invalid_dotenv_fails_before_any_upload(self):
        for content, expected in (("clef_api_token=\n", "Missing or empty"),
                                  ("unmapped_secret=fake\n", "Add these .env names")):
            with self.subTest(content=content):
                self.env_file.write_text(content, encoding="utf-8")
                with self.assertRaisesRegex(ValueError, expected):
                    load_secrets(self.env_file)

    def test_upload_verification_failure_does_not_reveal_either_value(self):
        mappings, values = load_secrets(self.env_file)
        class FakeCli:
            def run(self, *arguments, json_output=False):
                return "wrong-private-value" if json_output else None
        with self.assertRaises(AzureError) as error:
            upload_secrets(FakeCli(), "testvault", mappings, values)
        self.assertNotIn("wrong-private-value", str(error.exception))
        self.assertNotIn(values["clef_api_token"], str(error.exception))

    def test_cli_targets_subscription_and_redacts_upstream_errors(self):
        result = subprocess.CompletedProcess([], 1, stdout="private-output",
                                              stderr="ERROR: (Forbidden) private-token")
        with patch("scripts.sync_keyvault.shutil.which", return_value="az"), \
                patch("scripts.sync_keyvault.subprocess.run", return_value=result) as run:
            cli = AzureCli("a6dbfedb-8913-4e08-b801-c60fcad0a795", self.temporary.name)
            with self.assertRaises(AzureError) as error:
                cli.run("keyvault", "secret", "set")
        self.assertEqual(error.exception.code, "Forbidden")
        self.assertNotIn("private", str(error.exception))
        command = run.call_args.args[0]
        self.assertEqual(command[command.index("--subscription") + 1], cli.subscription)
        self.assertEqual(command[-2:], ["--output", "none"])
        self.assertEqual(run.call_args.kwargs["env"]["AZURE_CONFIG_DIR"], str(Path(self.temporary.name).resolve()))

    def test_configures_only_public_metadata_on_express_app(self):
        identity = {"id": "/subscriptions/test/resourceGroups/test/providers/Microsoft.ManagedIdentity/userAssignedIdentities/app",
                    "clientId": "02890ec3-a8ae-44b3-8da7-83cf83b9c70d"}
        vault = {"properties": {"vaultUri": "https://testvault.vault.azure.net/"}}
        app = {"identity": {"userAssignedIdentities": {identity["id"]: {}}}}
        updated = dict(app, properties={"template": {"containers": [{"env": [
            dict(name="AZURE_KEY_VAULT_URL", value=vault["properties"]["vaultUri"]),
            dict(name="AZURE_CLIENT_ID", value=identity["clientId"])]}]}})
        calls = []
        class FakeCli:
            subscription = "test"
            def run(self, *arguments, json_output=False):
                calls.append(arguments)
                if json_output:
                    return app if len(calls) == 1 else updated
        with redirect_stdout(io.StringIO()):
            configure_app(FakeCli(), "group", "app", identity, vault)
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[0][:3], ("rest", "--method", "get"))
        self.assertIn("AZURE_KEY_VAULT_URL=https://testvault.vault.azure.net/", calls[1])
        self.assertIn("AZURE_CLIENT_ID=" + identity["clientId"], calls[1])
        self.assertFalse(any("keyvaultref:" in argument for call in calls for argument in call))

    @unittest.skipUnless(sys.platform == "win32", "Windows CLI shim")
    def test_windows_cli_uses_explicit_utf8_without_the_command_shim(self):
        result = subprocess.CompletedProcess([], 0, stdout='"clasificación"', stderr="")
        with patch("scripts.sync_keyvault.shutil.which", return_value="C:/Azure/CLI2/wbin/az.cmd"), \
                patch("scripts.sync_keyvault.Path.is_file", return_value=True), \
                patch("scripts.sync_keyvault.subprocess.run", return_value=result) as run:
            cli = AzureCli("a6dbfedb-8913-4e08-b801-c60fcad0a795")
            self.assertEqual(cli.run("account", "show", json_output=True), "clasificación")
        command = run.call_args.args[0]
        self.assertEqual(command[1:5], ["-X", "utf8", "-IBm", "azure.cli"])

    def test_access_and_app_templates_use_the_same_role_assignment(self):
        app = json.loads(TEMPLATE.read_text(encoding="utf-8"))
        access = json.loads((TEMPLATE.parent / "keyvault-access.json").read_text(encoding="utf-8"))
        for name in ("identityId", "vaultId", "keyVaultSecretsUserRoleId", "keyVaultSecretsUserAssignmentName"):
            self.assertEqual(app["variables"][name], access["variables"][name])


class RuntimeKeyVaultTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict("os.environ", {
            "AZURE_KEY_VAULT_URL": "https://testvault.vault.azure.net/",
            "AZURE_CLIENT_ID": "02890ec3-a8ae-44b3-8da7-83cf83b9c70d",
        }, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_uses_selected_managed_identity_and_latest_secret_names(self):
        with patch("azure.identity.ManagedIdentityCredential") as credential, \
                patch("azure.keyvault.secrets.SecretClient") as client:
            secret_client = client.return_value.__enter__.return_value
            secret_client.get_secret.return_value.value = "synthetic-value"
            values = read_keyvault_secrets(SECRET_NAMES)
        credential.assert_called_once_with(client_id="02890ec3-a8ae-44b3-8da7-83cf83b9c70d")
        self.assertEqual(values, {name: "synthetic-value" for name in SECRET_NAMES})
        self.assertEqual([call.args for call in secret_client.get_secret.call_args_list],
                         [(name,) for name in SECRET_NAMES.values()])
        self.assertFalse(client.call_args.kwargs["logging_enable"])

    def test_bad_metadata_and_unknown_secrets_fail_without_network(self):
        for overrides in ({"AZURE_KEY_VAULT_URL": "http://testvault.vault.azure.net"},
                          {"AZURE_KEY_VAULT_URL": "https://other-service.example/secrets"},
                          {"AZURE_CLIENT_ID": ""}):
            with self.subTest(overrides=overrides), patch.dict("os.environ", overrides):
                with self.assertRaises(KeyVaultError):
                    read_keyvault_secrets(("clef_api_token",))
        with self.assertRaises(KeyVaultError):
            read_keyvault_secrets(("unknown",))

    def test_sdk_failure_and_empty_secret_are_safe_errors(self):
        with patch("azure.identity.ManagedIdentityCredential"), \
                patch("azure.keyvault.secrets.SecretClient") as client:
            secret_client = client.return_value.__enter__.return_value
            secret_client.get_secret.side_effect = RuntimeError("private-upstream-response")
            with self.assertRaises(KeyVaultError) as error:
                read_keyvault_secrets(("clef_api_token",))
            self.assertNotIn("private-upstream-response", str(error.exception))
            secret_client.get_secret.side_effect = None
            secret_client.get_secret.return_value.value = ""
            with self.assertRaisesRegex(KeyVaultError, "empty"):
                read_keyvault_secrets(("clef_api_token",))


if __name__ == "__main__":
    unittest.main()
