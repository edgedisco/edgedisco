import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import http.cookiejar
import hashlib
import time
from pathlib import Path

from ai_asset_inventory.database import Database
from ai_asset_inventory.server import InventoryServer
from ai_asset_inventory.agent import AgentClient
from ai_asset_inventory.runtime import RuntimeClient


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.server = InventoryServer(("127.0.0.1", 0), Database(Path(self.temp.name) / "db.sqlite"), "admin", "enroll")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.temp.cleanup()

    def post(self, path, token, payload):
        request = urllib.request.Request(self.base + path, json.dumps(payload).encode(), method="POST",
                                         headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        with urllib.request.urlopen(request) as response:
            return response.status, json.loads(response.read())

    def test_full_enroll_report_summary_flow(self):
        status, enrolled = self.post("/api/v1/enroll", "enroll", {"hostname": "host", "os": "Linux"})
        self.assertEqual(status, 201)
        report = {"scan_id": "unique", "observed_at": "2026-09-19T00:00:00+00:00", "device": {},
                  "assets": [], "privacy": {"content_captured": False}}
        status, accepted = self.post("/api/v1/reports", enrolled["device_token"], report)
        self.assertEqual(status, 202)
        request = urllib.request.Request(self.base + "/api/v1/summary", headers={"Authorization": "Bearer admin"})
        with urllib.request.urlopen(request) as response:
            summary = json.loads(response.read())
        self.assertEqual(summary["devices"], 1)

    def test_summary_requires_admin(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(self.base + "/api/v1/summary")
        self.assertEqual(caught.exception.code, 401)

    def test_local_browser_bootstrap_is_single_use_and_keeps_admin_token_private(self):
        self.server.admin_token = "test-secret-admin-token-never-in-browser"
        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        issue = urllib.request.Request(
            self.base + "/api/v1/browser-bootstrap", data=b"", method="POST",
            headers={"Authorization": f"Bearer {self.server.admin_token}"},
        )
        with opener.open(issue) as response:
            path = json.load(response)["bootstrap_path"]
        self.assertTrue(path.startswith("/browser-bootstrap/"))
        self.assertNotIn(self.server.admin_token, path)
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, request, fp, code, msg, headers, newurl):
                return None
        bootstrap_client = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(jar), NoRedirect(),
        )
        with self.assertRaises(urllib.error.HTTPError) as redirect:
            bootstrap_client.open(self.base + path)
        self.assertEqual(redirect.exception.code, 303)
        self.assertEqual(redirect.exception.headers["Location"], "/")
        self.assertNotIn(self.server.admin_token, redirect.exception.headers["Location"])
        self.assertEqual(redirect.exception.headers["Referrer-Policy"], "no-referrer")
        with opener.open(self.base + "/") as response:
            html = response.read().decode()
        self.assertNotIn(self.server.admin_token, html)
        self.assertTrue(any(cookie.name == "aai_session" and cookie.has_nonstandard_attr("HttpOnly") for cookie in jar))
        with opener.open(self.base + "/api/v1/summary") as response:
            self.assertEqual(response.status, 200)
        with self.assertRaises(urllib.error.HTTPError) as reused:
            opener.open(self.base + path)
        self.assertEqual(reused.exception.code, 401)
        with self.assertRaises(urllib.error.HTTPError) as invalid:
            opener.open(self.base + "/browser-bootstrap/invalid")
        self.assertEqual(invalid.exception.code, 401)

    def test_browser_bootstrap_requires_bearer_and_local_host_and_expires(self):
        with self.assertRaises(urllib.error.HTTPError) as unauthenticated:
            urllib.request.urlopen(urllib.request.Request(self.base + "/api/v1/browser-bootstrap", data=b""))
        self.assertEqual(unauthenticated.exception.code, 401)
        with self.assertRaises(urllib.error.HTTPError) as remote_host:
            urllib.request.urlopen(urllib.request.Request(
                self.base + "/api/v1/browser-bootstrap", data=b"",
                headers={"Authorization": "Bearer admin", "Host": "example.com"},
            ))
        self.assertEqual(remote_host.exception.code, 403)
        request = urllib.request.Request(self.base + "/api/v1/browser-bootstrap", data=b"",
                                         headers={"Authorization": "Bearer admin"})
        with urllib.request.urlopen(request) as response:
            path = json.load(response)["bootstrap_path"]
        digest = hashlib.sha256(path.rsplit("/", 1)[1].encode()).hexdigest()
        with self.server.browser_bootstrap_lock:
            self.server.browser_bootstraps[digest] = time.monotonic() - 1
        with self.assertRaises(urllib.error.HTTPError) as expired:
            urllib.request.urlopen(self.base + path)
        self.assertEqual(expired.exception.code, 401)
        with self.assertRaises(urllib.error.HTTPError) as remote_redeem:
            urllib.request.urlopen(urllib.request.Request(self.base + path, headers={"Host": "example.com"}))
        self.assertEqual(remote_redeem.exception.code, 403)
        with self.assertRaises(urllib.error.HTTPError) as still_protected:
            urllib.request.urlopen(self.base + "/api/v1/summary")
        self.assertEqual(still_protected.exception.code, 401)

    def test_runtime_event_upload(self):
        _, enrolled = self.post("/api/v1/enroll", "enroll", {"hostname": "host", "os": "Linux"})
        event = {
            "event_id": "event-1", "observed_at": "2026-09-19T00:00:00+00:00",
            "app": "claude-code", "event_type": "SessionStart", "session_hash": "a" * 64,
            "agent_hash": "root", "agent_type": None, "tool_name": None,
            "mcp_server": None, "model": "claude", "status": "active",
            "duration_ms": None, "workspace_hash": None, "user_hash": None,
            "metadata": {"source_event": "SessionStart"},
        }
        status, accepted = self.post("/api/v1/runtime-events", enrolled["device_token"], {"events": [event]})
        self.assertEqual(status, 202)
        self.assertEqual(accepted["runtime_event_count"], 1)

    def test_endpoint_spool_to_server_flow(self):
        config = Path(self.temp.name) / "agent.json"
        config.write_text(json.dumps({
            "server_url": self.base,
            "enrollment_token": "enroll",
            "runtime_spool": str(Path(self.temp.name) / "runtime.jsonl"),
        }))
        client = AgentClient(config)
        client.enroll()
        RuntimeClient(config).emit_hook(
            "cursor", "sessionStart", {"conversation_id": "runtime-session", "model": "test"}
        )
        result = AgentClient(config).send_once()
        self.assertEqual(result["runtime_event_count"], 1)
        request = urllib.request.Request(
            self.base + "/api/v1/summary", headers={"Authorization": "Bearer admin"}
        )
        with urllib.request.urlopen(request) as response:
            summary = json.loads(response.read())
        self.assertEqual(summary["active_sessions"], 1)


if __name__ == "__main__":
    unittest.main()
