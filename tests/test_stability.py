import argparse
import time

from scripts.run_stability import JsonlWriter, RunState, build_summary, percentile, run_load


def test_percentile_interpolates_values():
    assert percentile([], 95) is None
    assert percentile([10], 95) == 10
    assert percentile([10, 20, 30, 40], 50) == 25


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
