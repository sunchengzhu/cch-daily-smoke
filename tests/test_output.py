from subprocess import CompletedProcess
from types import SimpleNamespace

import pytest

from test_cch_daily_smoke import (
    active_lnd_channel,
    assert_balance_delta,
    call_cch_mutation_after_startup,
    format_cwbtc,
    is_retryable_cch_initialization_error,
    print_asset_convention,
    print_flow_summary,
    receive_btc_amounts,
    run_cmd,
)


def test_flow_summary_prints_before_after_and_change(capsys):
    print_flow_summary(
        path="FLOW 1: fiber2 -> (fiber1/CCH -> lnd-a) -> lnd-b",
        payment_hash="0x1234",
        principal_sats=100,
        cch_fee_sats=10,
        source_paid=format_cwbtc(110),
        destination_received="100 sats",
        fiber_channel_id="0xfiber",
        fiber_before={"fiber2": 1000, "fiber1_cch": 0},
        fiber_after={"fiber2": 890, "fiber1_cch": 110},
        lnd_before={
            "chan_id": "lnd-channel",
            "channel_point": "tx:0",
            "lnd_a": 500,
            "lnd_b": 100,
        },
        lnd_after={"lnd_a": 400, "lnd_b": 200},
    )

    output = capsys.readouterr().out
    assert "FLOW 1: fiber2 -> (fiber1/CCH -> lnd-a) -> lnd-b" in output
    assert "Principal            : 100 sats ↔ 100 cWBTC units" in output
    assert "Source paid          : 110 cWBTC units" in output
    assert "Destination received : 100 sats" in output
    assert "fiber2" in output and "-110" in output
    assert "fiber1/CCH" in output and "+110" in output
    assert "lnd-a" in output and "-100" in output
    assert "lnd-b" in output and "+100" in output
    assert "Fiber channel:" not in output
    assert "LND channel:" not in output
    assert "LND outpoint:" not in output


def test_flow_summary_prints_channel_details_in_debug_mode(capsys):
    print_flow_summary(
        path="debug flow",
        payment_hash="0x1234",
        principal_sats=100,
        cch_fee_sats=10,
        source_paid="110 sats",
        destination_received=format_cwbtc(100),
        fiber_channel_id="0xfiber",
        fiber_before={"fiber2": 1000, "fiber1_cch": 0},
        fiber_after={"fiber2": 900, "fiber1_cch": 100},
        lnd_before={
            "chan_id": "lnd-channel",
            "channel_point": "tx:0",
            "lnd_a": 500,
            "lnd_b": 100,
        },
        lnd_after={"lnd_a": 600, "lnd_b": 0},
        show_channel_details=True,
    )

    output = capsys.readouterr().out
    assert "Fiber channel: 0xfiber" in output
    assert "LND channel: lnd-channel" in output
    assert "LND outpoint: tx:0" in output


def test_format_cwbtc_uses_integer_units():
    assert format_cwbtc(100) == "100 cWBTC units"
    assert format_cwbtc(100_000_000) == "100,000,000 cWBTC units"


def test_asset_convention_is_printed_once(capsys):
    print_asset_convention()

    output = capsys.readouterr().out
    assert output.strip() == "Asset convention (CCH Demo): 1 BTC = 1 cWBTC"


def test_receive_btc_develop_amount_includes_fee():
    assert receive_btc_amounts(110, 100, 10, "develop") == (100, 110)


def test_receive_btc_develop_rejects_principal_only_amount():
    with pytest.raises(AssertionError, match="expected one of \\[110\\]"):
        receive_btc_amounts(100, 100, 10, "develop")


@pytest.mark.parametrize("reported_amount", [100, 110])
def test_receive_btc_release_accepts_old_and_new_amount_semantics(reported_amount):
    assert receive_btc_amounts(reported_amount, 100, 10, "release") == (100, 110)


@pytest.mark.parametrize(
    ("method", "message"),
    [
        ("send_btc", "Error: RPC error (code -32000): timeout"),
        (
            "send_btc",
            "Error: RPC error (code -32000): "
            "CCH startup recovery is still initializing",
        ),
        (
            "receive_btc",
            "receive_btc order creation for payment hash abc "
            "is already being recovered",
        ),
    ],
)
def test_cch_initialization_errors_are_retryable(method, message):
    assert is_retryable_cch_initialization_error(AssertionError(message), method)


def test_permanent_cch_error_is_not_retryable():
    error = AssertionError("Error: RPC error (code -32000): invoice network mismatch")
    assert not is_retryable_cch_initialization_error(error, "send_btc")


@pytest.mark.parametrize(
    "initial_error",
    [
        "Error: RPC error (code -32000): timeout",
        "CCH startup recovery is still initializing",
    ],
)
def test_cch_mutation_retries_startup_error_with_same_request(
    monkeypatch, initial_error
):
    config = SimpleNamespace(f1_rpc="http://fiber", wait_timeout=10)
    args = ["cch", "send_btc", "--btc-pay-req", "invoice"]
    responses = [
        AssertionError(initial_error),
        {"payment_hash": "0xabc"},
    ]
    calls = []

    def fake_fnn(actual_config, rpc_url, actual_args):
        calls.append((actual_config, rpc_url, actual_args))
        response = responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    monkeypatch.setattr("test_cch_daily_smoke.fnn", fake_fnn)
    monotonic_times = iter([0.0, 0.1])
    monkeypatch.setattr(
        "test_cch_daily_smoke.time.monotonic", lambda: next(monotonic_times)
    )
    retry_delays = []
    monkeypatch.setattr("test_cch_daily_smoke.time.sleep", retry_delays.append)

    assert call_cch_mutation_after_startup(config, args) == {
        "payment_hash": "0xabc"
    }
    assert calls == [(config, config.f1_rpc, args)] * 2
    assert retry_delays == [1.0]


def test_balance_failure_includes_channel_details():
    with pytest.raises(AssertionError, match="fiber_channel_id=0xfiber"):
        assert_balance_delta(
            "fiber2",
            before=1000,
            after=950,
            expected_delta=-100,
            details="fiber_channel_id=0xfiber",
        )


def test_channel_selection_failure_lists_available_channels():
    channels = {
        "channels": [
            {
                "chan_id": "lnd-channel",
                "channel_point": "tx:0",
                "remote_pubkey": "other-peer",
                "active": True,
            }
        ]
    }

    with pytest.raises(pytest.fail.Exception, match="lnd-channel"):
        active_lnd_channel(channels, remote_pubkey="expected-peer")


def test_failed_command_redacts_auth_token(monkeypatch):
    monkeypatch.setattr(
        "test_cch_daily_smoke.subprocess.run",
        lambda *args, **kwargs: CompletedProcess(
            args=args[0], returncode=1, stdout="", stderr="Unauthorized"
        ),
    )

    with pytest.raises(AssertionError) as error:
        run_cmd(["fnn", "--auth-token", "secret-token", "info"], timeout=1)

    assert "secret-token" not in str(error.value)
    assert "--auth-token '***'" in str(error.value)
