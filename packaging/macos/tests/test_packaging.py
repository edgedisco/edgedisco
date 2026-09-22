from __future__ import annotations

import hashlib
import os
import plistlib
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
MACOS = REPO / "packaging" / "macos"
DAEMON_PLIST = MACOS / "launchd" / "com.edgedisco.daemon.plist"
AGENT_PLIST = MACOS / "launchd" / "com.edgedisco.agent.plist"
BUILD = MACOS / "build-pkg.sh"
RELEASE = MACOS / "release.sh"
PREINSTALL = MACOS / "scripts" / "preinstall"
POSTINSTALL = MACOS / "scripts" / "postinstall"
BINARY_PATH = "/usr/local/libexec/edgedisco/edgedisco"
DATA_ROOT = "/Library/Application Support/EdgeDisco"


def run(*args: str | Path, env: dict[str, str] | None = None, check: bool = True):
    merged = os.environ.copy()
    if env:
        merged.update(env)
    return subprocess.run(
        [str(arg) for arg in args],
        cwd=REPO,
        env=merged,
        check=check,
        text=True,
        capture_output=True,
    )


def load_plist(path: Path) -> dict:
    with path.open("rb") as stream:
        return plistlib.load(stream)


def test_launchd_plists_are_valid_and_separate_privileges():
    for path in (DAEMON_PLIST, AGENT_PLIST):
        lint = run("plutil", "-lint", path)
        assert "OK" in lint.stdout

    daemon = load_plist(DAEMON_PLIST)
    agent = load_plist(AGENT_PLIST)

    assert daemon["Label"] == "com.edgedisco.daemon"
    assert daemon["ProgramArguments"] == [
        BINARY_PATH,
        "daemon",
        "--interval",
        "60",
        "--db",
        f"{DATA_ROOT}/data/inventory.db",
        "--ipc-mode",
        "system",
        "--ipc-socket",
        "/var/run/edgedisco.sock",
        "--ipc-allowed-uid",
        "0",
        "--ipc-owner-uid",
        "0",
        "--ipc-group-gid",
        "0",
    ]
    assert daemon["RunAtLoad"] is True
    assert daemon["KeepAlive"] is True
    assert daemon["WorkingDirectory"] == DATA_ROOT
    assert daemon["StandardOutPath"] == f"{DATA_ROOT}/logs/daemon.log"
    assert daemon["StandardErrorPath"] == f"{DATA_ROOT}/logs/daemon.error.log"

    assert agent["Label"] == "com.edgedisco.agent"
    assert agent["ProgramArguments"] == [BINARY_PATH, "scan"]
    assert agent["RunAtLoad"] is True
    assert agent["StartInterval"] == 60
    assert "KeepAlive" not in agent
    assert agent["WorkingDirectory"] == "/"
    assert agent["StandardOutPath"] == "/dev/null"
    assert agent["StandardErrorPath"] == "/dev/null"
    joined_agent_args = " ".join(agent["ProgramArguments"])
    assert DATA_ROOT not in joined_agent_args
    assert "/var/run/edgedisco.sock" not in joined_agent_args
    assert "--ipc-mode" not in agent["ProgramArguments"]


def test_staged_scripts_are_repeatable_preserve_state_and_record_ownership(
    tmp_path: Path,
):
    root = tmp_path / "root with spaces"
    data = root / DATA_ROOT.lstrip("/") / "data"
    data.mkdir(parents=True)
    binary = root / BINARY_PATH.lstrip("/")
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"native-binary-fixture")
    binary.chmod(0o700)
    daemon_target = root / "Library/LaunchDaemons/com.edgedisco.daemon.plist"
    agent_target = root / "Library/LaunchAgents/com.edgedisco.agent.plist"
    daemon_target.parent.mkdir(parents=True)
    agent_target.parent.mkdir(parents=True)
    shutil.copyfile(DAEMON_PLIST, daemon_target)
    shutil.copyfile(AGENT_PLIST, agent_target)
    inventory = data / "inventory.db"
    outbox = data / "outbox.db"
    inventory.write_bytes(b"inventory-state")
    outbox.write_bytes(b"outbox-state")
    before = (
        hashlib.sha256(inventory.read_bytes()).digest(),
        hashlib.sha256(outbox.read_bytes()).digest(),
    )
    ownership_log = tmp_path / "ownership intent.log"
    lifecycle_log = tmp_path / "lifecycle.log"
    env = {
        "EDGEDISCO_STAGED_ROOT": str(root),
        "EDGEDISCO_OWNERSHIP_LOG": str(ownership_log),
        "EDGEDISCO_LIFECYCLE_LOG": str(lifecycle_log),
    }

    for _ in range(2):
        run(PREINSTALL, "unused", "/", "unused", env=env)
        run(POSTINSTALL, "unused", "/", "unused", env=env)

    after = (
        hashlib.sha256(inventory.read_bytes()).digest(),
        hashlib.sha256(outbox.read_bytes()).digest(),
    )
    assert after == before
    support = root / DATA_ROOT.lstrip("/")
    assert stat.S_IMODE(support.stat().st_mode) == 0o750
    assert stat.S_IMODE((support / "config").stat().st_mode) == 0o750
    assert stat.S_IMODE((support / "logs").stat().st_mode) == 0o750
    assert stat.S_IMODE((support / "data").stat().st_mode) == 0o700
    assert stat.S_IMODE((root / BINARY_PATH.lstrip("/")).stat().st_mode) == 0o755
    assert (
        stat.S_IMODE(
            (root / "Library/LaunchDaemons/com.edgedisco.daemon.plist").stat().st_mode
        )
        == 0o644
    )
    assert (
        stat.S_IMODE(
            (root / "Library/LaunchAgents/com.edgedisco.agent.plist").stat().st_mode
        )
        == 0o644
    )
    assert "root:wheel" in ownership_log.read_text()
    lifecycle = lifecycle_log.read_text()
    assert "system/com.edgedisco.daemon" in lifecycle
    assert "com.edgedisco.agent" in lifecycle
    assert "killall" not in lifecycle
    assert "pkill" not in lifecycle


def test_staged_scripts_reject_relative_root(tmp_path: Path):
    result = run(
        POSTINSTALL,
        env={"EDGEDISCO_STAGED_ROOT": "relative/path"},
        check=False,
    )
    assert result.returncode != 0
    assert "absolute" in result.stderr.lower()


def test_staged_scripts_reject_symlink_escape(tmp_path: Path):
    real_root = tmp_path / "real-root"
    real_root.mkdir()
    linked_root = tmp_path / "linked-root"
    linked_root.symlink_to(real_root, target_is_directory=True)
    result = run(
        POSTINSTALL,
        env={"EDGEDISCO_STAGED_ROOT": str(linked_root)},
        check=False,
    )
    assert result.returncode != 0
    assert "canonical" in result.stderr.lower() or "symlink" in result.stderr.lower()

    staged_root = tmp_path / "staged-root"
    support_parent = staged_root / "Library/Application Support"
    support_parent.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (support_parent / "EdgeDisco").symlink_to(outside, target_is_directory=True)
    env = {"EDGEDISCO_STAGED_ROOT": str(staged_root)}
    for script in (PREINSTALL, POSTINSTALL):
        nested = run(script, env=env, check=False)
        assert nested.returncode != 0
        assert "symlink" in nested.stderr.lower()
    assert list(outside.iterdir()) == []

    ancestor_root = tmp_path / "ancestor-root"
    ancestor_root.mkdir()
    (ancestor_root / "Library").symlink_to(outside, target_is_directory=True)
    ancestor_env = {"EDGEDISCO_STAGED_ROOT": str(ancestor_root)}
    for script in (PREINSTALL, POSTINSTALL):
        ancestor = run(script, env=ancestor_env, check=False)
        assert ancestor.returncode != 0
        assert "symlink" in ancestor.stderr.lower()
    assert list(outside.iterdir()) == []


def test_build_rejects_malformed_version_before_creating_output(tmp_path: Path):
    result = run(BUILD, "1.0;bad", "--output-dir", tmp_path, check=False)
    assert result.returncode != 0
    assert "version" in result.stderr.lower()
    assert not list(tmp_path.glob("*.pkg"))


def test_build_rejects_non_mach_o_binary(tmp_path: Path):
    fake = tmp_path / "edgedisco"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    result = run(
        BUILD,
        "1.2.3",
        "--binary",
        fake,
        "--output-dir",
        tmp_path,
        check=False,
    )
    assert result.returncode != 0
    assert "mach-o" in result.stderr.lower()
    assert not list(tmp_path.glob("*.pkg"))

    source = tmp_path / "fixture.c"
    source.write_text("int fixture(void) { return 0; }\n")
    object_file = tmp_path / "fixture.o"
    run("cc", "-c", source, "-o", object_file)
    object_file.chmod(0o755)
    object_result = run(
        BUILD,
        "1.2.3",
        "--binary",
        object_file,
        "--output-dir",
        tmp_path,
        check=False,
    )
    assert object_result.returncode != 0
    assert "executable" in object_result.stderr.lower()


def test_make_target_does_not_interpolate_version_as_shell_code(tmp_path: Path):
    marker = tmp_path / "injected"
    malicious = f'1.2.3"; touch "{marker}"; #'
    result = run("make", "macos-pkg", f"VERSION={malicious}", check=False)
    assert result.returncode != 0
    assert not marker.exists()

    generated = REPO / "dist/EdgeDisco-1.2.8-unsigned.pkg"
    try:
        successful = run("make", "macos-pkg", "VERSION=1.2.8")
        assert generated.name in successful.stdout
    finally:
        generated.unlink(missing_ok=True)


def test_release_preflight_fails_closed_and_plan_is_non_mutating(tmp_path: Path):
    package = tmp_path / "EdgeDisco-9.9.9-unsigned.pkg"
    package.write_bytes(b"placeholder")
    clean_env = {
        "DEVELOPER_ID_APPLICATION": "",
        "DEVELOPER_ID_INSTALLER": "",
        "NOTARY_PROFILE": "",
    }
    missing = run(RELEASE, "--plan", package, env=clean_env, check=False)
    assert missing.returncode != 0
    assert "DEVELOPER_ID_APPLICATION" in missing.stderr
    assert sorted(tmp_path.iterdir()) == [package]

    run("cargo", "build", "--release", "--bin", "edgedisco")
    real_package = Path(
        run(
            BUILD,
            "1.2.3",
            "--binary",
            REPO / "target/release/edgedisco",
            "--output-dir",
            tmp_path,
        )
        .stdout.strip()
        .splitlines()[-1]
    )
    release_env = {
        "DEVELOPER_ID_APPLICATION": "Developer ID Application: Example (TEAMID)",
        "DEVELOPER_ID_INSTALLER": "Developer ID Installer: Example (TEAMID)",
        "NOTARY_PROFILE": "edgedisco-notary",
    }
    configured = run(RELEASE, "--plan", real_package, env=release_env)
    assert "codesign" in configured.stdout
    assert "productbuild" in configured.stdout
    assert "notarytool submit" in configured.stdout
    assert "stapler staple" in configured.stdout
    assert set(tmp_path.iterdir()) == {package, real_package}

    expanded = tmp_path / "hostile-expanded"
    run("pkgutil", "--expand-full", real_package, expanded)
    (expanded / "EdgeDisco.pkg/Payload/unauthorized-server").write_text("forbidden")
    hostile = tmp_path / "EdgeDisco-1.2.4-unsigned.pkg"
    run("pkgutil", "--flatten", expanded, hostile)
    rejected = run(RELEASE, "--plan", hostile, env=release_env, check=False)
    assert rejected.returncode != 0
    assert "unexpected" in rejected.stderr.lower()

    unsafe_expanded = tmp_path / "unsafe-mode-expanded"
    run("pkgutil", "--expand-full", real_package, unsafe_expanded)
    (
        unsafe_expanded / "EdgeDisco.pkg/Payload/usr/local/libexec/edgedisco/edgedisco"
    ).chmod(0o777)
    unsafe_dir = tmp_path / "unsafe"
    unsafe_dir.mkdir()
    unsafe_package = unsafe_dir / "EdgeDisco-1.2.3-unsigned.pkg"
    run("pkgutil", "--flatten", unsafe_expanded, unsafe_package)
    unsafe_result = run(RELEASE, "--plan", unsafe_package, env=release_env, check=False)
    assert unsafe_result.returncode != 0
    assert "mode" in unsafe_result.stderr.lower()

    special_expanded = tmp_path / "special-object-expanded"
    run("pkgutil", "--expand-full", real_package, special_expanded)
    os.mkfifo(special_expanded / "EdgeDisco.pkg/Payload/unsupported-fifo")
    special_dir = tmp_path / "special"
    special_dir.mkdir()
    special_package = special_dir / "EdgeDisco-1.2.3-unsigned.pkg"
    run("pkgutil", "--flatten", special_expanded, special_package)
    special_result = run(
        RELEASE, "--plan", special_package, env=release_env, check=False
    )
    assert special_result.returncode != 0
    assert "unexpected" in special_result.stderr.lower()


@pytest.mark.skipif(
    shutil.which("pkgbuild") is None, reason="Apple packaging tools required"
)
def test_real_flat_package_payload_scripts_modes_and_unsigned_signature(tmp_path: Path):
    run("cargo", "build", "--release", "--bin", "edgedisco")
    previous_umask = os.umask(0o077)
    try:
        package = Path(
            run(
                BUILD,
                "0.1.0",
                "--binary",
                REPO / "target/release/edgedisco",
                "--output-dir",
                tmp_path,
            )
            .stdout.strip()
            .splitlines()[-1]
        )
    finally:
        os.umask(previous_umask)
    assert package.name == "EdgeDisco-0.1.0-unsigned.pkg"
    assert package.is_file()

    signature = run("pkgutil", "--check-signature", package, check=False)
    signature_text = signature.stdout + signature.stderr
    assert "Status: no signature" in signature_text

    expanded = tmp_path / "expanded package"
    run("pkgutil", "--expand-full", package, expanded)
    component = expanded / "EdgeDisco.pkg"
    payload = component / "Payload"
    expected_files = {
        "usr/local/libexec/edgedisco/edgedisco",
        "Library/LaunchDaemons/com.edgedisco.daemon.plist",
        "Library/LaunchAgents/com.edgedisco.agent.plist",
    }
    actual_files = {
        str(path.relative_to(payload)) for path in payload.rglob("*") if path.is_file()
    }
    assert actual_files == expected_files
    assert (component / "Scripts/preinstall").is_file()
    assert (component / "Scripts/postinstall").is_file()
    assert stat.S_IMODE((payload / BINARY_PATH.lstrip("/")).stat().st_mode) == 0o755
    for executable_parent in (
        payload / "usr",
        payload / "usr/local",
        payload / "usr/local/libexec",
        payload / "usr/local/libexec/edgedisco",
    ):
        assert stat.S_IMODE(executable_parent.stat().st_mode) == 0o755
    assert (
        stat.S_IMODE(
            (payload / "Library/LaunchDaemons/com.edgedisco.daemon.plist")
            .stat()
            .st_mode
        )
        == 0o644
    )
    assert (
        stat.S_IMODE(
            (payload / "Library/LaunchAgents/com.edgedisco.agent.plist").stat().st_mode
        )
        == 0o644
    )
    support = payload / DATA_ROOT.lstrip("/")
    assert stat.S_IMODE(support.stat().st_mode) == 0o750
    assert stat.S_IMODE((support / "config").stat().st_mode) == 0o750
    assert stat.S_IMODE((support / "logs").stat().st_mode) == 0o750
    assert stat.S_IMODE((support / "data").stat().st_mode) == 0o700
    assert "Mach-O" in run("file", payload / BINARY_PATH.lstrip("/")).stdout

    listed = run("pkgutil", "--payload-files", package).stdout.splitlines()
    for required in expected_files:
        assert f"./{required}" in listed
    lowered_paths = "\n".join(listed).lower()
    for forbidden in (
        "python",
        "mcp",
        "dashboard",
        "webserver",
        ".pem",
        ".p12",
        "notary",
    ):
        assert forbidden not in lowered_paths
    text_payload = b"\n".join(
        path.read_bytes()
        for path in (
            payload / "Library/LaunchDaemons/com.edgedisco.daemon.plist",
            payload / "Library/LaunchAgents/com.edgedisco.agent.plist",
            component / "Scripts/preinstall",
            component / "Scripts/postinstall",
        )
    ).lower()
    for secret_marker in (
        b"-----begin private key-----",
        b"enrollment_token",
        b"developer_id_application",
        b"notary_profile",
    ):
        assert secret_marker not in text_payload

    bom = run("lsbom", "-p", "f?", component / "Bom").stdout
    assert f"./{BINARY_PATH.lstrip('/')}\troot/wheel" in bom
