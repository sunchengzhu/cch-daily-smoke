import logging
import time
from argparse import Namespace

import pytest

from scripts.check_stability_readiness import (
    ReadinessProbeTimeout,
    collect_probe_diagnostics,
    run_readiness_probe,
)


def test_readiness_probe_runs_static_check_and_one_real_flow(monkeypatch, caplog):
    preflight_calls = []
    flow_calls = []
    monkeypatch.setattr(
        "scripts.check_stability_readiness.log_preflight",
        lambda *args: preflight_calls.append(args),
    )

    def flow(config, amount_sats, transaction_name, stage_callback):
        flow_calls.append((config, amount_sats, transaction_name))
        stage_callback("complete_flow", "START", {})
        stage_callback("complete_flow", "PASS", {"payment_hash": "0xprobe"})
        return {"payment_hash": "0xprobe"}

    config = object()
    with caplog.at_level(logging.INFO, logger="cch-readiness"):
        result = run_readiness_probe(config, "lnd-to-fiber", 100, 1, flow)

    assert result == {"payment_hash": "0xprobe"}
    assert preflight_calls == [(config, "lnd-to-fiber", 100, 1)]
    assert len(flow_calls) == 1
    assert flow_calls[0][:2] == (config, 100)
    assert flow_calls[0][2].startswith("cch-readiness-")
    assert any(
        "stage=complete_flow status=PASS" in record.message
        for record in caplog.records
    )
    assert any(record.message.startswith("PROBE PASS ") for record in caplog.records)


def test_readiness_probe_fails_at_complete_flow_deadline(monkeypatch, caplog):
    monkeypatch.setattr(
        "scripts.check_stability_readiness.log_preflight", lambda *args: None
    )

    def slow_flow(_config, _amount_sats, _transaction_name, stage_callback):
        stage_callback("pay_lnd_invoice", "START", {"payment_hash": "0xslow"})
        time.sleep(1)
        return {"payment_hash": "too-late"}

    with caplog.at_level(logging.ERROR, logger="cch-readiness"):
        with pytest.raises(ReadinessProbeTimeout, match="timed out after 0.02s"):
            run_readiness_probe(object(), "fiber-to-lnd", 100, 0.02, slow_flow)

    assert any(
        "PROBE FAIL" in record.message
        and "failed_stage=pay_lnd_invoice" in record.message
        and "ReadinessProbeTimeout" in record.message
        for record in caplog.records
    )


def test_readiness_probe_propagates_flow_failure(monkeypatch):
    monkeypatch.setattr(
        "scripts.check_stability_readiness.log_preflight", lambda *args: None
    )

    def failed_flow(_config, _amount_sats, _transaction_name, stage_callback):
        stage_callback("create_cch_order", "START", {})
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
            lambda *_args, **_kwargs: pytest.fail("flow must not start"),
        )


def test_probe_diagnostics_report_states_and_post_cleanup_htlcs(
    monkeypatch, caplog
):
    config = Namespace(command_timeout=60, f1_rpc="f1", f2_rpc="f2")

    def fake_fnn(_config, rpc, args, timeout):
        assert timeout == 10
        if rpc == "f1":
            assert args[:2] == ["cch", "get_cch_order"]
            return {"status": "Pending"}
        assert args[:2] == ["invoice", "get_invoice"]
        return {"status": "Open"}

    def fake_lncli(_config, node_name, args, timeout):
        assert timeout == 10
        if args[0] == "lookupinvoice":
            assert node_name == "lnd-a"
            return {"state": "CANCELED"}
        return {
            "channels": [
                {"pending_htlcs": []},
                {"pending_htlcs": [{}]} if node_name == "lnd-b" else {},
            ]
        }

    monkeypatch.setattr("scripts.check_stability_readiness.fnn", fake_fnn)
    monkeypatch.setattr("scripts.check_stability_readiness.lncli_json", fake_lncli)

    with caplog.at_level(logging.INFO, logger="cch-readiness"):
        diagnostics = collect_probe_diagnostics(
            config, "lnd-to-fiber", "0xabc"
        )

    assert diagnostics == {
        "payment_hash": "0xabc",
        "cch_order": "Pending",
        "fiber": "Open",
        "lnd-a_invoice": "CANCELED",
        "lnd-a_pending_htlcs": 0,
        "lnd-b_pending_htlcs": 1,
    }
    assert any(record.message.startswith("PROBE DIAG ") for record in caplog.records)
