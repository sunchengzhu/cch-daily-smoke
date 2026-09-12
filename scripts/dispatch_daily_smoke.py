#!/usr/bin/env python3
"""Dispatch one daily smoke using a server's existing GitHub CLI login.

Keep --state-dir outside runner checkouts. An uncertain POST is never retried
automatically: subsequent calls reconcile it against GitHub or require review.
"""

import argparse
from datetime import date, datetime, time, timezone
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from urllib.parse import urlencode
from zoneinfo import ZoneInfo


REPOSITORY = "sunchengzhu/cch-daily-smoke"
WORKFLOW = "cch-daily-smoke.yml"
ENDPOINT = f"repos/{REPOSITORY}/actions/workflows/{WORKFLOW}"
BEIJING = ZoneInfo("Asia/Shanghai")
DEFAULT_STATE_DIR = Path("/var/lib/cch-daily-smoke-dispatch")


def gh_api(endpoint, payload=None, paginate=False):
    if any(os.environ.get(name) for name in ("GH_TOKEN", "GITHUB_TOKEN")):
        raise RuntimeError("Unset GH_TOKEN/GITHUB_TOKEN; use the server's stored gh login.")
    if not shutil.which("gh"):
        raise RuntimeError("gh is not installed; install GitHub CLI and run gh auth login.")
    command = ["gh", "api", "--hostname", "github.com", "--method",
               "POST" if payload is not None else "GET", endpoint,
               "-H", "X-GitHub-Api-Version: 2026-03-10"]
    if paginate:
        command.extend(["--paginate", "--slurp"])
    if payload is not None:
        command.extend(["--input", "-"])
    try:
        result = subprocess.run(command, input=json.dumps(payload) if payload is not None else None,
                                capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"GitHub API request did not complete ({type(exc).__name__}).") from exc
    if result.returncode:
        raise RuntimeError(f"GitHub API request failed (gh exit {result.returncode}); check server gh auth and connectivity.")
    return json.loads(result.stdout) if result.stdout.strip() else None


def find_daily_run(day):
    since = datetime.combine(day, time.min, BEIJING).astimezone(timezone.utc)
    query = urlencode({"branch": "main", "event": "workflow_dispatch", "per_page": 100,
                       "created": ">=" + since.strftime("%Y-%m-%dT%H:%M:%SZ")})
    pages = gh_api(f"{ENDPOINT}/runs?{query}", paginate=True)
    if not isinstance(pages, list) or not pages:
        raise RuntimeError("Unexpected workflow-runs API response; refusing to dispatch.")
    for page in pages:
        if not isinstance(page, dict) or not isinstance(page.get("workflow_runs"), list):
            raise RuntimeError("Incomplete workflow-runs API response; refusing to dispatch.")
        for run in page["workflow_runs"]:
            if not isinstance(run, dict):
                raise RuntimeError("Invalid workflow run; refusing to dispatch.")
            if (run.get("head_branch") == "main" and run.get("event") == "workflow_dispatch"
                    and run.get("display_title") == f"CCH daily smoke · {day.isoformat()}"):
                if type(run.get("id")) is not int or run["id"] <= 0:
                    raise RuntimeError("Matched daily run has no valid ID; refusing to dispatch.")
                return run["id"]
    return None


def save_state(path, state):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(state, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def dispatch(state_dir=DEFAULT_STATE_DIR, scheduled_date=None, now=None):
    now = (now or datetime.now(BEIJING)).astimezone(BEIJING)
    day = date.fromisoformat(scheduled_date) if scheduled_date else now.date()
    if scheduled_date and scheduled_date != day.isoformat():
        raise ValueError("--date must use YYYY-MM-DD format.")
    if day != now.date():
        raise ValueError("--date must be today's date in Asia/Shanghai; past/future dates are not dispatched.")
    if now.hour < 10:
        return "Not due: daily smoke starts at 10:00 Asia/Shanghai."
    state_dir = Path(state_dir)
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    with os.fdopen(os.open(state_dir / "dispatch.lock", os.O_CREAT | os.O_RDWR, 0o600), "a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return "Another dispatcher holds the state lock; no request sent."
        path = state_dir / f"{day.isoformat()}.json"
        state = json.loads(path.read_text()) if path.exists() else None
        if state is not None:
            if (not isinstance(state, dict) or state.get("date") != day.isoformat()
                    or state.get("status") not in ("pending", "accepted")):
                raise RuntimeError("Invalid daily state; review it before dispatching.")
            if state["status"] == "accepted":
                return f"Already accepted for {day}: run {state.get('run_id') or 'ID unavailable'}."
        run_id = find_daily_run(day)
        if run_id is not None:
            save_state(path, {"date": day.isoformat(), "status": "accepted", "run_id": run_id})
            return f"Existing daily run {run_id}; no request sent."
        if state is not None:
            raise RuntimeError(f"Uncertain dispatch for {day}; no matching run found. Pending state retained; manual review required.")
        save_state(path, {"date": day.isoformat(), "status": "pending", "requested_at": now.isoformat()})
        response = gh_api(f"{ENDPOINT}/dispatches", payload={"ref": "main", "inputs": {
            "fiber_source": "release", "send_discord_report": True, "scheduled_date": day.isoformat()}})
        run_id = response.get("workflow_run_id") if isinstance(response, dict) else None
        if response is not None and (type(run_id) is not int or run_id <= 0):
            raise RuntimeError("Unexpected dispatch response. Pending state retained; manual review required.")
        save_state(path, {"date": day.isoformat(), "status": "accepted", "run_id": run_id})
        return f"Accepted daily smoke for {day}: run {run_id or 'ID unavailable (HTTP 204)'}."


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    parser.add_argument("--date", help="Asia/Shanghai date, YYYY-MM-DD; only today is allowed")
    parser.add_argument("--check", action="store_true", help="Read-only gh authentication/API preflight")
    args = parser.parse_args(argv)
    try:
        if args.check:
            response = gh_api(f"{ENDPOINT}/runs?per_page=1")
            if not isinstance(response, dict) or not isinstance(response.get("workflow_runs"), list):
                raise RuntimeError("Unexpected workflow-runs response.")
            print("Preflight passed: gh login can read workflow runs; no dispatch sent. Write permission is not proven.")
        else:
            print(dispatch(args.state_dir, args.date))
    except (OSError, ValueError, RuntimeError) as exc:
        parser.exit(1, f"daily-smoke-dispatch: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
