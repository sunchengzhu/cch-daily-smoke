import argparse
import logging
import subprocess
import time

import pytest

from scripts.run_stability import (
    JsonlWriter,
    MODE_SEQUENTIAL,
    RunState,
    build_summary,
    compact_error,
    cleanup_lnd_to_fiber_invoice,
    log_preflight,
    percentile,
    run_flow_lnd_to_fiber,
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


def test_cleanup_cancels_open_lnd_hold_invoice(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "scripts.run_stability.lncli_json",
        lambda *_args, **_kwargs: {"state": "ACCEPTED"},
    )
    monkeypatch.setattr(
        "scripts.run_stability.lncli_raw",
        lambda _config, node, args: calls.append((node, args)),
    )

    result = cleanup_lnd_to_fiber_invoice(object(), "0xabc")

    assert calls == [("lnd-a", ["cancelinvoice", "abc"])]
    assert result == {
        "action": "cancel",
        "previous_state": "ACCEPTED",
        "result": "PASS",
    }


def test_cleanup_leaves_settled_lnd_invoice_unchanged(monkeypatch):
    monkeypatch.setattr(
        "scripts.run_stability.lncli_json",
        lambda *_args, **_kwargs: {"state": "SETTLED"},
    )
    monkeypatch.setattr(
        "scripts.run_stability.lncli_raw",
        lambda *_args, **_kwargs: pytest.fail("settled invoice must not be canceled"),
    )

    result = cleanup_lnd_to_fiber_invoice(object(), "0xabc")

    assert result == {"action": "none", "previous_state": "SETTLED", "result": "PASS"}


def test_lnd_to_fiber_failure_cleans_up_hold_invoice(monkeypatch):
    config = argparse.Namespace(
        f1_rpc="f1",
        command_timeout=60,
        udt_script={"code_hash": "code", "hash_type": "type", "args": "args"},
    )
    monkeypatch.setattr(
        "scripts.run_stability.create_fiber_invoice", lambda *_args: ("fiber", "0xabc")
    )
    monkeypatch.setattr(
        "scripts.run_stability.fnn",
        lambda *_args: {
            "payment_hash": "0xabc",
            "wrapped_btc_type_script": config.udt_script,
            "fee_sats": "0xa",
            "amount_sats": "0x64",
            "incoming_invoice": {"Lightning": "lntb-invoice"},
        },
    )
    monkeypatch.setattr(
        "scripts.run_stability.pay_lnd_invoice",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            subprocess.TimeoutExpired(["lncli", "payinvoice"], 60)
        ),
    )
    cleaned = []
    monkeypatch.setattr(
        "scripts.run_stability.cleanup_lnd_to_fiber_invoice",
        lambda _config, payment_hash: (
            cleaned.append(payment_hash)
            or {"action": "cancel", "previous_state": "ACCEPTED", "result": "PASS"}
        ),
    )
    stages = []

    with pytest.raises(subprocess.TimeoutExpired):
        run_flow_lnd_to_fiber(
            config,
            100,
            "tx",
            stage_callback=lambda stage, status, details: stages.append(
                (stage, status, details)
            ),
        )

    assert cleaned == ["0xabc"]
    assert ("pay_lnd_invoice", "FAIL", {"error_type": "TimeoutExpired"}) in stages
    assert (
        "cleanup_lnd_invoice",
        "PASS",
        {"action": "cancel", "previous_state": "ACCEPTED", "result": "PASS"},
    ) in stages


def test_lnd_to_fiber_preflight_rejects_stale_pending_htlcs(monkeypatch):
    monkeypatch.setattr(
        "scripts.run_stability.get_fiber_channel",
        lambda _config: {"local_balance": "0x1000", "remote_balance": "0x1000"},
    )
    monkeypatch.setattr(
        "scripts.run_stability.lnd_channel_balances_from_a",
        lambda _config: {"lnd_a": 4096, "lnd_b": 4096},
    )
    monkeypatch.setattr(
        "scripts.run_stability.lnd_b_liquidity",
        lambda _config: {"spendable_sats": 4096, "pending_htlcs_count": 3},
    )

    with pytest.raises(RuntimeError, match="3 pending HTLC"):
        log_preflight(object(), "lnd-to-fiber", 100, 1)


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


def test_sequential_mode_waits_for_each_flow_before_starting_next(
    monkeypatch, tmp_path
):
    monkeypatch.setattr("scripts.run_stability.log_preflight", lambda *args: None)
    args = argparse.Namespace(
        flow="lnd-to-fiber",
        mode=MODE_SEQUENTIAL,
        tps=100.0,
        duration=0.12,
        amount_sats=100,
        max_inflight=100,
        progress_interval=0.05,
        max_failure_rate=0.0,
    )
    active = 0
    max_active = 0

    def fake_flow(config, amount_sats, transaction_name):
        nonlocal active, max_active
        del config, amount_sats, transaction_name
        active += 1
        max_active = max(max_active, active)
        time.sleep(0.03)
        active -= 1
        return {"payment_hash": "fake"}

    writer = JsonlWriter(tmp_path / "details.jsonl")
    try:
        summary = run_load(args, object(), writer, fake_flow)
    finally:
        writer.close()

    assert 3 <= summary["scheduled"] <= 5
    assert summary["started"] == summary["scheduled"]
    assert summary["succeeded"] == summary["scheduled"]
    assert summary["rejected"] == 0
    assert summary["load_mode"] == "sequential"
    assert summary["target_tps"] is None
    assert summary["target_transactions"] is None
    assert summary["max_inflight"] == 1
    assert max_active == 1


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
    assert any("success_tps=" in message and "success_p95=" in message for message in messages)
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
