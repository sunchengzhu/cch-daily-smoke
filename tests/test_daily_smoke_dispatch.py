from datetime import datetime, timedelta, timezone
import fcntl
import json
import subprocess
from types import SimpleNamespace

import pytest

from scripts import dispatch_daily_smoke as dispatch


NOW = datetime(2026, 9, 12, 10, tzinfo=dispatch.BEIJING)
DAY = "2026-09-12"


@pytest.fixture
def github(monkeypatch):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(dispatch.shutil, "which", lambda name: "/usr/bin/gh")
    api = SimpleNamespace(calls=[], runs=[], pages=None, response={"workflow_run_id": 123},
                          post_error=None, before_post=None)

    def run(command, **kwargs):
        payload = json.loads(kwargs["input"]) if kwargs.get("input") else None
        api.calls.append((command, payload))
        if payload is not None:
            if api.before_post:
                api.before_post()
            if api.post_error:
                raise api.post_error
            output = "" if api.response is None else json.dumps(api.response)
        else:
            page = {"workflow_runs": api.runs}
            output = json.dumps((api.pages or [page]) if "--slurp" in command else page)
        return SimpleNamespace(returncode=0, stdout=output, stderr="")

    monkeypatch.setattr(dispatch.subprocess, "run", run)
    return api


def daily_run(**overrides):
    return {"id": 42, "head_branch": "main", "event": "workflow_dispatch",
            "display_title": f"CCH daily smoke · {DAY}", **overrides}


def posts(api):
    return [call for call in api.calls if call[1] is not None]


@pytest.mark.parametrize("response", [{"workflow_run_id": 123}, None])
def test_200_and_204_are_persisted_and_not_redispatched(github, tmp_path, response):
    github.response = response
    github.before_post = lambda: assert_pending(tmp_path)
    assert "Accepted" in dispatch.dispatch(tmp_path, now=NOW)
    assert "Already accepted" in dispatch.dispatch(tmp_path, now=NOW)
    command, payload = posts(github)[0]
    assert len(posts(github)) == 1
    assert payload == {"ref": "main", "inputs": {
        "fiber_source": "release", "send_discord_report": True, "scheduled_date": DAY}}
    assert command[-2:] == ["--input", "-"]
    assert "X-GitHub-Api-Version: 2026-03-10" in command
    assert json.loads((tmp_path / f"{DAY}.json").read_text())["status"] == "accepted"


def assert_pending(directory):
    assert json.loads((directory / f"{DAY}.json").read_text())["status"] == "pending"


@pytest.mark.parametrize("run", [daily_run(display_title="cch daily smoke"),
                                daily_run(head_branch="develop"), daily_run(event="schedule"),
                                daily_run(display_title="CCH daily smoke · 2026-09-11")])
def test_unrelated_manual_or_other_runs_do_not_suppress_daily(github, tmp_path, run):
    github.runs = [run]
    dispatch.dispatch(tmp_path, now=NOW)
    assert len(posts(github)) == 1


@pytest.mark.parametrize("status", ["queued", "in_progress", "completed"])
def test_exact_daily_run_deduplicates_even_failed_smoke(github, tmp_path, status):
    github.runs = [daily_run(status=status, conclusion="failure")]
    assert "Existing daily run 42" in dispatch.dispatch(tmp_path, now=NOW)
    assert not posts(github)


def test_pagination_checks_later_pages(github, tmp_path):
    github.pages = [{"workflow_runs": []}, {"workflow_runs": [daily_run()]}]
    assert "Existing daily run 42" in dispatch.dispatch(tmp_path, now=NOW)
    assert not posts(github)


def test_failed_lookup_does_not_create_pending_or_dispatch(github, tmp_path, monkeypatch):
    monkeypatch.setattr(dispatch.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=4))
    with pytest.raises(RuntimeError, match="check server gh auth"):
        dispatch.dispatch(tmp_path, now=NOW)
    assert not (tmp_path / f"{DAY}.json").exists()
    assert not posts(github)


def test_uncertain_post_is_never_blindly_retried(github, tmp_path):
    github.post_error = subprocess.TimeoutExpired("gh", 60)
    with pytest.raises(RuntimeError, match="did not complete"):
        dispatch.dispatch(tmp_path, now=NOW)
    assert_pending(tmp_path)
    with pytest.raises(RuntimeError, match="manual review required"):
        dispatch.dispatch(tmp_path, now=NOW + timedelta(minutes=5))
    assert len(posts(github)) == 1
    github.runs = [daily_run()]
    assert "Existing daily run 42" in dispatch.dispatch(tmp_path, now=NOW)
    assert len(posts(github)) == 1


def test_unexpected_success_response_retains_pending(github, tmp_path):
    github.response = {"message": "unexpected"}
    with pytest.raises(RuntimeError, match="Unexpected dispatch response"):
        dispatch.dispatch(tmp_path, now=NOW)
    assert_pending(tmp_path)


@pytest.mark.parametrize("day", ["2026-09-11", "2026-09-13"])
def test_past_and_future_dates_are_rejected(github, tmp_path, day):
    with pytest.raises(ValueError, match="past/future"):
        dispatch.dispatch(tmp_path, scheduled_date=day, now=NOW)
    assert not github.calls


def test_before_ten_and_new_day_use_beijing_date(github, tmp_path):
    assert "Not due" in dispatch.dispatch(tmp_path, now=NOW - timedelta(seconds=1))
    assert not github.calls
    dispatch.dispatch(tmp_path, now=NOW.astimezone(timezone.utc))
    dispatch.dispatch(tmp_path, now=NOW + timedelta(days=1))
    assert [payload["inputs"]["scheduled_date"] for _, payload in posts(github)] == [DAY, "2026-09-13"]


def test_held_lock_prevents_concurrent_dispatch(github, tmp_path):
    with (tmp_path / "dispatch.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert "holds the state lock" in dispatch.dispatch(tmp_path, now=NOW)
    assert not github.calls


def test_invalid_state_fails_closed(github, tmp_path):
    (tmp_path / f"{DAY}.json").write_text('{"status":"accepted","date":"2026-09-11"}')
    with pytest.raises(RuntimeError, match="Invalid daily state"):
        dispatch.dispatch(tmp_path, now=NOW)
    assert not github.calls


def test_check_does_not_post_or_write_state(github, tmp_path, capsys):
    assert dispatch.main(["--check", "--state-dir", str(tmp_path)]) == 0
    assert "Write permission is not proven" in capsys.readouterr().out
    assert not posts(github)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("name", ["GH_TOKEN", "GITHUB_TOKEN"])
def test_environment_tokens_are_refused(github, monkeypatch, name):
    monkeypatch.setenv(name, "test-not-a-real-token")
    with pytest.raises(RuntimeError, match="stored gh login"):
        dispatch.gh_api(dispatch.ENDPOINT)
    assert not github.calls


def test_missing_gh_and_missing_login_fail_explicitly(github, monkeypatch):
    monkeypatch.setattr(dispatch.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="gh is not installed"):
        dispatch.gh_api(dispatch.ENDPOINT)
    monkeypatch.setattr(dispatch.shutil, "which", lambda name: "/usr/bin/gh")
    monkeypatch.setattr(dispatch.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=4))
    with pytest.raises(RuntimeError, match="check server gh auth"):
        dispatch.gh_api(dispatch.ENDPOINT)
