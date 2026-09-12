#!/usr/bin/env python3
"""Claim one daily smoke execution across the server timer and GitHub fallback."""

from __future__ import annotations

import fcntl
import json
import os
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo


SHANGHAI = ZoneInfo("Asia/Shanghai")


def claim(environ, state_dir: Path, now: datetime) -> dict[str, str]:
    event = environ["GITHUB_EVENT_NAME"]
    requested = environ.get("SCHEDULED_DATE", "")
    if event == "workflow_dispatch" and not requested:
        return {"should_run": "true", "scheduled_date": "", "reason": "manual run"}
    if event not in {"schedule", "workflow_dispatch"}:
        raise ValueError("unsupported trigger")
    if environ.get("GITHUB_REF") != "refs/heads/main":
        raise ValueError("daily runs must use main")
    if environ.get("FIBER_SOURCE", "release") != "release" or environ.get("FIBER_PR_NUMBER"):
        raise ValueError("daily runs must use the release source")

    local_now = now.astimezone(SHANGHAI)
    day = requested or local_now.date().isoformat()
    if date.fromisoformat(day).isoformat() != day:
        raise ValueError("scheduled_date must be YYYY-MM-DD")
    result = {"should_run": "false", "scheduled_date": day}
    if day != local_now.date().isoformat() or local_now.hour < 10:
        return {**result, "reason": "not due today; skip stale or early trigger"}

    run_id = environ["GITHUB_RUN_ID"]
    if not run_id.isdecimal():
        raise ValueError("invalid GitHub run ID")
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (state_dir / "claim.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        record = state_dir / f"{day}.json"
        if record.exists():
            prior = json.loads(record.read_text())
            # An explicit GitHub re-run keeps its original run ID.
            if prior["run_id"] != run_id:
                return {**result, "reason": f"already claimed by run {prior['run_id']}"}
        else:
            temporary = state_dir / f"{day}.tmp"
            with temporary.open("w") as output:
                json.dump({"run_id": run_id, "claimed_at": local_now.isoformat()}, output)
                output.flush()
                os.fsync(output.fileno())
            temporary.replace(record)
            directory_fd = os.open(state_dir, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    return {**result, "should_run": "true", "reason": f"daily execution claimed by run {run_id}"}


def main():
    state_dir = Path.home() / ".local/state/cch-daily-smoke/executions"
    result = claim(os.environ, state_dir, datetime.now(SHANGHAI))
    with open(os.environ["GITHUB_OUTPUT"], "a") as output:
        for key, value in result.items():
            output.write(f"{key}={value}\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
