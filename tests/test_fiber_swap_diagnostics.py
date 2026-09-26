import io
import json
import urllib.error
from types import SimpleNamespace

import pytest

import test_fiber_swap_daily_smoke as smoke
from smoke_failure_class import emit_failure_diagnostic, failure_phase


def test_nested_phase_preserves_pytest_failure_and_innermost_operation():
    original = pytest.fail.Exception("wrong channel")
    with pytest.raises(pytest.fail.Exception) as caught:
        with failure_phase("flow1"):
            with failure_phase("preflight.direct-fiberswap-link"):
                raise original
    assert caught.value is original
    assert original.smoke_phase == "preflight.direct-fiberswap-link"
    assert original.smoke_phases == ["preflight.direct-fiberswap-link", "flow1"]


@pytest.mark.parametrize("status", [400, 503])
def test_api_http_error_reports_actual_status_without_blanket_product_blame(monkeypatch, status):
    config = SimpleNamespace(api_base_url="https://swap.invalid", command_timeout=1)
    source = urllib.error.HTTPError(
        "https://swap.invalid/api/order/hash", status, "error", {}, io.BytesIO(b"upstream response")
    )

    def fail(*args, **kwargs):
        raise source

    monkeypatch.setattr(smoke.urllib.request, "urlopen", fail)
    with pytest.raises(AssertionError) as caught:
        smoke.api_json(config, "GET", "/api/order/hash")
    diagnostic = smoke.build_fiber_swap_failure_diagnostic(config, caught.value)
    assert diagnostic["scope"] == "fiberswap-api"
    assert str(status) in diagnostic["summary"]
    assert "/api/order/hash" in diagnostic["evidence"][0]
    assert caught.value.__cause__ is source
    if status == 503:
        assert "Server error observed" in diagnostic["fiberswap_assessment"]
    else:
        assert "request/configuration" in diagnostic["fiberswap_assessment"]


def test_api_timeout_does_not_claim_remote_service_fault(monkeypatch):
    config = SimpleNamespace(api_base_url="https://swap.invalid", command_timeout=1)

    def timeout(*args, **kwargs):
        raise urllib.error.URLError("timed out")

    monkeypatch.setattr(smoke.urllib.request, "urlopen", timeout)
    with pytest.raises(AssertionError) as caught:
        smoke.api_json(config, "GET", "/api/order/hash")
    diagnostic = smoke.build_fiber_swap_failure_diagnostic(config, caught.value)
    assert diagnostic["scope"] == "runner-fiberswap-api"
    assert diagnostic["fiberswap_assessment"].startswith("Unknown")
    assert "network" in diagnostic["fiberswap_assessment"]


def test_flow2_reports_all_participant_states_and_remote_order_failure(monkeypatch):
    config = SimpleNamespace(wait_timeout=1)
    monkeypatch.setattr(smoke, "fiber_swap_fnn", lambda *_: {"status": "Success"})
    monkeypatch.setattr(smoke, "api_json", lambda *_: {"status": "Failed"})
    monkeypatch.setattr(smoke, "fiber_swap_lncli_json", lambda *_: {"state": "OPEN"})
    with pytest.raises(AssertionError) as caught:
        smoke.wait_flow_2_success(config, "0xabc", 100)
    diagnostic = smoke.build_fiber_swap_failure_diagnostic(config, caught.value)
    observations = json.loads(diagnostic["evidence"][0])
    assert observations["payment_hash"] == "0xabc"
    assert observations["fiber"]["status"] == "Success"
    assert observations["order"]["status"] == "Failed"
    assert observations["lnd-d"]["state"] == "OPEN"
    assert "FiberSwap settlement path" in diagnostic["fiberswap_assessment"]
    assert "not established" in diagnostic["fiberswap_assessment"]


def test_direct_link_diagnostic_is_bounded_and_does_not_assign_endpoint_blame(monkeypatch):
    config = SimpleNamespace(lnd_channel_point="point:1", fiber_swap_lnd_pubkey="swap")
    error = AssertionError("channel inactive")
    error.smoke_phase = "preflight.direct-fiberswap-link"
    error.smoke_phases = [error.smoke_phase]
    calls = []

    def probe(_config, args, timeout):
        calls.append((args, timeout))
        if args == ["listpeers"]:
            return {"peers": []}
        return {"channels": [{"channel_point": "point:1", "active": False}]}

    monkeypatch.setattr(smoke, "fiber_swap_lncli_json", probe)
    diagnostic = smoke.build_fiber_swap_failure_diagnostic(config, error)
    assert calls == [(["listpeers"], 5), (["listchannels"], 5)]
    assert diagnostic["scope"] == "local-fiberswap-link"
    assert diagnostic["fiberswap_assessment"].startswith("Unknown")
    assert '"active": false' in diagnostic["evidence"][-1]


def test_direct_entry_emits_diagnostic_and_keeps_original_pytest_failure(monkeypatch, tmp_path):
    output = tmp_path / "outputs"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    config = SimpleNamespace()
    original = pytest.fail.Exception("balance mismatch")
    monkeypatch.setattr(smoke.FiberSwapSmokeConfig, "from_env", lambda: config)

    def fail(*_args):
        with failure_phase("flow1"):
            raise original

    monkeypatch.setattr(smoke, "_run_direct_smoke", fail)
    with pytest.raises(pytest.fail.Exception) as caught:
        smoke.test_fiber_swap_bidirectional()
    assert caught.value is original
    text = output.read_text()
    assert text.startswith("failure_diagnostic=")
    diagnostic = json.loads(text.split("=", 1)[1])
    assert diagnostic["phase"] == "flow1"
    assert "balance mismatch" in diagnostic["evidence"][0]
    assert "failure_class=" not in text


def test_diagnostic_write_failure_does_not_replace_original_error(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path))
    emit_failure_diagnostic({"summary": "failed\nnext line", "evidence": []})
    assert "Could not publish failure diagnostic" in capsys.readouterr().out


def test_direct_diagnostic_pytest_failure_does_not_mask_original(monkeypatch):
    original = AssertionError("original settlement failure")
    monkeypatch.setattr(smoke.FiberSwapSmokeConfig, "from_env", lambda: SimpleNamespace())

    def fail(*_args):
        raise original

    def diagnostic_failure(*_args):
        pytest.fail("diagnostic probe failed")

    monkeypatch.setattr(smoke, "_run_direct_smoke", fail)
    monkeypatch.setattr(smoke, "build_fiber_swap_failure_diagnostic", diagnostic_failure)
    with pytest.raises(AssertionError) as caught:
        smoke.test_fiber_swap_bidirectional()
    assert caught.value is original


def test_diagnostic_output_is_one_line_json(monkeypatch, tmp_path):
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    value = {"summary": "异常\nnext line", "evidence": ["a\nb"]}
    emit_failure_diagnostic(value)
    lines = output.read_text().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0].split("=", 1)[1]) == value


@pytest.mark.parametrize('upstream', [True, False])
def test_http400_upstream_fnn_liquidity_is_explicitly_attributed(monkeypatch, upstream):
    body = json.dumps({
        'error': 'FNN RPC error [-32000]: Failed to build route, Insufficient balance: max outbound liquidity 0 is insufficient, required amount: 100',
        'upstream': upstream,
    }).encode()
    source = urllib.error.HTTPError('https://swap.invalid/api/swap/btc-to-ckb', 400, 'error', {}, io.BytesIO(body))
    def fail(*_args, **_kwargs):
        raise source
    monkeypatch.setattr(smoke.urllib.request, 'urlopen', fail)
    config = SimpleNamespace(api_base_url='https://swap.invalid', command_timeout=1)
    with pytest.raises(AssertionError) as caught:
        smoke.api_json(config, 'POST', '/api/swap/btc-to-ckb', {'fiber_pay_req': 'invoice'})
    diagnosis = smoke.build_fiber_swap_failure_diagnostic(config, caught.value)
    if upstream:
        assert diagnosis['scope'] == 'fiberswap-fnn'
        assert 'insufficient outbound liquidity' in diagnosis['summary']
        assert 'not yet established' in diagnosis['fiberswap_assessment']
        assert 'pending TLCs' in diagnosis['next_action']
    else:
        assert diagnosis['scope'] == 'fiberswap-api'
