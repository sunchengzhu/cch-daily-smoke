"""Environment-unavailable classification must reach the run report."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from smoke_failure_class import (
    ENVIRONMENT_UNAVAILABLE,
    EnvironmentUnavailable,
    classify_failure,
    describe_environment_failure,
    emit_failure_class,
)

ROOT = Path(__file__).resolve().parents[1]
REPORT_SCRIPT = ROOT / "scripts" / "send_daily_smoke_report.py"


def load_report_module():
    spec = importlib.util.spec_from_file_location("send_daily_smoke_report", REPORT_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


report = load_report_module()


# --- classification -------------------------------------------------------


def test_environment_fault_is_classified():
    assert classify_failure(EnvironmentUnavailable("peer not connected")) == (
        ENVIRONMENT_UNAVAILABLE
    )


def test_plain_assertion_is_not_classified():
    """An unattributed failure must never be excused as environmental."""

    assert classify_failure(AssertionError("balance mismatch")) is None


def test_classification_survives_wrapping():
    """The relay scenario re-raises a timeout that wraps the real cause."""

    try:
        try:
            raise EnvironmentUnavailable("peer not connected")
        except EnvironmentUnavailable as inner:
            raise AssertionError("timed out waiting for HTLCs") from inner
    except AssertionError as outer:
        assert classify_failure(outer) == ENVIRONMENT_UNAVAILABLE


def test_no_false_positive_when_an_unrelated_error_is_chained():
    try:
        try:
            raise ValueError("unrelated")
        except ValueError as inner:
            raise AssertionError("product regression") from inner
    except AssertionError as outer:
        assert classify_failure(outer) is None


def test_classification_is_written_as_a_step_output(tmp_path, monkeypatch):
    output = tmp_path / "github_output"
    output.write_text("", encoding="utf-8")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))

    assert emit_failure_class(EnvironmentUnavailable("peer not connected")) == (
        ENVIRONMENT_UNAVAILABLE
    )
    assert output.read_text(encoding="utf-8") == (
        f"failure_class={ENVIRONMENT_UNAVAILABLE}\n"
    )


def test_unclassified_failure_writes_nothing(tmp_path, monkeypatch):
    output = tmp_path / "github_output"
    output.write_text("", encoding="utf-8")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))

    assert emit_failure_class(AssertionError("balance mismatch")) is None
    assert output.read_text(encoding="utf-8") == ""


def test_missing_github_output_is_not_an_error(monkeypatch):
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)

    assert emit_failure_class(EnvironmentUnavailable("x")) == ENVIRONMENT_UNAVAILABLE


def test_describe_only_explains_environment_faults():
    assert "environment" in describe_environment_failure(ENVIRONMENT_UNAVAILABLE).lower()
    assert describe_environment_failure("") == ""
    assert describe_environment_failure("something-else") == ""


# --- report rendering ----------------------------------------------------


def sample_report(failure_class: str = "") -> dict:
    return {
        "job_result": "failure",
        "failure_class": failure_class,
        "branch": "main",
        "fiber_source": "release",
        "fnn_version": "fnn Fiber v0.9.1",
        "fnn_package": "release v0.9.1",
        "total_seconds": 210,
        "scheduled_date": "",
        "started_at": "1790400000",
        "trigger": "workflow_dispatch",
        "preflight": {
            "Checkout": "success",
            "FNN nodes": "success",
            "LND liquidity": "success",
            "Cleanup": "success",
        },
        "scenarios": {
            "local": {"outcome": "success", "data": {"duration_seconds": 5.0}},
            "direct": {"outcome": "success", "data": {"duration_seconds": 11.0}},
            "relay": {"outcome": "failure", "data": None},
        },
        "run_url": "https://example.invalid/run",
        "run_number": "141",
        "run_attempt": "1",
        "sha": "abc123",
    }


def test_environment_fault_changes_the_discord_verdict():
    payload = report.build_discord_payload(sample_report(ENVIRONMENT_UNAVAILABLE))
    embed = payload["embeds"][0]

    assert embed["color"] == report.ENVIRONMENT_COLOR
    assert "Environment unavailable" in embed["description"]
    assert "relay path not verified" in embed["description"]
    assert "no product regression detected" not in embed["description"]
    assert embed["title"].startswith("🔌")
    checks = next(f for f in embed["fields"] if f["name"] == "Checks")
    assert "Environment unavailable" in checks["value"]
    assert "All checks passed" not in checks["value"]


def test_plain_failure_keeps_the_regression_verdict():
    payload = report.build_discord_payload(sample_report(""))
    embed = payload["embeds"][0]

    assert embed["color"] == report.FAILURE_COLOR
    assert "workflow did not pass" in embed["description"]
    assert "Environment unavailable" not in embed["description"]
    assert not embed["title"].startswith("🔌")


def test_success_is_unaffected_by_the_new_branch():
    successful = sample_report("")
    successful["job_result"] = "success"
    successful["scenarios"]["relay"] = {"outcome": "success", "data": {"duration_seconds": 12.0}}

    embed = report.build_discord_payload(successful)["embeds"][0]

    assert embed["color"] == report.SUCCESS_COLOR
    assert "3/3 smoke scenarios passed" in embed["description"]


def test_environment_fault_is_visible_in_the_github_summary():
    summary = report.build_github_summary(sample_report(ENVIRONMENT_UNAVAILABLE))

    assert "Environment unavailable" in summary
    assert "🔌" in summary
    assert "relay flow remains unverified" in summary
    assert "not a product regression" not in summary


def test_report_constant_matches_the_test_module():
    """The script cannot import the tests directory, so the value is duplicated."""

    assert report.ENVIRONMENT_UNAVAILABLE == ENVIRONMENT_UNAVAILABLE
    assert report.FAILURE_CLASS_ENV == "CCH_REPORT_FAILURE_CLASS"


def test_collect_report_reads_the_failure_class():
    environ = {
        "CCH_REPORT_JOB_RESULT": "failure",
        "CCH_REPORT_FAILURE_CLASS": ENVIRONMENT_UNAVAILABLE,
    }

    assert report.collect_report(environ)["failure_class"] == ENVIRONMENT_UNAVAILABLE


def test_discord_payload_stays_within_limits_with_the_environment_note():
    payload = report.build_discord_payload(sample_report(ENVIRONMENT_UNAVAILABLE))
    encoded = json.dumps(payload, ensure_ascii=False)

    assert len(encoded) < 6000
    for field in payload["embeds"][0]["fields"]:
        assert len(field["value"]) <= report.FIELD_VALUE_LIMIT
