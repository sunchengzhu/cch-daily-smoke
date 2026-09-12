from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor

import pytest

from scripts.claim_daily_smoke import claim


NOW = datetime(2026, 9, 13, 2, tzinfo=timezone.utc)


def env(run_id="1", **overrides):
    return {
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_RUN_ID": run_id,
        "SCHEDULED_DATE": "2026-09-13",
        **overrides,
    }


def test_server_and_delayed_fallback_execute_only_once(tmp_path):
    assert claim(env(), tmp_path, NOW)["should_run"] == "true"
    fallback = env("2", GITHUB_EVENT_NAME="schedule", SCHEDULED_DATE="")
    assert claim(fallback, tmp_path, NOW.replace(hour=7))["should_run"] == "false"
    assert claim(env("3"), tmp_path, NOW)["should_run"] == "false"


def test_explicit_github_rerun_and_manual_run_still_work(tmp_path):
    claim(env(), tmp_path, NOW)
    assert claim(env(), tmp_path, NOW)["should_run"] == "true"
    assert claim(env("2", SCHEDULED_DATE=""), tmp_path, NOW)["should_run"] == "true"
    assert claim(env("3"), tmp_path, NOW)["should_run"] == "false"


def test_manual_run_does_not_consume_daily_slot(tmp_path):
    claim(env("2", SCHEDULED_DATE=""), tmp_path, NOW)
    assert not list(tmp_path.iterdir())
    assert claim(env(), tmp_path, NOW)["should_run"] == "true"


@pytest.mark.parametrize("day", ["2026-09-12", "2026-09-14"])
def test_stale_or_future_dispatch_skips(tmp_path, day):
    assert claim(env(SCHEDULED_DATE=day), tmp_path, NOW)["should_run"] == "false"
    assert not list(tmp_path.iterdir())


def test_early_run_skips_without_consuming_daily_slot(tmp_path):
    assert claim(env(), tmp_path, NOW.replace(hour=1))["should_run"] == "false"
    assert claim(env(), tmp_path, NOW)["should_run"] == "true"


def test_next_day_is_independent(tmp_path):
    claim(env(), tmp_path, NOW)
    assert claim(env("2", SCHEDULED_DATE="2026-09-14"), tmp_path, NOW.replace(day=14))["should_run"] == "true"


@pytest.mark.parametrize("overrides", [
    {"SCHEDULED_DATE": "../bad"},
    {"SCHEDULED_DATE": "20260913"},
    {"GITHUB_REF": "refs/heads/develop"},
    {"FIBER_SOURCE": "pr"},
])
def test_invalid_daily_inputs_fail_closed(tmp_path, overrides):
    with pytest.raises(ValueError):
        claim(env(**overrides), tmp_path, NOW)


def test_concurrent_claims_have_one_winner(tmp_path):
    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(lambda i: claim(env(str(i)), tmp_path, NOW), range(10)))
    assert sum(r["should_run"] == "true" for r in results) == 1


def test_corrupt_state_does_not_silently_repeat_payments(tmp_path):
    (tmp_path / "2026-09-13.json").write_text("broken")
    with pytest.raises(ValueError):
        claim(env(), tmp_path, NOW)
