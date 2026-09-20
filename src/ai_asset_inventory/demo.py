"""Self-service EdgeDisco demo: simulated workloads, real discovery."""

from __future__ import annotations

import atexit
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import replace
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, TextIO

from .detector import collect_inventory, _process_rows, _safe_command_fingerprint
from .agent import AgentClient
from .self_service import ensure_local_server
from .models import Asset

# Simulated fixture label -> script filename -> detector asset name.
WORKLOADS: tuple[tuple[str, str, str], ...] = (
    ("CrewAI", "crewai_workload.py", "CrewAI"),
    ("AutoGen", "autogen_workload.py", "AutoGen"),
    ("LangGraph", "langgraph_workload.py", "LangGraph/LangChain"),
    ("MCP server", "mcp-server_workload.py", "MCP Server"),
)

EXPECTED_RUNTIME_NAMES = tuple(item[2] for item in WORKLOADS)

_IDLE_SCRIPT = """\
# SIMULATED TEST WORKLOAD — not real AI software.
# Idle process whose path contains an EdgeDisco signature needle.
import time
while True:
    time.sleep(3600)
"""

_BANNER = """\
             \U0001faa9
      E D G E D I S C O
     EDGE DISCOVERY FOR AI
"""

_FOOTER = """\
             \U0001faa9
            DISCO.
"""


@dataclass(frozen=True)
class DemoResult:
    discovered: tuple[str, ...]
    missing: tuple[str, ...]
    assets: tuple[Asset, ...]

    @property
    def ok(self) -> bool:
        return not self.missing


class DemoLab:
    """Creates temporary simulated workloads and tracks only their PIDs."""

    def __init__(self, python: str | None = None) -> None:
        self._python = python or sys.executable
        self._root: Path | None = None
        self._processes: list[subprocess.Popen[bytes]] = []
        self._started_labels: list[str] = []
        self._cleaned = False

    @property
    def root(self) -> Path | None:
        return self._root

    @property
    def pids(self) -> list[int]:
        return [proc.pid for proc in self._processes if proc.pid is not None]

    @property
    def started_labels(self) -> list[str]:
        return list(self._started_labels)

    def prepare(self) -> Path:
        if self._root is not None:
            return self._root
        self._root = Path(tempfile.mkdtemp(prefix="edgedisco-demo-"))
        marker = self._root / "SIMULATED_TEST_WORKLOADS.txt"
        marker.write_text(
            "SIMULATED TEST WORKLOADS\n"
            "These idle scripts mimic AI runtime signatures for EdgeDisco demo only.\n"
            "They are not CrewAI, AutoGen, LangGraph, or MCP server installations.\n",
            encoding="utf-8",
        )
        for _label, filename, _name in WORKLOADS:
            (self._root / filename).write_text(_IDLE_SCRIPT, encoding="utf-8")
        return self._root

    def start_workloads(self, on_started: Callable[[str], None] | None = None) -> None:
        root = self.prepare()
        creationflags = 0
        if platform.system() == "Windows":
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        for label, filename, _name in WORKLOADS:
            script = root / filename
            proc = subprocess.Popen(
                [self._python, str(script)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                start_new_session=(platform.system() != "Windows"),
                creationflags=creationflags,
            )
            self._processes.append(proc)
            self._started_labels.append(label)
            if on_started:
                on_started(label)

    def cleanup(self) -> None:
        if self._cleaned:
            return
        self._cleaned = True
        for proc in self._processes:
            _terminate_owned(proc)
        self._processes.clear()
        if self._root is not None and self._root.exists():
            shutil.rmtree(self._root, ignore_errors=True)
            self._root = None

    def __enter__(self) -> "DemoLab":
        return self

    def __exit__(self, *exc: object) -> None:
        self.cleanup()


def _terminate_owned(proc: subprocess.Popen[bytes]) -> None:
    """Terminate only a process started by this demo run."""
    if proc.poll() is not None:
        return
    try:
        if platform.system() == "Windows":
            proc.terminate()
        else:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            if platform.system() == "Windows":
                proc.kill()
            else:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    proc.kill()
            proc.wait(timeout=3)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def discovered_runtime_names(assets: list[Asset]) -> set[str]:
    """Names of agent runtimes / processes from a real inventory scan."""
    names: set[str] = set()
    for asset in assets:
        if asset.kind in {"agent_runtime", "process"} and asset.name in EXPECTED_RUNTIME_NAMES:
            names.add(asset.name)
    return names


def evaluate_discovery(assets: list[Asset]) -> DemoResult:
    found = discovered_runtime_names(assets)
    discovered = tuple(name for name in EXPECTED_RUNTIME_NAMES if name in found)
    missing = tuple(name for name in EXPECTED_RUNTIME_NAMES if name not in found)
    return DemoResult(discovered=discovered, missing=missing, assets=tuple(assets))


def fixture_assets(lab: DemoLab, assets: list[Asset]) -> list[Asset]:
    """Match detector evidence to our child PIDs without retaining command text."""
    hashes = {
        _safe_command_fingerprint(row.args)
        for row in _process_rows() if row.pid in lab.pids
    }
    return [asset for asset in assets
            if asset.kind == "agent_runtime" and asset.command_hash in hashes
            and asset.name in EXPECTED_RUNTIME_NAMES]


def demo_evidence(assets: list[Asset]) -> list[Asset]:
    """Persist historical metadata after fixtures exit; keep detector provenance."""
    return [replace(asset, running=False, metadata={
        **asset.metadata,
        "demo_lab": True,
        "evidence_label": "SIMULATED TEST WORKLOADS",
        "discovery_source": "EdgeDisco process detector",
        "observed_running": True,
    }) for asset in assets]


def wait_for_discovery(
    lab: DemoLab,
    *,
    timeout: float = 15.0,
    poll_interval: float = 0.25,
    inventory_fn: Callable[[], list[Asset]] | None = None,
) -> DemoResult:
    """Poll real collect_inventory until expected fixtures appear or timeout."""
    scan = inventory_fn or collect_inventory
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if lab._processes and any(proc.poll() is not None for proc in lab._processes):
            break
        all_assets = scan()
        matched = evaluate_discovery(fixture_assets(lab, all_assets))
        result = replace(matched, assets=tuple(all_assets))
        if result.ok:
            return result
        time.sleep(poll_interval)
    all_assets = scan()
    return replace(evaluate_discovery(fixture_assets(lab, all_assets)), assets=tuple(all_assets))


def report_assets(lab: DemoLab, assets: list[Asset]) -> list[Asset]:
    """Send a complete scan so demo reporting preserves other live asset states."""
    matched = fixture_assets(lab, assets)
    hashes = {asset.command_hash for asset in matched}
    fingerprints = {asset.fingerprint for asset in matched}
    return [demo_evidence([asset])[0] if asset.fingerprint in fingerprints else asset
            for asset in assets
            if not (asset.kind == "process" and asset.command_hash in hashes)]


def privacy_check_results(assets: list[Asset]) -> list[tuple[str, bool]]:
    """Return (label, ok) privacy expectations for demo-relevant inventory assets."""
    relevant = [
        asset
        for asset in assets
        if asset.name in EXPECTED_RUNTIME_NAMES
        or asset.kind in {"agent_runtime", "process", "mcp_server", "application"}
    ]
    # Prefer demo families when present; otherwise inspect the full inventory shape.
    demo_assets = [asset for asset in relevant if asset.name in EXPECTED_RUNTIME_NAMES]
    check_assets = demo_assets or relevant

    blobs: list[str] = []
    for asset in check_assets:
        blobs.append(asset.name)
        blobs.append(asset.vendor)
        blobs.append(asset.kind)
        for key, value in asset.metadata.items():
            blobs.append(str(key))
            blobs.append(str(value))
        if asset.path_hash:
            blobs.append(asset.path_hash)
        if asset.command_hash:
            blobs.append(asset.command_hash)
    serialized = " ".join(blobs).lower()
    return [
        ("No prompts collected", "prompt" not in serialized and "user_message" not in serialized),
        ("No responses collected", "response" not in serialized and "completion" not in serialized),
        (
            "No credentials collected",
            "api_key" not in serialized
            and "password" not in serialized
            and "authorization" not in serialized,
        ),
        (
            "No raw command lines stored",
            all(
                (asset.command_hash is None or len(asset.command_hash) == 64)
                and (asset.path_hash is None or len(asset.path_hash) == 64)
                for asset in check_assets
            ),
        ),
    ]


def _print(stream: TextIO, text: str = "") -> None:
    print(text, file=stream)
    stream.flush()


def render_discovery_and_privacy(result: DemoResult, stream: TextIO) -> None:
    for name in EXPECTED_RUNTIME_NAMES:
        if name in result.discovered:
            _print(stream, f"\u2713 {name}")
        else:
            _print(stream, f"\u2717 {name} (not discovered)")
    _print(stream)
    _print(stream, "PRIVACY CHECK")
    _print(stream)
    for label, ok in privacy_check_results(list(result.assets)):
        mark = "\u2713" if ok else "\u2717"
        suffix = "" if ok else " (privacy expectation failed)"
        _print(stream, f"{mark} {label}{suffix}")
    _print(stream)
    if result.ok:
        _print(stream, f"{len(result.discovered)} AI runtime families discovered.")
        _print(stream)
        _print(stream, _FOOTER)
    else:
        missing = ", ".join(result.missing)
        _print(stream, f"Demo incomplete: missing {missing}.")
        _print(
            stream,
            "Discovery uses the real EdgeDisco detector; "
            "fixtures must be visible to process inventory.",
        )


def run_demo(
    *,
    stream: TextIO | None = None,
    inventory_fn: Callable[[], list[Asset]] | None = None,
    timeout: float = 15.0,
    lab: DemoLab | None = None,
    server_fn=None,
    client_fn=None,
    browser_fn=None,
) -> int:
    """Run the interactive demo. Returns process exit code."""
    out = stream or sys.stdout
    system = platform.system()
    if system not in {"Darwin", "Linux", "Windows"}:
        _print(out, f"edgedisco demo is not supported on {system}.")
        return 2

    lab = lab or DemoLab()
    previous_handlers: dict[int, object] = {}

    def _cleanup_and_reraise(signum: int, _frame: object) -> None:
        lab.cleanup()
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    def _atexit_cleanup() -> None:
        lab.cleanup()

    try:
        atexit.register(_atexit_cleanup)
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                previous_handlers[sig] = signal.signal(sig, _cleanup_and_reraise)
            except (ValueError, OSError):
                pass

        _print(out)
        _print(out, _BANNER)
        _print(out, "Preparing EdgeDisco Lab...")
        _print(out)
        _print(out, "SIMULATED TEST WORKLOADS")
        _print(out, "These are lightweight local fixtures, not installed AI frameworks.")
        _print(out)

        lab.prepare()
        lab.start_workloads(
            on_started=lambda label: _print(out, f"\u2713 {label} workload started"),
        )
        _print(out)
        _print(out, "Discovering this endpoint...")
        _print(out)

        # Brief settle so process inventory can observe children.
        time.sleep(0.35)
        result = wait_for_discovery(lab, timeout=timeout, inventory_fn=inventory_fn)
        render_discovery_and_privacy(result, out)
        if not result.ok or not all(ok for _, ok in privacy_check_results(list(result.assets))):
            return 1
        server = (server_fn or ensure_local_server)()
        client = (client_fn or AgentClient)(server.agent_config)
        evidence = report_assets(lab, list(result.assets))
        report = client.submit_assets(evidence)
        if report.get("status") != "accepted" or report.get("asset_count") != len(evidence):
            raise RuntimeError("server did not accept all demo evidence")
        _print(out, f"Demo evidence saved to EdgeDisco: {server.dashboard}")
        browser_url = server.dashboard
        if getattr(server, "admin_token", None):
            try:
                parsed = urllib.parse.urlsplit(server.dashboard)
                if parsed.hostname != "127.0.0.1" or parsed.scheme != "http":
                    raise ValueError("browser bootstrap requires the local dashboard")
                request = urllib.request.Request(
                    server.dashboard + "/api/v1/browser-bootstrap",
                    data=b"",
                    headers={"Authorization": f"Bearer {server.admin_token}"},
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=5) as response:
                    bootstrap = json.load(response)
                path = bootstrap["bootstrap_path"]
                if not path.startswith("/browser-bootstrap/") or "/" in path[len("/browser-bootstrap/"):]:
                    raise ValueError("invalid browser bootstrap response")
                browser_url = server.dashboard + path
            except Exception:
                _print(out, f"Automatic browser sign-in unavailable. Visit {server.dashboard} and use manual admin-token sign-in if prompted.")
        try:
            opened = (browser_fn or webbrowser.open)(browser_url)
            if not opened:
                _print(out, f"Browser did not open. Visit {server.dashboard}; manual admin-token sign-in is available.")
        except Exception as exc:  # Browser is optional after successful persistence.
            _print(out, f"Browser did not open ({exc}). Visit {server.dashboard}; manual admin-token sign-in is available.")
        return 0
    except Exception as exc:  # noqa: BLE001 — demo must always clean up
        _print(out, f"Demo failed: {exc}")
        return 1
    finally:
        lab.cleanup()
        try:
            atexit.unregister(_atexit_cleanup)
        except Exception:
            pass
        for sig, handler in previous_handlers.items():
            try:
                signal.signal(sig, handler)  # type: ignore[arg-type]
            except (ValueError, OSError):
                pass
