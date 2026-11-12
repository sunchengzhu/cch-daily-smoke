"""Daily verification must report a mismatch without changing installed nodes."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
VERIFY = ROOT / "scripts" / "verify_fnn.sh"
UPGRADE = ROOT / "scripts" / "update_fnn.sh"
pytestmark = pytest.mark.skipif(
    not shutil.which("jq") or not shutil.which("timeout"),
    reason="needs jq and GNU timeout",
)


def fake_node(path: Path, version: str) -> None:
    path.mkdir()
    binary = path / "fnn"
    binary.write_text(f"#!/bin/sh\nprintf '%s\\n' '{version}'\n")
    binary.chmod(0o755)


def run_verify(tmp_path: Path, node2_version: str) -> subprocess.CompletedProcess[str]:
    node1, node2 = tmp_path / "node1", tmp_path / "node2"
    expected = "fnn Fiber v0.10.0-rc3 (7c8b0e2 2026-11-12)"
    fake_node(node1, expected)
    fake_node(node2, node2_version)
    cli = node1 / "fnn-cli"
    cli.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = --version ]; then echo 'fnn-cli 0.10.0-rc3'; else "
        "echo '{\"version\":\"0.10.0-rc3\",\"commit_hash\":\"7c8b0e2 2026-11-12\","
        "\"pubkey\":\"test-pubkey\"}'; fi\n"
    )
    cli.chmod(0o755)
    token = tmp_path / "token"
    token.write_text("test-token")
    output = tmp_path / "output"
    env = {
        **os.environ,
        "CCH_SMOKE_FNN_EXPECTED_TAG": "v0.10.0-rc3",
        "CCH_SMOKE_FNN_EXPECTED_COMMIT": "7c8b0e2",
        "CCH_SMOKE_NODE1_DIR": str(node1),
        "CCH_SMOKE_NODE2_DIR": str(node2),
        "CCH_SMOKE_FNN_AUTH_TOKEN_FILE": str(token),
        "GITHUB_OUTPUT": str(output),
    }
    result = subprocess.run(["bash", str(VERIFY)], env=env, capture_output=True, text=True)
    return result


def test_matching_nodes_pass_and_report_installed_version(tmp_path):
    version = "fnn Fiber v0.10.0-rc3 (7c8b0e2 2026-11-12)"
    result = run_verify(tmp_path, version)

    assert result.returncode == 0, result.stderr
    assert "Both installed Fiber nodes match" in result.stdout
    assert "fnn_package=installed release v0.10.0-rc3" in (tmp_path / "output").read_text()


def test_mismatched_node_fails_without_replacing_it(tmp_path):
    old_version = "fnn Fiber v0.10.0-rc2 (7aa1d5d 2026-11-09)"
    result = run_verify(tmp_path, old_version)

    assert result.returncode != 0
    assert "upgrade it manually" in result.stderr
    assert old_version in (tmp_path / "node2" / "fnn").read_text()


def test_manual_release_upgrade_requires_explicit_tag(tmp_path):
    env = {**os.environ, "CCH_SMOKE_FNN_SOURCE": "release"}
    env.pop("CCH_SMOKE_FNN_RELEASE_TAG", None)
    result = subprocess.run(["bash", str(UPGRADE)], env=env, capture_output=True, text=True)

    assert result.returncode != 0
    assert "requires CCH_SMOKE_FNN_RELEASE_TAG" in result.stderr
