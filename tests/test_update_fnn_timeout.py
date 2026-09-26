"""Exercise the real validation wait and cleanup with disposable child processes."""

from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys

import pytest


UPDATE_FNN = Path(__file__).resolve().parents[1] / "scripts" / "update_fnn.sh"
pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


def shell_function(script: str, name: str) -> str:
    match = re.search(rf"^{name}\(\) \{{.*?^\}}", script, re.MULTILINE | re.DOTALL)
    assert match, f"missing shell function: {name}"
    return match.group()


def run_validation(tmp_path: Path, node1: str, node2: str):
    script = UPDATE_FNN.read_text(encoding="utf-8")
    validation = script.split('  VALIDATION_STARTED_AT="$(date +%s)"', 1)[1]
    validation = '  VALIDATION_STARTED_AT="$(date +%s)"' + validation.split(
        '\n  BACKUP_DIR="$BACKUP_ROOT/', 1
    )[0]
    work_dir = tmp_path / "validation"
    work_dir.mkdir()
    binary = work_dir / "fnn"
    binary.write_text(
        f"#!{sys.executable}\n"
        """import os
from pathlib import Path
import signal
import sys
import time

node = sys.argv[sys.argv.index("--dir") + 1]
markers = Path(os.environ["TEST_MARKERS"])
(markers / f"{node}.pid").write_text(str(os.getpid()))
outcome = os.environ[f"TEST_{node.upper()}_OUTCOME"]
if outcome != "hang":
    sys.exit(int(outcome))

def record_term(_signum, _frame):
    (markers / f"{node}.term").write_text(str(time.monotonic()))

# Keep running after TERM so the test also requires the KILL/reap path.
signal.signal(signal.SIGTERM, record_term)
while True:
    time.sleep(60)
""",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    harness = "\n".join(
        [
            "set -Eeuo pipefail",
            shell_function(script, "wait_for_pid_with_timeout"),
            shell_function(script, "cleanup"),
            shell_function(script, "log"),
            "rollback() { printf 'rollback-called\\n'; }",
            'TMP_DIR="$TEST_WORK_DIR"',
            "NODE1_DIR=node1",
            "NODE2_DIR=node2",
            "SERVICES_STOPPED=1",
            "BINARIES_REPLACED=0",
            "CLI_REPLACED=0",
            "VALIDATION_TIMEOUT=3",
            "trap cleanup EXIT",
            validation,
        ]
    )
    env = {
        **os.environ,
        "TEST_WORK_DIR": str(work_dir),
        "TEST_MARKERS": str(tmp_path),
        "TEST_NODE1_OUTCOME": node1,
        "TEST_NODE2_OUTCOME": node2,
    }
    process = subprocess.Popen(
        ["bash", "-c", harness],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=15)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        raise
    return process.returncode, stdout, stderr


def test_parallel_validations_share_one_deadline_and_are_reaped(tmp_path):
    status, stdout, stderr = run_validation(tmp_path, "hang", "hang")

    assert status == 1
    assert "database validation timed out after 3s (node1=124, node2=124)" in stderr
    assert "rollback-called" in stdout
    term_times = [float((tmp_path / f"node{node}.term").read_text()) for node in (1, 2)]
    # The second wait may spend only the first child's one-second kill grace
    # past the shared deadline; it must not grant another three-second budget.
    assert term_times[1] - term_times[0] < 3, term_times
    for node in (1, 2):
        pid = int((tmp_path / f"node{node}.pid").read_text())
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


@pytest.mark.parametrize("node1,node2", [("7", "0"), ("0", "9")])
def test_failed_validation_preserves_exit_status_and_rolls_back(tmp_path, node1, node2):
    status, stdout, stderr = run_validation(tmp_path, node1, node2)

    assert status == 1
    assert f"database validation failed (node1={node1}, node2={node2})" in stderr
    assert "timed out" not in stderr
    assert "rollback-called" in stdout


def test_successful_validations_do_not_roll_back(tmp_path):
    status, stdout, stderr = run_validation(tmp_path, "0", "0")

    assert status == 0, stderr
    assert "both node stores validated" in stdout
    assert "rollback-called" not in stdout
