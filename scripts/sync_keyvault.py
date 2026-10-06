"""Upload .env secrets and optionally configure Container Apps Key Vault access.

Secret values never enter CLI arguments or console output. Only the explicitly
mapped application secret names are accepted, so new .env entries cannot
silently miss deployment. Azure CLI authentication is reused; no SDK is needed.
"""

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from uuid import UUID

from dotenv import dotenv_values


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from campaigns.secrets import SECRET_NAMES

SUBSCRIPTION = "a6dbfedb-8913-4e08-b801-c60fcad0a795"


class AzureError(RuntimeError):
    def __init__(self, operation, code="AzureCliError"):
        self.code = code
        super().__init__(f"{operation} failed ({code}); check Azure login and permissions.")


class AzureCli:
    def __init__(self, subscription, config_dir=None):
        self.executable = shutil.which("az")
        if not self.executable:
            raise ValueError("Azure CLI is required (az was not found on PATH).")
        self.command_prefix = [self.executable]
        if os.name == "nt" and self.executable.lower().endswith(".cmd"):
            cli_python = Path(self.executable).parent.parent / "python.exe"
            if cli_python.is_file():
                # The Windows az.cmd shim uses an isolated Python that otherwise
                # emits the local code page and ignores PYTHONIOENCODING. Explicit
                # UTF-8 also avoids cmd.exe interpreting query punctuation.
                self.command_prefix = [str(cli_python), "-X", "utf8", "-IBm", "azure.cli"]
        self.subscription = str(UUID(subscription))
        self.environment = os.environ.copy()
        if config_dir is not None:
            self.environment["AZURE_CONFIG_DIR"] = str(Path(config_dir).resolve())

    def run(self, *arguments, json_output=False):
        command = [*self.command_prefix, *arguments, "--subscription", self.subscription,
                   "--only-show-errors", "--output", "json" if json_output else "none"]
        result = subprocess.run(command, env=self.environment, capture_output=True,
                                text=True, encoding="utf-8", timeout=300)
        if result.returncode:
            # Never echo stdout/stderr: upstream responses can contain credentials.
            match = re.search(r"\(([A-Za-z][A-Za-z0-9]+)\)", result.stderr)
            raise AzureError(" ".join(arguments[:3]), match[1] if match else "AzureCliError")
        if json_output:
            try:
                return json.loads(result.stdout)
            except json.JSONDecodeError:
                raise AzureError(" ".join(arguments[:3]), "InvalidJson") from None


def load_secrets(env_file):
    mappings = [dict(environmentVariable=variable, secretName=name) for variable, name in SECRET_NAMES.items()]
    if not mappings:
        raise ValueError("The application must map at least one secret.")
    variables, names = set(), set()
    for mapping in mappings:
        variable, name = mapping["environmentVariable"], mapping["secretName"]
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", variable):
            raise ValueError("Invalid environment variable name in SECRET_NAMES.")
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,126}", name):
            raise ValueError("Secret names must use lowercase letters, digits, and hyphens.")
        if variable in variables or name in names:
            raise ValueError("Duplicate environment variable or secret name in SECRET_NAMES.")
        variables.add(variable)
        names.add(name)
    env_file = Path(env_file)
    if not env_file.is_file():
        raise ValueError("The .env file does not exist.")
    values = dotenv_values(env_file, encoding="utf-8-sig", interpolate=False)
    unknown = sorted(set(values) - variables)
    if unknown:
        raise ValueError("Add these .env names to SECRET_NAMES first: " + ", ".join(unknown))
    missing = sorted(variable for variable in variables
                     if not isinstance(values.get(variable), str) or not values[variable].strip())
    if missing:
        raise ValueError("Missing or empty .env entries: " + ", ".join(missing))
    return mappings, values


def upload_secrets(cli, vault_name, mappings, values):
    for mapping in mappings:
        name, variable = mapping["secretName"], mapping["environmentVariable"]
        # A private temporary directory; no .env file is copied into the repository.
        with tempfile.TemporaryDirectory(prefix="keyvault-upload-") as directory:
            secret_file = Path(directory) / "value.txt"
            secret_file.write_text(values[variable], encoding="utf-8", newline="")
            cli.run("keyvault", "secret", "set", "--vault-name", vault_name,
                    "--name", name, "--file", str(secret_file), "--encoding", "utf-8",
                    "--content-type", "text/plain", "--tags", f"environmentVariable={variable}")
        stored = cli.run("keyvault", "secret", "show", "--vault-name", vault_name,
                         "--name", name, "--query", "value", json_output=True)
        if stored != values[variable]:
            raise AzureError("Secret verification", "ValueMismatch")
        print(f"Uploaded and verified: {name}")


def configure_app(cli, resource_group, app_name, identity, vault):
    # The installed CLI's older show API omits Express environment values.
    # Read them with the same Express API version used by our ARM template.
    app_url = (f"https://management.azure.com/subscriptions/{cli.subscription}/resourceGroups/"
               f"{resource_group}/providers/Microsoft.App/containerApps/{app_name}?api-version=2026-03-02-preview")
    app = cli.run("rest", "--method", "get", "--url", app_url, json_output=True)
    if identity["id"].lower() not in {key.lower() for key in
                                      app.get("identity", {}).get("userAssignedIdentities", {})}:
        raise ValueError("The selected managed identity is not attached to the Container App.")
    # Express environments support runtime managed identity, but reject native
    # Key Vault references. Only public connection metadata is passed to the app.
    expected = {"AZURE_KEY_VAULT_URL": vault["properties"]["vaultUri"],
                "AZURE_CLIENT_ID": identity["clientId"]}
    env = [f"{name}={value}" for name, value in expected.items()]
    cli.run("containerapp", "update", "--resource-group", resource_group,
            "--name", app_name, "--set-env-vars", *env)
    updated = cli.run("rest", "--method", "get", "--url", app_url, json_output=True)
    containers = updated["properties"]["template"]["containers"]
    # az containerapp update applies --set-env-vars to the first container by default.
    environment = {item["name"]: item.get("value") for item in containers[0].get("env", [])}
    if any(environment.get(name) != value for name, value in expected.items()):
        raise AzureError("Container App Key Vault configuration verification", "ConfigurationMismatch")
    print(f"Verified Key Vault URL and managed identity client ID on {app_name}.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--subscription", default=SUBSCRIPTION)
    parser.add_argument("--resource-group", default="factored-hackaton-DR")
    parser.add_argument("--vault-name", default="kvcampsahorro")
    parser.add_argument("--app-name", default="ca-camps-ahorro")
    parser.add_argument("--identity-name", default="id-ca-camps-ahorro-acrpull")
    parser.add_argument("--azure-config-dir", type=Path,
                        help="Reuse a dedicated Azure CLI login (sets AZURE_CONFIG_DIR for child processes only).")
    parser.add_argument("--configure-app", action="store_true",
                        help="Also configure the existing app with the public vault URL and managed identity client ID.")
    args = parser.parse_args()
    for name in (args.resource_group, args.vault_name, args.app_name, args.identity_name):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
            raise ValueError("Azure resource names must use letters, digits, dots, underscores, and hyphens.")
    mappings, values = load_secrets(args.env_file)
    cli = AzureCli(args.subscription, args.azure_config_dir)
    vault = cli.run("keyvault", "show", "--resource-group", args.resource_group,
                    "--name", args.vault_name, json_output=True)
    if not vault["properties"].get("enableRbacAuthorization"):
        raise ValueError("The vault must use Azure RBAC authorization.")
    identity = None
    if args.configure_app:
        identity = cli.run("identity", "show", "--resource-group", args.resource_group,
                           "--name", args.identity_name, json_output=True)
    upload_secrets(cli, args.vault_name, mappings, values)
    if args.configure_app:
        configure_app(cli, args.resource_group, args.app_name, identity, vault)


if __name__ == "__main__":
    try:
        main()
    except (AzureError, ValueError, OSError, KeyError, subprocess.TimeoutExpired) as error:
        # Only explicitly sanitized errors are shown; other exceptions may hold CLI output.
        message = str(error) if isinstance(error, (AzureError, ValueError)) else type(error).__name__
        print(f"Key Vault sync failed: {message}", file=sys.stderr)
        sys.exit(1)
