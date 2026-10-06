# Key Vault secrets for Container Apps

The existing `kvcampsahorro` vault stores the individual values from the root `.env`.
When deployed, the Python application reads secrets with `SecretClient` and the
existing `id-ca-camps-ahorro-acrpull` user-assigned managed identity.

This app uses an **Express** environment, which supports runtime managed identity
but does not support native Key Vault secret references. The application therefore
reads the vault directly. [Microsoft's Express feature table](https://learn.microsoft.com/en-us/azure/container-apps/express-overview).

| Application credential name | Key Vault secret name |
| --- | --- |
| `clef_api_token` | `clef-api-token` |
| `clef_Account_ID` | `clef-account-id` |
| `storage_connection_string` | `storage-connection-string` |

`SECRET_NAMES` in [secrets.py](../../src/campaigns/secrets.py) is the shared mapping
used by the runtime and uploader. Vault names use hyphens because underscores are
not valid Key Vault secret name characters. Container startup uses the storage
connection string to read the existing Blob CSVs and prepare its local SQLite
cache. Clef reads its two credentials only when
`intent_classifier.provider: clef` is selected. TF-IDF and baseline routing do not
contact the vault.

The app uses two public environment variables:

- `AZURE_KEY_VAULT_URL=https://kvcampsahorro.vault.azure.net/`
- `AZURE_CLIENT_ID=02890ec3-a8ae-44b3-8da7-83cf83b9c70d`

When `AZURE_KEY_VAULT_URL` is present, Clef retrieves its credentials from the vault
using `ManagedIdentityCredential(client_id=AZURE_CLIENT_ID)`. Azure credential
failures stop startup with a sanitized error. Locally, leave the vault variable
unset to retain existing environment-variable and `.env` behavior. The cloud path
uses no Azure CLI login, client secret, or local `.env` file.

The app identity has **Key Vault Secrets User** at this vault's scope. The uploader
separately needs permission to set and read secrets, such as **Key Vault Secrets
Officer** at the vault's scope. Assigning roles requires permission to manage
RBAC; subscription Owner alone does not give secret data access to an RBAC vault.
The temporary uploader role used for the initial upload was removed afterward.

## Upload or rotate secrets

Install `requirements.txt` and authenticate Azure CLI in the target subscription.
On this machine, the relevant login is in `.azure-project`, so set the CLI directory
for these PowerShell commands. Other environments can omit this setting and the
script's `--azure-config-dir` argument to use their own authenticated login.
The uploading account must have the secret permissions described above.

```powershell
$env:AZURE_CONFIG_DIR = (Resolve-Path .azure-project).Path

# Grant the existing app identity read access.
az deployment group create --subscription a6dbfedb-8913-4e08-b801-c60fcad0a795 --resource-group factored-hackaton-DR --name keyvault-app-access --template-file infra/azure/keyvault-access.json --output none --only-show-errors

# Upload the mapped .env entries and configure the app's public vault metadata.
python scripts/sync_keyvault.py --azure-config-dir .azure-project --configure-app
```

Use `python scripts/sync_keyvault.py --azure-config-dir .azure-project` to upload
without changing the Container App. On this machine, `.venv/Scripts/python.exe`
can replace `python`.

The script validates all mapped entries before uploading and rejects unmapped
`.env` entries. Values travel through temporary UTF-8 files that are removed even
on failure. It verifies each stored value in memory, without printing values or
putting them in CLI arguments. Every upload creates a new secret version. Uploads
are not a transaction across all three secrets; rerun after correcting a failure.
Do not enable Azure CLI debug logging when handling secrets.

The runtime reads the latest secret versions at startup. Restart or redeploy the
application after rotation to refresh Clef's credentials; there is no native
Container Apps reference to trigger an automatic refresh.

## Application deployment

[containerapp.json](./containerapp.json) includes the vault-scoped reader role and
public vault, app-origin, and Storage configuration. Both deployment templates use the same
deterministic role assignment name. Upload secrets, grant the identity access,
and allow RBAC propagation before deploying the application image.

Keep `.env`, Azure login caches, generated access credentials, and state databases
out of the image and source control. Install the Azure dependencies in
`requirements.txt`. Supply the built image and its listening port through the
`image` and `targetPort` template parameters. Include `src/campaigns/secrets.py`
in the image so Clef can retrieve credentials.

The [container deployment instructions](./deployment.md) prepare the stored data,
build the application image, and test it in Azure. The container entry point uses
port `8002` and an explicitly configured HTTPS origin; local runs retain loopback
binding by default. Access credentials are supplied through a separate vault
secret and remain outside the image.

## Verify without displaying values

```powershell
az rest --subscription a6dbfedb-8913-4e08-b801-c60fcad0a795 --method get --url "https://management.azure.com/subscriptions/a6dbfedb-8913-4e08-b801-c60fcad0a795/resourceGroups/factored-hackaton-DR/providers/Microsoft.App/containerApps/ca-camps-ahorro?api-version=2026-03-02-preview" --query "{identity:identity,environment:properties.template.containers[].env,state:properties.provisioningState}" --output json --only-show-errors
```

The result should show the two public environment variables and the attached
user-assigned identity. The installed CLI's older `containerapp show` API omits
Express environment variable values; use the current ARM API above. Secret values are stored in Key Vault, outside the
Container App environment configuration. Avoid unfiltered secret-value commands,
such as `az keyvault secret show` without a metadata-only query.
