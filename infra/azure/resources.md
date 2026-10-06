# Azure resource inventory: factored-hackaton-DR

Original snapshot checked on **October 5, 2026, approximately 23:28–23:30 America/Bogota (UTC−05:00)** using Azure CLI and Azure Resource Manager metadata. The integration and deployment updates below supersede the original application findings.

## Application deployment verified: October 6, 2026

- **Image:** `crcampsahorro.azurecr.io/customer-service:kv-1ee97750`; digest `sha256:b9b00c9e20407aed668f350753b4ea76eb321f7fc15a41cc9cce5ee48c2a2c68`.
- **Deployment:** `customer-service-keyvault`, provisioning state `Succeeded`; public `/healthz` returns `{"status":"ok"}`.
- **Scale/resources:** minimum `1`, maximum `1` replica; `2` vCPU, `4 GiB` memory, `8 GiB` ephemeral disk; port `8002`.
- **Data:** reads the existing CSVs in `stlatambank`, container `data`, and prepares the SQLite cache inside the container. No data or database files are included in the image; no Storage blobs are uploaded or modified by the app.
- **Secrets:** user-assigned managed identity retrieves the three application secrets and the separate private demo-access secret from `kvcampsahorro`.
- **Verified behavior:** all **15 live deployment checks passed**, including operator/customer authentication, role restrictions, original-data campaign selection, list confirmation and idempotent replay, complete CSV export, and Clef chat in Spanish/Portuguese.
- **Scenario coverage:** all `13` original-data scenario profiles are available. Prepared source version: `1b28e7b565ace1d0b72936b0d28ff628493c03c67cfcf97800598056ea97276e`.
- **State lifetime:** the agreed demo uses ephemeral SQLite data and state. A container replacement rebuilds its query cache and resets local sessions/actions; one minimum replica prevents idle scale-to-zero.

The repeatable setup is documented in [deployment.md](./deployment.md). Private logins and test evidence are saved locally under the Git-ignored `outputs/deployment/` directory.

## Key Vault integration update

- Uploaded and verified the three root `.env` entries as `clef-api-token`, `clef-account-id`, and `storage-connection-string` in `kvcampsahorro`. Values were compared in memory and were not printed or committed.
- Granted `id-ca-camps-ahorro-acrpull` the vault-scoped **Key Vault Secrets User** role. The role assignment is `2ca2ecdb-d4db-5570-9935-28447bdf9a4c`.
- Configured the app with `AZURE_KEY_VAULT_URL=https://kvcampsahorro.vault.azure.net/` and `AZURE_CLIENT_ID=02890ec3-a8ae-44b3-8da7-83cf83b9c70d`, verified using ARM API `2026-03-02-preview`. These are public configuration values.
- Express rejected native Key Vault secret references with `ExpressEnvironmentFeatureNotSupported`. The Python code now uses `SecretClient` and the app's user-assigned managed identity instead. [Express supported features](https://learn.microsoft.com/en-us/azure/container-apps/express-overview).
- The temporary vault-scoped uploader permission was removed after verification. The app identity retains read access.
- Azure validated and deployed the updated template. The application image is running and passed the live checks listed above.

See [Key Vault setup and upload instructions](./README.md) and the [runtime secret reader](../../src/campaigns/secrets.py).

## Scope

| Item | Observed value |
| --- | --- |
| Subscription | Azure for Students |
| Subscription ID | `a6dbfedb-8913-4e08-b801-c60fcad0a795` |
| Resource group | `factored-hackaton-DR` |
| Resource group metadata location | Canada Central (`canadacentral`) |
| Location of all seven listed resources | North Central US (`northcentralus`) |
| Resource group provisioning state | Succeeded |
| Resource group tags | None returned |

The group's metadata location differs from the deployment region of its resources. Seven resources were returned by the resource-group inventory. Their provisioning states were all `Succeeded`; the Container App also reported `Running`. RBAC assignments are described separately below.

The original inventory was a read-only configuration snapshot: stored files, blobs, logs, usage, and billing were not inspected. Registry repository names and relevant role assignments were checked. The integration update records the later secret uploads, vault role assignment, and public app configuration changes.

## Resource overview

| Resource | Azure resource type | Purpose in this setup |
| --- | --- | --- |
| `ca-camps-ahorro` | `Microsoft.App/containerApps` | Runs the public web container |
| `managedEnvironment-factoredhackato-9435` | `Microsoft.App/managedEnvironments` | Express environment hosting the app |
| `crcampsahorro` | `Microsoft.ContainerRegistry/registries` | Registry for future application container images |
| `id-ca-camps-ahorro-acrpull` | `Microsoft.ManagedIdentity/userAssignedIdentities` | App identity with registry pull permission |
| `workspacefactoredhackatondr8c2b` | `Microsoft.OperationalInsights/workspaces` | Configured destination for environment logs |
| `stlatambank` | `Microsoft.Storage/storageAccounts` | Existing CSV source read by the app using its vault-held connection string |
| `kvcampsahorro` | `Microsoft.KeyVault/vaults` | Stores the three app secrets; managed identity reader access configured |

## Container App: ca-camps-ahorro

- **Public endpoint:** [Open the app](https://ca-camps-ahorro.politedune-96575e92.northcentralus.azurecontainerapps.io).
- **Current image:** `crcampsahorro.azurecr.io/customer-service:kv-1ee97750`.
- **Environment:** `managedEnvironment-factoredhackato-9435`.
- **Ingress:** external HTTP ingress, container target port `8002`, insecure HTTP disabled.
- **Resources per replica:** `2` vCPU, `4 GiB` memory, `8 GiB` ephemeral storage.
- **Scale:** minimum `1`, maximum `1` replica.
- **Identity:** user-assigned identity `id-ca-camps-ahorro-acrpull`.
- **Environment variables:** public vault URL/client ID, HTTPS app origin, access-secret name, Storage account/container, and empty source prefix. Values are defined in the deployment template.
- **Configured app secrets:** secret values remain in Key Vault for direct SDK retrieval.
- **Registry entry:** `crcampsahorro.azurecr.io`, authenticated with the attached user-assigned identity.

The repository's customer service application is deployed. Private registry pulling, vault-backed credentials, original Blob data preparation, and the main user workflows have been exercised successfully.

## Container Apps environment: managedEnvironment-factoredhackato-9435

- **Mode:** `Express`.
- **Environment-level managed identity:** none.
- **Public network access:** enabled.
- **Default domain:** `politedune-96575e92.northcentralus.azurecontainerapps.io`.
- **Log destination:** Log Analytics.
- **Configured workspace:** `workspacefactoredhackatondr8c2b`, confirmed by matching the workspace customer ID.
- **Infrastructure resource group:** none returned.

The previous portal deployment failed with `ExpressEnvironmentManagedIdentityNotSupported` because it requested a system-assigned identity on the environment. The current environment has no identity, while the app has a user-assigned identity. Express supports app-level user-assigned identities for runtime access and ACR image pulls. [Express capabilities](https://learn.microsoft.com/en-us/azure/container-apps/express-overview).

## Container registry: crcampsahorro

- **Login server:** `crcampsahorro.azurecr.io`.
- **SKU:** `Standard`.
- **Public network access:** enabled.
- **Admin user:** disabled.
- **Role assignment mode:** `LegacyRegistryPermissions`.
- **Repository inventory:** `customer-service`, with the verified application image above.
- **App identity permission:** `AcrPull`, scoped to this registry only.

The running application image comes from this registry. The app's managed identity authenticates image pulls without an admin username or password. ACR Tasks is blocked for this student subscription (`TasksOperationsNotAllowed`), so the image was built with local Docker and pushed to this registry.

The [deployment template](./containerapp.json) adds that registry configuration when its `image` parameter starts with `crcampsahorro.azurecr.io/`.

## Managed identity: id-ca-camps-ahorro-acrpull

- **Type:** user-assigned managed identity.
- **Attached to:** `ca-camps-ahorro`.
- **Principal/object ID:** `a5801d61-8b22-4ff0-9e7f-054fe905ef03`.
- **Client ID:** `02890ec3-a8ae-44b3-8da7-83cf83b9c70d`.
- **Verified role:** `AcrPull` on `crcampsahorro`.
- **Key Vault role:** `Key Vault Secrets User` on `kvcampsahorro`, added in the integration update.

The identity has no observed role assignment at `stlatambank`. Its vault-scoped reader assignment is now verified at `kvcampsahorro`. These checks concern this app identity; they are not an inventory of every user's or service principal's permissions.

## Log Analytics workspace: workspacefactoredhackatondr8c2b

- **SKU:** `PerGB2018`.
- **Retention:** `30` days.
- **Daily ingestion quota:** `-1`, meaning no configured daily cap. [Azure CLI quota definition](https://learn.microsoft.com/en-us/cli/azure/monitor/log-analytics/workspace?view=azure-cli-latest).
- **Public ingestion and query access:** enabled.
- **Resource-based log access flag:** enabled.
- **Connected resource:** the Express environment uses this workspace as its log destination.

Application console logs were successfully queried after deployment. They include source reading, SQLite preparation, cache readiness, and Clef startup. Overall ingestion volume and billing were not inspected. The installed `az containerapp logs show` command cannot read Express logs (`eventStreamEndpoint` missing); use Log Analytics queries instead.

## Storage account: stlatambank

- **Account kind:** `StorageV2`.
- **SKU:** `Standard_RAGRS`.
- **Primary region:** North Central US.
- **Secondary region:** South Central US (`southcentralus`).
- **Default blob access tier:** `Hot`.
- **Blob endpoint:** [stlatambank Blob Storage](https://stlatambank.blob.core.windows.net/).
- **File endpoint:** [stlatambank Azure Files](https://stlatambank.file.core.windows.net/).
- **HTTPS required:** yes; minimum TLS version `TLS1_2`.
- **Anonymous blob access:** disabled at the account level.
- **Shared Key access:** allowed.
- **Public network access:** enabled; network default action `Allow`.
- **Hierarchical namespace:** the queried flag returned `null`; its enabled state was not established by this check.

RA-GRS means read-access geo-redundant storage: data is replicated asynchronously to a secondary region, with secondary reads supported for eligible services. Azure Files does not support secondary read access through RA-GRS. [Storage redundancy documentation](https://learn.microsoft.com/en-us/azure/storage/common/storage-redundancy).

The app reads the existing `data` container's customer, campaign, product, campaign-send, and transaction CSVs through the connection string held in Key Vault. The connection string uses Shared Key authentication; the app identity has no observed Storage RBAC assignment. Application code only lists/downloads source blobs and writes the derived working cache to local container storage. Original blobs remain unchanged.

## Key Vault: kvcampsahorro

Azure Key Vault provides storage and access management for secrets, keys, and certificates. [Key Vault overview](https://learn.microsoft.com/en-us/azure/key-vault/general/overview).

- **Vault URI:** [kvcampsahorro](https://kvcampsahorro.vault.azure.net/).
- **SKU:** `Standard`.
- **Authorization model:** Azure RBAC enabled.
- **Legacy access policies:** `0`; this does not imply there are no RBAC-authorized users.
- **Soft delete:** enabled, with `90` days of retention.
- **Purge protection:** the queried flag returned `null`; no enabled value was reported.
- **Public network access:** enabled; network default action `Allow`.

The app identity has vault-scoped reader access. The three `.env` secrets and the separate `app-access-credentials` secret were uploaded and verified privately. Runtime startup retrieves these through the SDK, and authenticated live tests passed. Keys and certificates were not inspected. Temporary uploader access was removed after verification.

## Verified relationships

| From | To | Observed relationship |
| --- | --- | --- |
| `ca-camps-ahorro` | Express environment | App references this environment |
| Express environment | Log Analytics workspace | Configured logging destination; customer IDs match |
| `ca-camps-ahorro` | Managed identity | User-assigned identity attached to the app |
| Managed identity | `crcampsahorro` | Registry-scoped `AcrPull` assignment |
| `ca-camps-ahorro` | `crcampsahorro` | Private application image pulled using the attached managed identity |
| `ca-camps-ahorro` | `kvcampsahorro` | Public vault URL/client ID configured; app identity has vault-scoped `Key Vault Secrets User` |
| `ca-camps-ahorro` | `stlatambank` | Reads existing source CSVs using the vault-held connection string; creates SQLite only in its local cache |

## Resource identifiers and refresh

All seven resources share this ARM resource-group prefix:

```text
/subscriptions/a6dbfedb-8913-4e08-b801-c60fcad0a795/resourceGroups/factored-hackaton-DR
```

Append `/providers/<Azure resource type>/<resource name>` using the overview table to obtain each resource ID. The registry pull permission is an additional `Microsoft.Authorization/roleAssignments` extension resource scoped to the registry.

To refresh the top-level inventory with an authenticated Azure CLI session, run this read-only command. It explicitly targets the student subscription without changing the default subscription:

```powershell
az resource list --subscription a6dbfedb-8913-4e08-b801-c60fcad0a795 --resource-group factored-hackaton-DR --query "[].{name:name,type:type,location:location}" --output table --only-show-errors
```

The [Container App template](./containerapp.json) defines the app, environment, managed identity, registry pull role, and Key Vault reader role, and sets the public vault connection metadata. It references the existing registry, workspace, and vault. It does not provision the storage account or Key Vault. [keyvault-access.json](./keyvault-access.json) can grant the runtime reader role independently before app deployment.
