"""Tests for the EdgeDisco self-service demo."""

from __future__ import annotations

import io
import os
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from ai_asset_inventory import detector
from ai_asset_inventory.demo import (
    EXPECTED_RUNTIME_NAMES,
    DemoLab,
    evaluate_discovery,
    privacy_check_results,
    run_demo,
    wait_for_discovery,
    demo_evidence,
)
from ai_asset_inventory.models import Asset


def _asset(name: str, kind: str = "agent_runtime") -> Asset:
    return Asset(
        fingerprint=f"fp-{name}-{kind}",
        kind=kind,
        name=name,
        vendor="Test",
        running=True,
        command_hash="a" * 64,
        path_hash="b" * 64,
        metadata={"host_app": "Direct/local", "runtime": "python3"},
    )


class DemoUnitTests(unittest.TestCase):
    def test_evaluate_discovery_success(self):
        assets = [_asset(name) for name in EXPECTED_RUNTIME_NAMES]
        result = evaluate_discovery(assets)
        self.assertTrue(result.ok)
        self.assertEqual(result.discovered, EXPECTED_RUNTIME_NAMES)
        self.assertEqual(result.missing, ())

    def test_evaluate_discovery_failure_is_honest(self):
        assets = [_asset("CrewAI"), _asset("AutoGen")]
        result = evaluate_discovery(assets)
        self.assertFalse(result.ok)
        self.assertIn("LangGraph/LangChain", result.missing)
        self.assertIn("MCP Server", result.missing)

    def test_run_demo_failure_exits_nonzero(self):
        stream = io.StringIO()

        def empty_inventory():
            return []

        code = run_demo(stream=stream, inventory_fn=empty_inventory, timeout=0.4)
        self.assertEqual(code, 1)
        text = stream.getvalue()
        self.assertIn("SIMULATED TEST WORKLOADS", text)
        self.assertIn("not discovered", text)
        self.assertIn("Demo incomplete", text)

    def test_run_demo_success_with_injected_inventory(self):
        stream = io.StringIO()
        assets = [_asset(name) for name in EXPECTED_RUNTIME_NAMES]
        from types import SimpleNamespace
        server = SimpleNamespace(agent_config="config", dashboard="http://127.0.0.1:8080")
        class Client:
            def __init__(self, path): self.path = path
            def submit_assets(self, evidence):
                self.evidence = evidence
                return {"status": "accepted", "asset_count": len(evidence)}
        with patch("ai_asset_inventory.demo.fixture_assets", side_effect=lambda lab, found: found):
            code = run_demo(stream=stream, inventory_fn=lambda: assets, timeout=2.0,
                            server_fn=lambda: server, client_fn=Client, browser_fn=lambda url: True)
        self.assertEqual(code, 0)
        text = stream.getvalue()
        self.assertIn("CrewAI", text)
        self.assertIn("AutoGen", text)
        self.assertIn("LangGraph/LangChain", text)
        self.assertIn("MCP Server", text)
        self.assertIn("4 AI runtime families discovered", text)
        self.assertIn("No prompts collected", text)
        self.assertIn("DISCO.", text)

    def test_simulated_evidence_is_historical_and_real_inventory_unchanged(self):
        real = _asset("CrewAI")
        demo = demo_evidence([real])[0]
        self.assertTrue(real.running)
        self.assertNotIn("demo_lab", real.metadata)
        self.assertFalse(demo.running)
        self.assertTrue(demo.metadata["demo_lab"])
        self.assertEqual(demo.metadata["evidence_label"], "SIMULATED TEST WORKLOADS")

    def test_browser_after_persistence_and_failure_is_graceful(self):
        assets = [_asset(name) for name in EXPECTED_RUNTIME_NAMES]
        events = []
        from types import SimpleNamespace
        server = SimpleNamespace(agent_config="config", dashboard="http://127.0.0.1:8080")
        class Client:
            def __init__(self, path): pass
            def submit_assets(self, evidence):
                events.append("persist")
                return {"status": "accepted", "asset_count": len(evidence)}
        def browser(url):
            events.append("browser")
            raise RuntimeError("unavailable")
        out = io.StringIO()
        with patch("ai_asset_inventory.demo.fixture_assets", side_effect=lambda lab, found: found):
            code = run_demo(stream=out, inventory_fn=lambda: assets, timeout=2,
                            server_fn=lambda: server, client_fn=Client, browser_fn=browser)
        self.assertEqual(code, 0, out.getvalue())
        self.assertEqual(events, ["persist", "browser"])
        self.assertIn("Visit http://127.0.0.1:8080", out.getvalue())

    def test_demo_opens_authenticated_browser_flow_after_reporting(self):
        assets = [_asset(name) for name in EXPECTED_RUNTIME_NAMES]
        events = []
        from types import SimpleNamespace
        server = SimpleNamespace(agent_config="config", dashboard="http://127.0.0.1:8080",
                                 admin_token="secret-admin-token")
        class Client:
            def __init__(self, path): pass
            def submit_assets(self, evidence):
                events.append("persist")
                return {"status": "accepted", "asset_count": len(evidence)}
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return b'{"bootstrap_path":"/browser-bootstrap/one-time-code"}'
        def issue(request, timeout):
            events.append("issue")
            self.assertEqual(request.get_header("Authorization"), "Bearer secret-admin-token")
            return Response()
        def browser(url):
            events.append("browser")
            self.assertEqual(url, "http://127.0.0.1:8080/browser-bootstrap/one-time-code")
            self.assertNotIn("secret-admin-token", url)
            return True
        out = io.StringIO()
        with patch("ai_asset_inventory.demo.fixture_assets", side_effect=lambda lab, found: found), \
             patch("ai_asset_inventory.demo.urllib.request.urlopen", side_effect=issue):
            code = run_demo(stream=out, inventory_fn=lambda: assets, timeout=2,
                            server_fn=lambda: server, client_fn=Client, browser_fn=browser)
        self.assertEqual(code, 0, out.getvalue())
        self.assertEqual(events, ["persist", "issue", "browser"])
        self.assertNotIn("secret-admin-token", out.getvalue())
        self.assertNotIn("manual admin-token", out.getvalue())

    def test_no_browser_when_report_rejected(self):
        assets = [_asset(name) for name in EXPECTED_RUNTIME_NAMES]
        from types import SimpleNamespace
        server = SimpleNamespace(agent_config="config", dashboard="http://127.0.0.1:8080")
        class Client:
            def __init__(self, path): pass
            def submit_assets(self, evidence): return {"status": "error"}
        browser = unittest.mock.Mock()
        with patch("ai_asset_inventory.demo.fixture_assets", side_effect=lambda lab, found: found):
            code = run_demo(stream=io.StringIO(), inventory_fn=lambda: assets, timeout=2,
                            server_fn=lambda: server, client_fn=Client, browser_fn=browser)
        self.assertEqual(code, 1)
        browser.assert_not_called()

    def test_privacy_expectations(self):
        assets = [_asset(name) for name in EXPECTED_RUNTIME_NAMES]
        checks = dict(privacy_check_results(assets))
        self.assertTrue(all(checks.values()))
        self.assertIn("No raw command lines stored", checks)

    def test_privacy_fails_if_raw_command_present(self):
        bad = Asset(
            fingerprint="bad",
            kind="process",
            name="CrewAI",
            vendor="CrewAI",
            running=True,
            command_hash="python3 /tmp/crewai --prompt hello",
            metadata={},
        )
        checks = dict(privacy_check_results([bad]))
        self.assertFalse(checks["No raw command lines stored"])


class DemoLabLifecycleTests(unittest.TestCase):
    def test_cleanup_removes_processes_and_tempdir(self):
        lab = DemoLab()
        lab.prepare()
        root = lab.root
        assert root is not None
        self.assertTrue(root.exists())
        lab.start_workloads()
        pids = list(lab.pids)
        self.assertEqual(len(pids), 4)
        for pid in pids:
            self.assertIsNone(_pid_status(pid))
        lab.cleanup()
        self.assertFalse(root.exists())
        for pid in pids:
            self.assertFalse(_pid_alive(pid), f"pid {pid} still alive after cleanup")
        # Second cleanup is safe.
        lab.cleanup()

    def test_rerun_safety(self):
        for _ in range(2):
            lab = DemoLab()
            lab.start_workloads()
            pids = list(lab.pids)
            self.assertEqual(len(pids), 4)
            lab.cleanup()
            for pid in pids:
                self.assertFalse(_pid_alive(pid))

    def test_only_owned_pids_are_tracked(self):
        outsider = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            lab = DemoLab()
            lab.start_workloads()
            self.assertNotIn(outsider.pid, lab.pids)
            lab.cleanup()
            self.assertTrue(_pid_alive(outsider.pid))
        finally:
            outsider.terminate()
            outsider.wait(timeout=5)


class DemoDiscoveryIntegrationTests(unittest.TestCase):
    def test_real_detector_discovers_fixtures(self):
        lab = DemoLab()
        try:
            lab.start_workloads()
            time.sleep(0.4)
            result = wait_for_discovery(lab, timeout=10.0, inventory_fn=detector.collect_inventory)
            self.assertTrue(
                result.ok,
                f"missing={result.missing}; discovered={result.discovered}",
            )
            for name in EXPECTED_RUNTIME_NAMES:
                self.assertIn(name, result.discovered)
            # Results must come from inventory assets, not hardcoded success flags.
            names = {asset.name for asset in result.assets}
            for name in EXPECTED_RUNTIME_NAMES:
                self.assertIn(name, names)
            for label, ok in privacy_check_results(list(result.assets)):
                self.assertTrue(ok, label)
        finally:
            lab.cleanup()

    def test_full_run_demo_uses_real_collect_inventory(self):
        stream = io.StringIO()
        from types import SimpleNamespace
        server = SimpleNamespace(agent_config="config", dashboard="http://127.0.0.1:8080")
        class Client:
            def __init__(self, path): pass
            def submit_assets(self, evidence):
                return {"status": "accepted", "asset_count": len(evidence)}
        with patch("ai_asset_inventory.demo.collect_inventory", wraps=detector.collect_inventory) as wrapped:
            code = run_demo(stream=stream, timeout=12.0, server_fn=lambda: server,
                            client_fn=Client, browser_fn=lambda url: True)
            self.assertGreaterEqual(wrapped.call_count, 1)
        self.assertEqual(code, 0, stream.getvalue())
        text = stream.getvalue()
        self.assertIn("SIMULATED TEST WORKLOADS", text)
        self.assertIn("4 AI runtime families discovered", text)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _pid_status(pid: int) -> None:
    """Assert the process exists (raises if not)."""
    os.kill(pid, 0)


if __name__ == "__main__":
    unittest.main()
