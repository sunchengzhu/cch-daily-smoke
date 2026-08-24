import subprocess
from pathlib import Path
from subprocess import CompletedProcess
from types import SimpleNamespace

import pytest
import test_cch_daily_smoke as smoke

from test_cch_daily_smoke import (
    active_lnd_channel,
    assert_balance_delta,
    call_cch_mutation_after_startup,
    cch_initialization_retry_reason,
    format_cwbtc,
    is_retryable_cch_initialization_error,
    print_asset_convention,
    print_flow_summary,
    receive_btc_amounts,
    fnn,
    run_cmd,
)


def test_flow_summary_prints_before_after_and_change(capsys):
    print_flow_summary(
        number=1,
        direction="cWBTC → BTC",
        path=(
            "fiber2 --cWBTC--> fiber1/CCH == CCH swap == "
            "lnd-a --BTC--> lnd-b"
        ),
        payment_hash="0x1234",
        principal_sats=100,
        source_paid="fiber2 paid 110 raw cWBTC",
        destination_received="lnd-b received 100 sats",
        cch_fee_text=(
            "10 raw cWBTC; paid by fiber2, retained by CCH at fiber1/CCH"
        ),
        fiber_route_fee_text=(
            "0 raw cWBTC; direct fiber2 → fiber1/CCH channel, "
            "no intermediary receives a fee"
        ),
        lightning_route_fee_text=(
            "0 sats; direct lnd-a → lnd-b channel, "
            "no intermediary receives a fee"
        ),
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
    assert "FLOW 1 (cWBTC → BTC)" in output
    assert "fiber2 --cWBTC--> fiber1/CCH == CCH swap == lnd-a" in output
    assert "Principal            : 100 sats ↔ 100 raw cWBTC" in output
    assert "WHO PAID             : fiber2 paid 110 raw cWBTC" in output
    assert "WHO RECEIVED         : lnd-b received 100 sats" in output
    assert "CCH service fee     : 10 raw cWBTC" in output
    assert "Fiber route fee     : 0 raw cWBTC" in output
    assert "Lightning route fee : 0 sats" in output
    assert "fiber2" in output and "-110" in output
    assert "fiber1/CCH" in output and "+110" in output
    assert "lnd-a" in output and "-100" in output
    assert "lnd-b" in output and "+100" in output
    assert "Fiber channel:" not in output
    assert "LND channel:" not in output
    assert "LND outpoint:" not in output


def test_flow_summary_prints_channel_details_in_debug_mode(capsys):
    print_flow_summary(
        number=2,
        direction="BTC → cWBTC",
        path="debug flow",
        payment_hash="0x1234",
        principal_sats=100,
        source_paid="lnd-b paid 110 sats",
        destination_received="fiber2 received 100 raw cWBTC",
        cch_fee_text="10 sats; paid by lnd-b, retained by CCH at lnd-a/CCH",
        fiber_route_fee_text="0 raw cWBTC; direct Fiber channel",
        lightning_route_fee_text="0 sats; direct Lightning channel",
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
    assert format_cwbtc(100) == "100 raw cWBTC"
    assert format_cwbtc(100_000_000) == "100,000,000 raw cWBTC"


def test_asset_convention_is_printed_once(capsys):
    print_asset_convention("testnet4")

    output = capsys.readouterr().out
    assert output.strip().splitlines() == [
        "Asset convention: 1 sat BTC = 1 raw cWBTC for this demo",
        "Bitcoin/Lightning network: testnet4 (LND/lncli network=testnet4)",
    ]


def test_wait_lnd_payment_succeeded_returns_actual_route_fee(monkeypatch):
    config = SimpleNamespace(wait_timeout=1)
    calls = []
    payment = {
        "payment_hash": "1234",
        "status": "SUCCEEDED",
        "value_sat": "100",
        "fee_sat": "0",
    }
    def fake_lncli_json(_config, node_name, args):
        calls.append((node_name, args))
        return {"payments": [payment]}

    monkeypatch.setattr(smoke, "lncli_json", fake_lncli_json)
    monkeypatch.setattr(
        smoke,
        "wait_until",
        lambda _description, check, _timeout: check(),
    )

    assert smoke.wait_lnd_payment_succeeded(config, "lnd-a", "0x1234") == payment
    assert calls == [
        (
            "lnd-a",
            [
                "listpayments",
                "--include_incomplete",
                "--max_payments=100",
            ],
        )
    ]


def test_receive_btc_develop_amount_includes_fee():
    assert receive_btc_amounts(110, 100, 10, "develop") == (100, 110)


def test_receive_btc_develop_rejects_principal_only_amount():
    with pytest.raises(AssertionError, match="expected one of \\[110\\]"):
        receive_btc_amounts(100, 100, 10, "develop")


def test_receive_btc_pr_uses_develop_amount_semantics():
    assert receive_btc_amounts(110, 100, 10, "pr") == (100, 110)
    with pytest.raises(AssertionError, match="expected one of \\[110\\]"):
        receive_btc_amounts(100, 100, 10, "pr")


@pytest.mark.parametrize("reported_amount", [100, 110])
def test_receive_btc_release_accepts_old_and_new_amount_semantics(reported_amount):
    assert receive_btc_amounts(reported_amount, 100, 10, "release") == (100, 110)


@pytest.mark.parametrize(
    ("method", "message", "expected_reason"),
    [
        (
            "send_btc",
            "Error: RPC error (code -32000): timeout",
            "legacy actor RPC timeout",
        ),
        (
            "send_btc",
            "Error: RPC error (code -32000): "
            "CCH startup recovery is still initializing",
            "startup recovery in progress",
        ),
        (
            "receive_btc",
            "receive_btc order creation for payment hash abc "
            "is already being recovered",
            "receive_btc order creation is already being recovered",
        ),
    ],
)
def test_cch_initialization_errors_are_retryable(method, message, expected_reason):
    error = AssertionError(message)
    assert cch_initialization_retry_reason(error, method) == expected_reason
    assert is_retryable_cch_initialization_error(error, method)


def test_permanent_cch_error_is_not_retryable():
    error = AssertionError("Error: RPC error (code -32000): invoice network mismatch")
    assert cch_initialization_retry_reason(error, "send_btc") is None
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


def test_timed_out_command_redacts_auth_token_and_output(monkeypatch):
    def time_out(*args, **kwargs):
        raise subprocess.TimeoutExpired(
            args[0],
            kwargs["timeout"],
            output="partial secret-token output",
            stderr="secret-token error",
        )

    monkeypatch.setattr("test_cch_daily_smoke.subprocess.run", time_out)

    with pytest.raises(AssertionError) as error:
        run_cmd(["fnn", "--auth-token", "secret-token", "info"], timeout=1)

    message = str(error.value)
    assert "secret-token" not in message
    assert "--auth-token '***'" in message
    assert "partial *** output" in message
    assert "*** error" in message


def test_fnn_converts_env_token_to_private_temporary_file(monkeypatch):
    observed = {}
    config = SimpleNamespace(fnn_cli="fnn-cli", command_timeout=9)

    monkeypatch.delenv("CCH_SMOKE_FNN_AUTH_TOKEN_FILE", raising=False)
    monkeypatch.setenv("CCH_SMOKE_FNN_AUTH_TOKEN", "secret-token")

    def fake_run_cmd(args, timeout):
        token_file = Path(args[args.index("--auth-token-file") + 1])
        observed.update(
            args=list(args),
            timeout=timeout,
            token_file=token_file,
            token=token_file.read_text(encoding="utf-8"),
            mode=token_file.stat().st_mode & 0o777,
        )
        return '{"ok":true}'

    monkeypatch.setattr(smoke, "run_cmd", fake_run_cmd)

    assert fnn(config, "http://fnn", ["info"]) == {"ok": True}
    assert "--auth-token" not in observed["args"]
    assert "secret-token" not in observed["args"]
    assert observed["token"] == "secret-token"
    assert observed["mode"] == 0o600
    assert observed["timeout"] == 9
    assert not observed["token_file"].exists()


def test_fnn_does_not_forward_raw_token_environment(monkeypatch, tmp_path):
    token_file = tmp_path / "fnn.token"
    token_file.write_text("file-token", encoding="utf-8")
    observed = {}

    monkeypatch.setenv("CCH_SMOKE_FNN_AUTH_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("CCH_SMOKE_FNN_AUTH_TOKEN", "raw-token")
    monkeypatch.setenv("CCH_FIBER_SWAP_FNN_AUTH_TOKEN", "fiber-swap-token")

    def fake_subprocess_run(args, **kwargs):
        observed.update(args=list(args), env=kwargs["env"])
        return CompletedProcess(args=args, returncode=0, stdout='{"ok":true}', stderr="")

    monkeypatch.setattr("test_cch_daily_smoke.subprocess.run", fake_subprocess_run)
    config = SimpleNamespace(fnn_cli="fnn-cli", command_timeout=9)

    assert fnn(config, "http://fnn", ["info"]) == {"ok": True}
    assert observed["args"].count("--auth-token-file") == 1
    assert "--auth-token" not in observed["args"]
    assert "CCH_SMOKE_FNN_AUTH_TOKEN" not in observed["env"]
    assert "CCH_FIBER_SWAP_FNN_AUTH_TOKEN" not in observed["env"]
    assert token_file.exists()
