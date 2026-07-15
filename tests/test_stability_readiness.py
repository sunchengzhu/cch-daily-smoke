import logging
import time

import pytest

from scripts.check_stability_readiness import (
    ReadinessProbeTimeout,
    run_readiness_probe,
)


def test_readiness_probe_runs_static_check_and_one_real_flow(monkeypatch, caplog):
    preflight_calls = []
    flow_calls = []
    monkeypatch.setattr(
        "scripts.check_stability_readiness.log_preflight",
        lambda *args: preflight_calls.append(args),
    )

    def flow(config, amount_sats, transaction_name):
        flow_calls.append((config, amount_sats, transaction_name))
        return {"payment_hash": "0xprobe"}

    config = object()
    with caplog.at_level(logging.INFO, logger="cch-readiness"):
        result = run_readiness_probe(config, "lnd-to-fiber", 100, 1, flow)

    assert result == {"payment_hash": "0xprobe"}
    assert preflight_calls == [(config, "lnd-to-fiber", 100, 1)]
    assert len(flow_calls) == 1
    assert flow_calls[0][:2] == (config, 100)
    assert flow_calls[0][2].startswith("cch-readiness-")
    assert any(record.message.startswith("PROBE PASS ") for record in caplog.records)


def test_readiness_probe_fails_at_complete_flow_deadline(monkeypatch, caplog):
    monkeypatch.setattr(
        "scripts.check_stability_readiness.log_preflight", lambda *args: None
    )

    def slow_flow(_config, _amount_sats, _transaction_name):
        time.sleep(1)
        return {"payment_hash": "too-late"}

    with caplog.at_level(logging.ERROR, logger="cch-readiness"):
        with pytest.raises(ReadinessProbeTimeout, match="timed out after 0.02s"):
            run_readiness_probe(object(), "fiber-to-lnd", 100, 0.02, slow_flow)

    assert any(
        "PROBE FAIL" in record.message and "ReadinessProbeTimeout" in record.message
        for record in caplog.records
    )


def test_readiness_probe_propagates_flow_failure(monkeypatch):
    monkeypatch.setattr(
        "scripts.check_stability_readiness.log_preflight", lambda *args: None
    )

    def failed_flow(_config, _amount_sats, _transaction_name):
        raise RuntimeError("path unavailable")

    with pytest.raises(RuntimeError, match="path unavailable"):
        run_readiness_probe(object(), "fiber-to-lnd", 100, 1, failed_flow)


def test_readiness_probe_stops_before_flow_when_static_check_fails(monkeypatch):
    monkeypatch.setattr(
        "scripts.check_stability_readiness.log_preflight",
        lambda *args: (_ for _ in ()).throw(RuntimeError("pending HTLC")),
    )

    with pytest.raises(RuntimeError, match="pending HTLC"):
        run_readiness_probe(
            object(),
            "lnd-to-fiber",
            100,
            1,
            lambda *_args: pytest.fail("flow must not start"),
        )
