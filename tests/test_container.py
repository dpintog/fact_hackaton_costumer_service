"""Container ingress preserves the local server's origin and authentication guards."""

import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from campaigns.server import make_server
from campaigns.service import ChatService
from test_service import FixtureStore, FixedModel


class ContainerIngressTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.service = ChatService(FixtureStore(), FixedModel(), Path(self.temporary.name) / "state.sqlite")
        self.service.register_demo_user("tester", "a-long-test-password", "CUS-OWN")
        self.origin = "https://app.azurecontainerapps.io"
        self.server = make_server(self.service, ROOT / "web", host="0.0.0.0", port=0, public_origin=self.origin)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.addCleanup(self.close_server)

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def request(self, path, *, host="app.azurecontainerapps.io", origin=None, body=None):
        headers = {"Host": host}
        if origin:
            headers["Origin"] = origin
        payload = None
        if body is not None:
            payload = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        request = Request(self.url + path, payload, headers)
        try:
            response = urlopen(request)
        except HTTPError as error:
            response = error
        with response:
            return response.status, response.read()

    def test_health_probe_works_with_internal_host_and_reveals_no_private_state(self):
        status, body = self.request("/healthz", host="10.0.0.1:8002")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"status": "ok"})

    def test_proxy_host_and_https_origin_allow_ui_and_login(self):
        self.assertEqual(self.request("/")[0], 200)
        status, body = self.request("/api/login", origin=self.origin,
                                    body={"username": "tester", "password": "a-long-test-password"})
        self.assertEqual(status, 200)
        self.assertIn("token", json.loads(body))

    def test_untrusted_host_and_cross_origin_are_rejected(self):
        self.assertEqual(self.request("/api/info", host="attacker.example")[0], 403)
        self.assertEqual(self.request("/api/info", origin="https://attacker.example")[0], 403)
        self.assertEqual(self.request("/api/login", origin="http://app.azurecontainerapps.io", body={})[0], 403)

    def test_external_listener_requires_explicit_valid_https_origin(self):
        for origin in (None, "http://app.example", "https://user:password@app.example", "https://app.example/path"):
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                make_server(self.service, ROOT / "web", host="0.0.0.0", port=0, public_origin=origin)


if __name__ == "__main__":
    unittest.main()
