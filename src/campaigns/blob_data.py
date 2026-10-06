"""Read the existing Blob CSVs and build a disposable SQLite application cache."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import re


class BlobDataError(RuntimeError):
    """A storage failure that never includes credentials or service bodies."""


def download_source_data(connection_string, container, prefix, root, account="stlatambank"):
    if not re.fullmatch(r"[a-z0-9-]{3,63}", container or ""):
        raise BlobDataError("APP_DATA_CONTAINER is invalid.")
    prefix = (prefix or "").strip("/")
    if prefix and (not re.fullmatch(r"[A-Za-z0-9_/-]+", prefix) or ".." in prefix):
        raise BlobDataError("APP_DATA_PREFIX is invalid.")
    base = prefix + "/" if prefix else ""
    data_root = (Path(root) / "data").resolve()
    data_root.mkdir(parents=True, exist_ok=True)
    try:
        from azure.core import MatchConditions
        from azure.storage.blob import BlobServiceClient
        with BlobServiceClient.from_connection_string(connection_string, logging_enable=False,
                                                       connection_timeout=10, read_timeout=60) as service:
            if service.account_name != account:
                raise BlobDataError("The vault connection string targets a different storage account.")
            client = service.get_container_client(container)
            blobs = [client.get_blob_client(base + name).get_blob_properties()
                     for name in ("customers.csv", "marketing_campaigns.csv", "products.csv")]
            for directory in ("campaign_sends", "transactions"):
                partitions = [blob for blob in client.list_blobs(name_starts_with=base + directory + "/")
                              if blob.name.endswith(".csv")]
                if not partitions:
                    raise BlobDataError("A required dataset partition directory is empty.")
                blobs.extend(partitions)
            manifest = []
            for blob in blobs:
                relative = blob.name[len(base):]
                if (not re.fullmatch(r"[A-Za-z0-9_=./-]+", relative)
                        or any(part in (".", "..") for part in relative.split("/"))):
                    raise BlobDataError("An unsafe dataset blob path was rejected.")
                destination = (data_root / relative).resolve()
                if not destination.is_relative_to(data_root):
                    raise BlobDataError("A dataset blob path is outside the cache directory.")
                manifest.append(dict(name=relative, bytes=blob.size, etag=blob.etag))

            def download(blob):
                destination = data_root / blob.name[len(base):]
                destination.parent.mkdir(parents=True, exist_ok=True)
                temporary = destination.with_suffix(destination.suffix + ".download")
                try:
                    with temporary.open("wb") as handle:
                        client.get_blob_client(blob.name).download_blob(
                            etag=blob.etag, match_condition=MatchConditions.IfNotModified,
                            max_concurrency=4).readinto(handle)
                    if temporary.stat().st_size != blob.size:
                        raise BlobDataError("A downloaded source file has an unexpected size.")
                    temporary.replace(destination)
                finally:
                    temporary.unlink(missing_ok=True)

            print(f"Reading {len(blobs)} existing CSV files from {account}/{container}.", flush=True)
            with ThreadPoolExecutor(max_workers=8) as executor:
                for count, _ in enumerate(executor.map(download, blobs), 1):
                    if count % 200 == 0 or count == len(blobs):
                        print(f"Blob data read: {count}/{len(blobs)} files.", flush=True)
        manifest.sort(key=lambda item: item["name"])
        source = dict(account=account, container=container, prefix=prefix, files=manifest)
        source["blob_version"] = hashlib.sha256(json.dumps(source, sort_keys=True).encode()).hexdigest()
        out = Path(root) / "outputs"
        out.mkdir(parents=True, exist_ok=True)
        (out / "blob_source.json").write_text(json.dumps(source, indent=2), encoding="utf-8")
        return source
    except BlobDataError:
        raise
    except Exception:
        raise BlobDataError("Unable to read the existing Azure Blob data; check vault credentials and dataset files.") from None


def prepare_application_cache(root):
    from .prepare import audit_and_prepare, load_config
    from .phase2 import prepare
    from .scenarios import build_catalog
    root = Path(root)
    print("Preparing SQLite cache: validating customers, campaigns, and contact history.", flush=True)
    audit_and_prepare(root, root / "outputs/day1", load_config(root / "config/day1.json"))
    print("Preparing SQLite cache: accounts, transactions, and campaign decisions.", flush=True)
    prepare(root, root / "outputs/phase2", load_config(root / "config/project.json"))
    print("Preparing the scenario catalog.", flush=True)
    catalog = build_catalog(root / "outputs/phase2/prepared.sqlite", root / "outputs/scenarios")
    print(f"SQLite cache ready; scenario categories: {catalog['coverage']['covered_categories']}.", flush=True)
    return catalog
