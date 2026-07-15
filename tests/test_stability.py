import argparse
import logging
import subprocess
import time

from scripts.run_stability import (
    JsonlWriter,
    RunState,
    build_summary,
    compact_error,
    percentile,
    run_load,
    seconds,
)


def test_percentile_interpolates_values():
    assert percentile([], 95) is None
    assert percentile([10], 95) == 10
    assert percentile([10, 20, 30, 40], 50) == 25


def test_compact_error_prefers_stderr_and_truncates():
    error = AssertionError(
        "command failed (1): fnn --invoice very-long-secret\n"
        "stdout:\n\nstderr:\nError: RPC error: Unauthorized"
    )

    assert compact_error(error) == "Error: RPC error: Unauthorized"
    assert compact_error(AssertionError("x" * 20), max_length=10) == "xxxxxxx..."


def test_compact_error_redacts_invoice_from_timeout():
    error = subprocess.TimeoutExpired(
        [
            "docker",
            "exec",
            "lnd-b",
            "lncli",
            "--lnddir=/data/.lnd",
            "--network=testnet4",
            "payinvoice",
            "lntb-secret-invoice",
            "--force",
        ],
        120,
    )

    message = compact_error(error)

    assert message == "lnd-b payinvoice timed out after 120s"
    assert "lntb-secret-invoice" not in message


def test_seconds_formats_milliseconds_for_console():
    assert seconds(None) == "n/a"
    assert seconds(2489.4) == "2.49s"


def test_summary_counts_rejections_as_failures():
    args = argparse.Namespace(
        flow="fiber-to-lnd",
        tps=5.0,
        duration=1.0,
        max_inflight=2,
        max_failure_rate=0.1,
    )
    state = RunState(
        scheduled=10,
        started=9,
        succeeded=8,
        failed=1,
        rejected=1,
        latencies_ms=[100, 200],
        start_delays_ms=[1, 2],
        errors={"ExampleError": 1, "MaxInflightExceeded": 1},
    )

    summary = build_summary(args, "run", state, 10.0, 11.0, 12.0)

    assert summary["failure_rate"] == 0.2
    assert summary["actual_start_tps"] == 9.0
    assert summary["passed"] is False


def test_load_runner_starts_complete_flows_at_target_tps(monkeypatch, tmp_path):
    monkeypatch.setattr("scripts.run_stability.log_preflight", lambda *args: None)
    args = argparse.Namespace(
        flow="fiber-to-lnd",
        tps=5.0,
        duration=0.4,
        amount_sats=100,
        max_inflight=2,
        progress_interval=0.1,
        max_failure_rate=0.0,
    )

    def fake_flow(config, amount_sats, transaction_name):
        del config, amount_sats, transaction_name
        time.sleep(0.01)
        return {"payment_hash": "fake"}

    writer = JsonlWriter(tmp_path / "details.jsonl")
    try:
        summary = run_load(args, object(), writer, fake_flow)
    finally:
        writer.close()

    assert summary["scheduled"] == 2
    assert summary["started"] == 2
    assert summary["succeeded"] == 2
    assert 4.8 <= summary["actual_start_tps"] <= 5.1
    assert summary["passed"] is True


def test_load_runner_logs_compact_progress_and_result(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr("scripts.run_stability.log_preflight", lambda *args: None)
    args = argparse.Namespace(
        flow="fiber-to-lnd",
        tps=5.0,
        duration=0.4,
        amount_sats=100,
        max_inflight=2,
        progress_interval=0.1,
        max_failure_rate=0.0,
    )

    def fake_flow(config, amount_sats, transaction_name):
        del config, amount_sats, transaction_name
        return {"payment_hash": "fake"}

    writer = JsonlWriter(tmp_path / "details.jsonl")
    try:
        with caplog.at_level(logging.INFO, logger="cch-stability"):
            run_load(args, object(), writer, fake_flow)
    finally:
        writer.close()

    messages = [record.message for record in caplog.records]
    assert any(message.startswith("PROGRESS ") for message in messages)
    assert any(message.startswith("RESULT PASS ") for message in messages)
    assert not any(message.startswith("SUMMARY {") for message in messages)


def test_load_runner_logs_saturation_once(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr("scripts.run_stability.log_preflight", lambda *args: None)
    args = argparse.Namespace(
        flow="fiber-to-lnd",
        tps=100.0,
        duration=0.05,
        amount_sats=100,
        max_inflight=1,
        progress_interval=0.01,
        max_failure_rate=0.0,
    )

    def slow_flow(config, amount_sats, transaction_name):
        del config, amount_sats, transaction_name
        time.sleep(0.1)
        return {"payment_hash": "fake"}

    writer = JsonlWriter(tmp_path / "details.jsonl")
    try:
        with caplog.at_level(logging.INFO, logger="cch-stability"):
            summary = run_load(args, object(), writer, slow_flow)
    finally:
        writer.close()

    messages = [record.message for record in caplog.records]
    assert summary["rejected"] == 4
    assert sum(message.startswith("SATURATED ") for message in messages) == 1
    assert not any(message.startswith("TX rejected ") for message in messages)
