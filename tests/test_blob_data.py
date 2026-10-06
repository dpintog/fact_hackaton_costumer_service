"""Read-only Blob acquisition, safe paths, and stable private cloud access."""

from contextlib import redirect_stdout
import io
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from campaigns.access import build_access_users, scenario_password
from campaigns.blob_data import BlobDataError, download_source_data


class BlobReadTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.files = {name: b"id\nsynthetic\n" for name in (
            "customers.csv", "marketing_campaigns.csv", "products.csv",
            "campaign_sends/year=2026/part.csv", "transactions/year=2026/part.csv")}
        self.calls = []
        files, calls = self.files, self.calls

        class Blob:
            def __init__(self, name):
                self.name = name
            def get_blob_properties(self):
                calls.append(("properties", self.name))
                return SimpleNamespace(name=self.name, size=len(files[self.name]), etag="original-etag")
            def download_blob(self, **kwargs):
                calls.append(("download", self.name, kwargs))
                return self
            def readinto(self, handle):
                handle.write(files[self.name])

        class Container:
            def get_blob_client(self, name):
                return Blob(name)
            def list_blobs(self, name_starts_with):
                calls.append(("list", name_starts_with))
                return [Blob(name).get_blob_properties() for name in files if name.startswith(name_starts_with)]

        class Service:
            account_name = "stlatambank"
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def get_container_client(self, name):
                calls.append(("container", name))
                return Container()

        self.service = Service()

    def load(self):
        with patch("azure.storage.blob.BlobServiceClient.from_connection_string", return_value=self.service), \
                redirect_stdout(io.StringIO()):
            return download_source_data("fake-private-connection", "data", "", self.root)

    def test_reads_existing_blobs_with_etag_guards_and_no_storage_writes(self):
        result = self.load()
        self.assertEqual(len(result["files"]), 5)
        for name, value in self.files.items():
            self.assertEqual((self.root / "data" / name).read_bytes(), value)
        downloads = [call for call in self.calls if call[0] == "download"]
        self.assertEqual(len(downloads), 5)
        self.assertTrue(all(call[2]["etag"] == "original-etag" for call in downloads))
        self.assertTrue(all(call[0] in {"properties", "download", "container", "list"} for call in self.calls))
        self.assertTrue((self.root / "outputs/blob_source.json").is_file())

    def test_rejects_source_path_traversal_before_downloading(self):
        self.files["transactions/../escape.csv"] = b"unsafe"
        with self.assertRaises(BlobDataError):
            self.load()
        self.assertFalse(any(call[0] == "download" for call in self.calls))

    def test_rejects_a_connection_to_the_wrong_account(self):
        self.service.account_name = "otheraccount"
        with self.assertRaises(BlobDataError) as error:
            self.load()
        self.assertNotIn("fake-private-connection", str(error.exception))
        self.assertFalse(self.calls)


class CloudAccessTests(unittest.TestCase):
    def test_scenario_passwords_are_stable_distinct_and_bound_to_the_vault_seed(self):
        seed = "a" * 40
        self.assertEqual(scenario_password(seed, "escenario01"), scenario_password(seed, "escenario01"))
        self.assertNotEqual(scenario_password(seed, "escenario01"), scenario_password(seed, "escenario02"))
        self.assertNotEqual(scenario_password(seed, "escenario01"), scenario_password("b" * 40, "escenario01"))

    def test_cloud_users_use_catalog_identities_and_separate_operator_access(self):
        document = dict(users=[dict(username="operador", password="a-private-password", role="operator")],
                        scenario_password_seed="a" * 40)
        catalog = dict(profiles=[dict(username="escenario01", customer_id="C01", scenario="selected_reactivation")])
        result = build_access_users(document, catalog)
        self.assertEqual([user["role"] for user in result["users"]], ["operator", "customer"])
        self.assertEqual(result["users"][1]["customer_id"], "C01")
        self.assertEqual(len(document["users"]), 1)
