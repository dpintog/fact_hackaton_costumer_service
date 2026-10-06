"""Managed identity access to application secrets stored in Azure Key Vault."""

import os
import re
from uuid import UUID


SECRET_NAMES = {
    "clef_api_token": "clef-api-token",
    "clef_Account_ID": "clef-account-id",
    "storage_connection_string": "storage-connection-string",
}


class KeyVaultError(RuntimeError):
    """A configuration or retrieval failure with no upstream body or secret values."""


def read_keyvault_secrets(names):
    """Read latest versions using only the app's user-assigned managed identity.

    Callers request only the secrets they need. Cloud authentication failures do
    not fall back to local .env values. The storage secret is available through
    this function when a storage client is added to the application.
    """
    names = tuple(names)
    if not names or any(name not in SECRET_NAMES for name in names):
        raise KeyVaultError("Unknown or empty application secret selection.")
    return _read_keyvault_values({name: SECRET_NAMES[name] for name in names})


def read_keyvault_secret(secret_name):
    """Read a deployment secret by its explicitly configured vault name."""
    if not isinstance(secret_name, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,127}", secret_name):
        raise KeyVaultError("Invalid deployment secret name.")
    return _read_keyvault_values({"deployment": secret_name})["deployment"]


def _read_keyvault_values(selected):
    vault_url = os.environ.get("AZURE_KEY_VAULT_URL", "").strip()
    if not re.fullmatch(r"https://[A-Za-z0-9-]+\.vault\.azure\.net/?", vault_url):
        raise KeyVaultError("AZURE_KEY_VAULT_URL must be an HTTPS Azure Key Vault URL.")
    try:
        client_id = str(UUID(os.environ.get("AZURE_CLIENT_ID", "")))
    except ValueError:
        raise KeyVaultError("AZURE_CLIENT_ID must identify the app's user-assigned managed identity.") from None
    try:
        from azure.identity import ManagedIdentityCredential
        from azure.keyvault.secrets import SecretClient
        with ManagedIdentityCredential(client_id=client_id) as credential:
            with SecretClient(vault_url=vault_url, credential=credential,
                              logging_enable=False, retry_total=2,
                              connection_timeout=5, read_timeout=15) as client:
                values = {name: client.get_secret(vault_name).value for name, vault_name in selected.items()}
        if any(not isinstance(value, str) or not value.strip() for value in values.values()):
            raise KeyVaultError("An application secret in Key Vault is empty.")
        return values
    except KeyVaultError:
        raise
    except Exception:
        # Azure errors may include request/response details. Keep them out of logs.
        raise KeyVaultError("Unable to read application secrets from Key Vault; check the managed identity and its vault access.") from None
