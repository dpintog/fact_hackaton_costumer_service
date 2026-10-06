# Deploy and test the customer service app

The image contains application code only. The original files remain in
`stlatambank`, container `data`. Container startup reads `customers.csv`,
`marketing_campaigns.csv`, `products.csv`, `campaign_sends/`, and `transactions/`
using the storage connection string retrieved from Key Vault. The application
never uploads or modifies Storage blobs.

The existing preparation code builds its SQLite working cache inside the
container from that source snapshot. It retains the original validation,
campaign policy, indexes, provenance, and scenario generation. No CSVs or database
files are included in the image. Conditional ETag reads detect blobs that change
while downloading, and unsafe source paths are rejected.

## Runtime configuration

- Container App: `ca-camps-ahorro`; HTTPS public origin remains unchanged.
- Image registry: `crcampsahorro.azurecr.io`.
- Port: `8002`; non-root container user.
- Scale: minimum `1`, maximum `1` replica.
- Resources: `2` vCPU, `4 GiB` memory, `8 GiB` ephemeral disk for full-data preparation.
- Classifier: Clef through `config/intent.azure.yaml`.
- Source: account `stlatambank`, container `data`, empty `APP_DATA_PREFIX` (existing files at the container root).
- Vault: `kvcampsahorro`, using the existing user-assigned managed identity.

`AZURE_KEY_VAULT_URL`, `AZURE_CLIENT_ID`, `APP_PUBLIC_ORIGIN`,
`APP_STORAGE_ACCOUNT`, `APP_DATA_CONTAINER`, and `APP_DATA_PREFIX` are public
configuration metadata. The three application secret values remain in Key Vault.
Private demo login material is stored separately as `app-access-credentials`.
It contains an operator password and a random seed used to derive distinct,
stable passwords for the original scenario identities after preparation.
The local private copy is `outputs/deployment/access_credentials.json`.

## Initialization and state lifetime

The container keeps `/healthz` responsive with `status: initializing` while it
reads the source files and builds SQLite. Application routes return HTTP `503`
until preparation finishes. Afterward `/healthz` returns `status: ok` and the
normal authenticated interface is available. Large source snapshots take time
to prepare; do not treat a healthy initialization probe as a completed app test.

A minimum of one replica prevents idle scale-to-zero. Azure can still replace
the container during maintenance, failures, or redeployment. A replacement reads
the original blobs and rebuilds the cache. Runtime sessions, conversations,
confirmed campaign lists, and local service requests are ephemeral and reset.
This is the agreed demo configuration; durable state needs a separate design.

## Build, deploy, and check

Install the dependencies from `requirements.txt`. The dedicated Azure CLI login
on this machine is in `.azure-project`:

```powershell
$env:AZURE_CONFIG_DIR = (Resolve-Path .azure-project).Path
az acr login --subscription a6dbfedb-8913-4e08-b801-c60fcad0a795 --name crcampsahorro
```

ACR Tasks is blocked for this student subscription (`TasksOperationsNotAllowed`).
Build with local Docker's Linux engine and push to the existing registry. Use a
fresh tag for each changed image:

```powershell
docker --context desktop-linux build --tag crcampsahorro.azurecr.io/customer-service:<tag> .
docker --context desktop-linux push crcampsahorro.azurecr.io/customer-service:<tag>
az deployment group create --subscription a6dbfedb-8913-4e08-b801-c60fcad0a795 --resource-group factored-hackaton-DR --template-file infra/azure/containerapp.json --parameters image=crcampsahorro.azurecr.io/customer-service:<tag> --output none --only-show-errors
```

The allowlist in `.dockerignore` excludes `.env`, data, outputs, credentials, and
Azure login caches. Do not pass secret values as build arguments or environment
values in the deployment template. An image change replaces the container and
requires another initialization pass.

Wait until `/healthz` reports `status: ok`, then run:

```powershell
python scripts/smoke_container.py --url https://ca-camps-ahorro.politedune-96575e92.northcentralus.azurecontainerapps.io --credentials outputs/deployment/access_credentials.json
```

The checks cover health, the UI, authentication, role permissions, campaign
selection over the original data, scenario coverage, Spanish/Portuguese chat,
and rejection of secret-file and cross-origin requests. The test logs out its
sessions and saves only check names/statuses in
`outputs/deployment/smoke-results.json`. Passwords, bearer tokens, and customer
records are omitted from test output. Startup logs separately confirm managed
identity retrieval from Key Vault and acquisition of the original Blob snapshot.

Use `--confirm-batch` to additionally confirm a local campaign list, verify
idempotent replay, and check the complete authenticated CSV export. This option
writes one test batch to ephemeral app state and sends no advertising. The
verified deployment passed all **15** checks with this option. Usable private
operator/scenario logins are in `outputs/deployment/login_credentials.json`.

The uploading login needs secret write permission only when the private access
secret is first created or rotated. Temporary vault upload grants used during
setup are removed after verification. The runtime identity retains only
`Key Vault Secrets User` at this vault's scope.
