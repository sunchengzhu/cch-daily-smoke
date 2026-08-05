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
    create_or_recover_receive_btc_order,
    is_recoverable_receive_btc_error,
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
    monkeypatch.setenv("CCH_SMOKE_FNN_SOURCE", "release")
    config = argparse.Namespace(
        f1_rpc="f1",
        command_timeout=60,
        wait_timeout=10,
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
            "outgoing_pay_req": "fiber",
            "status": "Pending",
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


@pytest.mark.parametrize("fiber_source", ["develop", "pr"])
def test_lnd_to_fiber_recovers_same_request_after_rpc_timeout(
    monkeypatch, fiber_source
):
    monkeypatch.setenv("CCH_SMOKE_FNN_SOURCE", fiber_source)
    config = argparse.Namespace(
        f1_rpc="f1",
        command_timeout=60,
        udt_script={"code_hash": "code", "hash_type": "type", "args": "args"},
    )
    monkeypatch.setattr(
        "scripts.run_stability.create_fiber_invoice", lambda *_args: ("fiber", "0xabc")
    )
    responses = [
        AssertionError(
            "command failed (1): fnn cch receive_btc\n"
            "stdout:\n\nstderr:\nError: RPC error (code -32000): timeout"
        ),
        AssertionError(
            "command failed (1): fnn cch receive_btc\n"
            "stdout:\n\nstderr:\nError: RPC error: receive_btc order creation "
            "for payment hash abc is already being recovered"
        ),
        {
            "payment_hash": "0xabc",
            "wrapped_btc_type_script": config.udt_script,
            "fee_sats": "0xa",
            "amount_sats": "0x6e",
            "incoming_invoice": {"Lightning": "lntb-invoice"},
            "outgoing_pay_req": "fiber",
            "status": "Pending",
        },
    ]
    receive_btc_calls = []

    def fake_fnn(_config, rpc, args):
        receive_btc_calls.append((rpc, args))
        response = responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    monkeypatch.setattr("scripts.run_stability.fnn", fake_fnn)
    retry_delays = []
    monotonic_times = iter([0.0, 0.1, 1.1])
    monkeypatch.setattr(
        "scripts.run_stability.time.monotonic", lambda: next(monotonic_times)
    )
    monkeypatch.setattr("scripts.run_stability.time.sleep", retry_delays.append)

    def fake_lncli_json(_config, node, args):
        assert (node, args) == ("lnd-a", ["lookupinvoice", "abc"])
        return {"payment_request": "lntb-invoice", "state": "OPEN", "value": "110"}

    monkeypatch.setattr("scripts.run_stability.lncli_json", fake_lncli_json)
    monkeypatch.setattr("scripts.run_stability.pay_lnd_invoice", lambda *_a, **_k: None)
    monkeypatch.setattr("scripts.run_stability.wait_cch_order_status", lambda *_a: None)
    monkeypatch.setattr(
        "scripts.run_stability.wait_fiber_invoice_status", lambda *_a: None
    )
    monkeypatch.setattr(
        "scripts.run_stability.wait_lnd_invoice_settled", lambda *_a: None
    )
    monkeypatch.setattr(
        "scripts.run_stability.cleanup_lnd_to_fiber_invoice",
        lambda *_args: pytest.fail("a recovered flow must not be cleaned up"),
    )
    stages = []

    result = run_flow_lnd_to_fiber(
        config,
        100,
        "tx",
        stage_callback=lambda stage, status, details: stages.append(
            (stage, status, details)
        ),
    )

    expected_args = ["cch", "receive_btc", "--fiber-pay-req", "fiber"]
    assert receive_btc_calls == [("f1", expected_args)] * 3
    assert retry_delays == [1.0, 2.0]
    assert result == {
        "payment_hash": "0xabc",
        "fee_sats": 10,
        "source_amount": 110,
        "destination_amount": 100,
        "receive_btc_attempts": 3,
        "receive_btc_recovered": True,
        "actor_rpc_timeout_recovered": True,
    }
    assert (
        "create_cch_order",
        "PASS",
        {
            "payment_hash": "0xabc",
            "receive_btc_attempts": 3,
            "receive_btc_recovered": True,
            "actor_rpc_timeout_recovered": True,
        },
    ) in stages


def test_startup_recovery_error_is_recoverable():
    error = AssertionError(
        "Error: RPC error (code -32000): "
        "CCH startup recovery is still initializing"
    )

    assert is_recoverable_receive_btc_error(error)


def test_receive_btc_permanent_error_is_not_retried_or_cleaned(monkeypatch):
    config = argparse.Namespace(f1_rpc="f1", wait_timeout=10)
    calls = []

    def fail_permanently(*_args):
        calls.append(1)
        raise AssertionError(
            "Error: RPC error (code -32000): CKB invoice network mismatch"
        )

    monkeypatch.setattr("scripts.run_stability.fnn", fail_permanently)
    monkeypatch.setattr(
        "scripts.run_stability.time.sleep",
        lambda *_args: pytest.fail("permanent errors must not be retried"),
    )
    monkeypatch.setattr(
        "scripts.run_stability.cleanup_lnd_to_fiber_invoice",
        lambda *_args: pytest.fail("a rejected request has no invoice to clean up"),
    )

    with pytest.raises(AssertionError, match="network mismatch"):
        create_or_recover_receive_btc_order(config, "fiber", "0xabc")

    assert len(calls) == 1


def test_receive_btc_retry_deadline_does_not_cancel_ambiguous_creation(monkeypatch):
    config = argparse.Namespace(f1_rpc="f1", wait_timeout=2)
    calls = []

    def timeout(*_args):
        calls.append(1)
        raise AssertionError("Error: RPC error (code -32000): timeout")

    monkeypatch.setattr("scripts.run_stability.fnn", timeout)
    monotonic_times = iter([0.0, 0.5, 1.5, 2.0])
    monkeypatch.setattr(
        "scripts.run_stability.time.monotonic", lambda: next(monotonic_times)
    )
    retry_delays = []
    monkeypatch.setattr("scripts.run_stability.time.sleep", retry_delays.append)
    monkeypatch.setattr(
        "scripts.run_stability.cleanup_lnd_to_fiber_invoice",
        lambda *_args: pytest.fail("ambiguous server recovery must not be canceled"),
    )

    with pytest.raises(AssertionError, match="timeout"):
        create_or_recover_receive_btc_order(config, "fiber", "0xabc")

    assert len(calls) == 3
    assert retry_delays == [1.0, 0.5]


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
        recovered_receive_btc=2,
        recovered_actor_rpc_timeouts=1,
        failed=1,
        rejected=1,
        latencies_ms=[100, 200],
        start_delays_ms=[1, 2],
        errors={"ExampleError": 1, "MaxInflightExceeded": 1},
    )

    summary = build_summary(args, "run", state, 10.0, 11.0, 12.0)

    assert summary["failure_rate"] == 0.2
    assert summary["recovered_receive_btc"] == 2
    assert summary["recovered_actor_rpc_timeouts"] == 1
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

    def fake_flow(config, amount_sats, transaction_name, stage_callback=None):
        del config, amount_sats, transaction_name, stage_callback
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


def test_load_counts_actor_timeout_recovery_before_later_flow_failure(
    monkeypatch, tmp_path
):
    monkeypatch.setattr("scripts.run_stability.log_preflight", lambda *args: None)
    args = argparse.Namespace(
        flow="lnd-to-fiber",
        tps=1.0,
        duration=0.01,
        amount_sats=100,
        max_inflight=1,
        progress_interval=1.0,
        max_failure_rate=0.0,
        min_actor_rpc_timeout_recoveries=1,
    )

    def fake_flow(_config, _amount_sats, _transaction_name, stage_callback=None):
        stage_callback(
            "create_cch_order",
            "PASS",
            {
                "receive_btc_attempts": 2,
                "receive_btc_recovered": True,
                "actor_rpc_timeout_recovered": True,
            },
        )
        raise RuntimeError("later tracker failure")

    writer = JsonlWriter(tmp_path / "details.jsonl")
    try:
        summary = run_load(args, object(), writer, fake_flow)
    finally:
        writer.close()

    assert summary["failed"] == 1
    assert summary["recovered_receive_btc"] == 1
    assert summary["recovered_actor_rpc_timeouts"] == 1
    assert summary["actor_rpc_timeout_coverage_met"] is True
    assert summary["passed"] is False


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

    def fake_flow(config, amount_sats, transaction_name, stage_callback=None):
        nonlocal active, max_active
        del config, amount_sats, transaction_name, stage_callback
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

    def fake_flow(config, amount_sats, transaction_name, stage_callback=None):
        del config, amount_sats, transaction_name, stage_callback
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

    def slow_flow(config, amount_sats, transaction_name, stage_callback=None):
        del config, amount_sats, transaction_name, stage_callback
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
