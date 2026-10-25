from __future__ import annotations

import http.client
import importlib.util
import json
import sys
import threading
import unittest
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

WEBAPP_PATH = (
    PROJECT_ROOT
    / "DeepPrint"
    / "footprints"
    / "cloud_storage"
    / "assets"
    / "webapp.py"
)

TEST_CONFIG = {
    "site_name": "Unit Test Cloud",
    "domain": "cloud.example.test",
    "nextcloud_version": "30.0.4",
    "php_version": "8.3.14",
    "node_version": "v20.11.1",
    "nginx_version": "1.24.0",
    "log_file": "",
}


def load_webapp() -> Any:
    spec = importlib.util.spec_from_file_location(
        "deepprint_cloud_storage_webapp",
        WEBAPP_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class CloudStorageWebDecoyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.webapp = load_webapp()
        cls.server = cls.webapp.build_server(
            "127.0.0.1",
            0,
            config=dict(TEST_CONFIG),
        )
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def request(
        self,
        path: str,
        method: str = "GET",
        body: bytes | None = None,
        headers: dict | None = None,
    ) -> tuple:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    # ------------------------------------------------------------------
    # PHP surface
    # ------------------------------------------------------------------

    def test_root_redirects_to_canonical_login(self) -> None:
        status, headers, _ = self.request("/")

        self.assertEqual(302, status)
        self.assertEqual("/index.php/login", headers.get("Location"))
        self.assertIn("PHP/", headers.get("X-Powered-By", ""))
        self.assertIn("nginx/1.24.0", headers.get("Server", ""))

    def test_login_page_presents_php_nextcloud_surface(self) -> None:
        status, headers, body = self.request("/index.php/login")

        self.assertEqual(200, status)
        self.assertEqual("PHP/8.3.14", headers.get("X-Powered-By"))
        self.assertIn("nginx/1.24.0", headers.get("Server", ""))
        self.assertIn("PHPSESSID=", headers.get("Set-Cookie", ""))
        self.assertIn(b"Unit Test Cloud", body)
        self.assertIn(b'name="user"', body)
        self.assertIn(b'name="password"', body)
        self.assertIn(b"Log in", body)
        self.assertIn(b"/index.php/lostpassword", body)

    def test_failed_login_shows_nextcloud_error_message(self) -> None:
        status, _, body = self.request("/index.php/login?wrongpwd=1")

        self.assertEqual(200, status)
        self.assertIn(b"Wrong username or password.", body)

    def test_login_post_captures_credentials_and_redirects(self) -> None:
        recorded: list = []
        original = self.webapp.log_event

        def spy(event, config, **fields):
            recorded.append((event, fields))

        self.webapp.log_event = spy
        try:
            status, headers, _ = self.request(
                "/index.php/login",
                method="POST",
                body=b"user=admin&password=hunter2",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        finally:
            self.webapp.log_event = original

        self.assertEqual(302, status)
        self.assertIn("wrongpwd", headers.get("Location", ""))
        attempts = [entry for entry in recorded if entry[0] == "login_attempt"]
        self.assertEqual(1, len(attempts))
        self.assertEqual("admin", attempts[0][1]["user"])
        self.assertEqual("hunter2", attempts[0][1]["password"])

    def test_login_post_accepts_json_credentials(self) -> None:
        recorded: list = []
        original = self.webapp.log_event

        def spy(event, config, **fields):
            recorded.append((event, fields))

        self.webapp.log_event = spy
        try:
            status, _, _ = self.request(
                "/index.php/login",
                method="POST",
                body=json.dumps({"user": "svc-backup", "password": "changeme"}).encode(
                    "utf-8"
                ),
                headers={"Content-Type": "application/json"},
            )
        finally:
            self.webapp.log_event = original

        self.assertEqual(302, status)
        attempts = [entry for entry in recorded if entry[0] == "login_attempt"]
        self.assertEqual(1, len(attempts))
        self.assertEqual("svc-backup", attempts[0][1]["user"])
        self.assertEqual("changeme", attempts[0][1]["password"])

    def test_status_php_reports_nextcloud_shape(self) -> None:
        status, headers, body = self.request("/status.php")

        self.assertEqual(200, status)
        self.assertEqual("PHP/8.3.14", headers.get("X-Powered-By"))
        payload = json.loads(body)
        self.assertTrue(payload["installed"])
        self.assertFalse(payload["maintenance"])
        self.assertEqual("30.0.4", payload["version"])
        self.assertEqual("Nextcloud", payload["productname"])

    def test_webdav_requires_basic_auth_and_logs_credentials(self) -> None:
        recorded: list = []
        original = self.webapp.log_event

        def spy(event, config, **fields):
            recorded.append((event, fields))

        self.webapp.log_event = spy
        try:
            import base64

            token = base64.b64encode(b"attacker:stolen").decode("ascii")
            status, headers, body = self.request(
                "/remote.php/dav",
                headers={"Authorization": "Basic " + token},
            )
        finally:
            self.webapp.log_event = original

        self.assertEqual(401, status)
        self.assertIn("Basic", headers.get("WWW-Authenticate", ""))
        self.assertIn(b"Sabre\\DAV\\Exception\\NotAuthenticated", body)
        attempts = [entry for entry in recorded if entry[0] == "basic_auth_attempt"]
        self.assertEqual(1, len(attempts))
        self.assertEqual("attacker", attempts[0][1]["user"])
        self.assertEqual("stolen", attempts[0][1]["password"])

    def test_webdav_options_advertises_dav_compliance(self) -> None:
        status, headers, _ = self.request("/remote.php/dav", method="OPTIONS")

        self.assertEqual(200, status)
        self.assertIn("1, 3", headers.get("DAV", ""))
        self.assertIn("PROPFIND", headers.get("Allow", ""))

    def test_ocs_getapppassword_returns_not_logged_in(self) -> None:
        status, _, body = self.request("/ocs/v2.php/core/getapppassword")

        self.assertEqual(401, status)
        self.assertIn(b"Current user is not logged in", body)

    def test_robots_txt_and_cron_php(self) -> None:
        status, _, body = self.request("/robots.txt")
        self.assertEqual(200, status)
        self.assertIn(b"Disallow: /", body)

        status, headers, body = self.request("/cron.php")
        self.assertEqual(200, status)
        self.assertEqual(b"", body)
        self.assertIn("PHP/", headers.get("X-Powered-By", ""))

    def test_favicon_is_served(self) -> None:
        status, headers, body = self.request("/favicon.ico")

        self.assertEqual(200, status)
        self.assertIn("image/x-icon", headers.get("Content-Type", ""))
        self.assertGreater(len(body), 100)

    def test_unknown_php_route_returns_themed_404(self) -> None:
        status, headers, body = self.request("/index.php/definitely-not-real")

        self.assertEqual(404, status)
        self.assertEqual("PHP/8.3.14", headers.get("X-Powered-By"))
        self.assertIn(b"Page not found", body)

    def test_apps_redirects_to_login(self) -> None:
        status, headers, _ = self.request("/index.php/apps/files/")

        self.assertEqual(302, status)
        self.assertEqual("/index.php/login", headers.get("Location"))

    def test_head_request_returns_headers_only(self) -> None:
        status, headers, body = self.request("/index.php/login", method="HEAD")

        self.assertEqual(200, status)
        self.assertEqual(b"", body)
        self.assertEqual("PHP/8.3.14", headers.get("X-Powered-By"))

    # ------------------------------------------------------------------
    # Node.js surface
    # ------------------------------------------------------------------

    def test_node_status_leaks_node_version(self) -> None:
        status, headers, body = self.request("/api/v1/status")

        self.assertEqual(200, status)
        self.assertEqual("Express", headers.get("X-Powered-By"))
        self.assertIn("W/\"", headers.get("ETag", ""))
        payload = json.loads(body)
        self.assertEqual("v20.11.1", payload["node"])
        self.assertEqual("ok", payload["status"])

    def test_healthz_is_node_flavored(self) -> None:
        status, headers, _ = self.request("/healthz")

        self.assertEqual(200, status)
        self.assertEqual("Express", headers.get("X-Powered-By"))

    def test_socketio_polling_handshake(self) -> None:
        status, headers, body = self.request(
            "/socket.io/?EIO=4&transport=polling"
        )

        self.assertEqual(200, status)
        self.assertEqual("Express", headers.get("X-Powered-By"))
        self.assertTrue(body.startswith(b"0{"))
        self.assertIn(b'"sid"', body)
        self.assertIn(b'"upgrades":["websocket"]', body)

    def test_socketio_rejects_unknown_transport(self) -> None:
        status, _, body = self.request("/socket.io/?EIO=4&transport=websocket")

        self.assertEqual(400, status)
        self.assertIn(b"Transport unknown", body)

    def test_metrics_exposes_nodejs_families(self) -> None:
        status, headers, body = self.request("/metrics")

        self.assertEqual(200, status)
        self.assertIn("text/plain", headers.get("Content-Type", ""))
        self.assertIn(b"nodejs_version_info", body)
        self.assertIn(b"process_resident_memory_bytes", body)
        self.assertIn(b"nodejs_eventloop_lag_seconds", body)
        self.assertIn(b'nodejs_version_info{version="v20.11.1"', body)

    def test_unknown_api_route_uses_express_404(self) -> None:
        status, headers, body = self.request("/api/v2/definitely-not-real")

        self.assertEqual(404, status)
        self.assertEqual("Express", headers.get("X-Powered-By"))
        self.assertIn(b"Cannot GET /api/v2/definitely-not-real", body)

    # ------------------------------------------------------------------
    # Favicon construction sanity
    # ------------------------------------------------------------------

    def test_favicon_builder_produces_ico(self) -> None:
        favicon = self.webapp.build_favicon()

        self.assertEqual(b"\x00\x00", favicon[0:2])
        self.assertEqual(1, int.from_bytes(favicon[2:4], "little"))
        self.assertEqual(1, int.from_bytes(favicon[4:6], "little"))
        self.assertGreater(len(favicon), 1000)


if __name__ == "__main__":
    unittest.main()
