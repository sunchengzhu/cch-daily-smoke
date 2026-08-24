import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_fiber_swap_daily_smoke as smoke

from test_fiber_swap_daily_smoke import (
    DEFAULT_BOTTLE_PUBKEY,
    DEFAULT_FIBER_CHANNEL_ID,
    DEFAULT_LND_CHANNEL_POINT,
    DEFAULT_LND_D_PUBKEY,
    DEFAULT_FIBER_SWAP_LND_PUBKEY,
    FiberSwapSmokeConfig,
    active_fiber_tlcs,
    canonical_hash,
    create_swap_order,
    decimal_scid,
    find_lnd_payment,
    invoice_string,
    lnd_balances,
    lnd_channel_route_ids,
    lnd_outgoing_chan_id,
    print_asset_convention,
    print_fiber_swap_topology_key,
    print_flow_1_summary,
    print_flow_2_summary,
    require_cch_fee_within_limit,
    fiber_swap_fnn,
    validate_new_order,
    verify_settled_invoice_channel,
    verify_lnd_direct_payment,
    wait_flow_2_success,
    wait_terminal_status,
)


def test_fiber_swap_context_names_lnd_testnet_as_bitcoin_testnet3(capsys):
    print_asset_convention("testnet")

    assert capsys.readouterr().out.strip().splitlines() == [
        "Asset convention: 1 sat BTC = 1 raw cWBTC for this demo",
        "Bitcoin/Lightning network: testnet3 (LND/lncli network=testnet)",
    ]


def test_fiber_swap_topology_separates_control_and_money_paths(capsys):
    print_fiber_swap_topology_key()

    output = capsys.readouterr().out
    assert "FiberSwap API            : control plane only" in output
    assert "FiberSwap CCH LND        : Lightning-side node" in output
    assert "FiberSwap FNN (CCH node) : Fiber-side node" in output
    assert "Bottle (Fiber trampoline): public Fiber routing node" in output
    assert "two coordinated payments" in output
    assert "funds do not move directly between LND and FNN" in output
    assert "CI → FiberSwap API → FiberSwap FNN (CCH node)" in output
    assert "no funds pass through the API" in output


def test_fiber_swap_config_uses_independent_lnd_d_testnet_defaults(monkeypatch):
    monkeypatch.setenv("CCH_FIBER_SWAP_FNN_CLI", sys.executable)
    for name in (
        "CCH_FIBER_SWAP_LND_D_CONTAINER",
        "CCH_FIBER_SWAP_LND_NETWORK",
        "CCH_FIBER_SWAP_LND_D_PUBKEY",
        "CCH_FIBER_SWAP_LND_REMOTE_PUBKEY",
        "CCH_FIBER_SWAP_LND_CHANNEL_POINT",
        "CCH_FIBER_SWAP_BOTTLE_PUBKEY",
        "CCH_FIBER_SWAP_FIBER_CHANNEL_ID",
        "CCH_FIBER_SWAP_AMOUNT_SATS",
        "CCH_FIBER_SWAP_MAX_CCH_FEE_SATS",
        "CCH_FIBER_SWAP_MAX_FIBER_FEE",
        "CCH_FIBER_SWAP_LND_FEE_LIMIT_SATS",
        "CCH_FIBER_SWAP_PAYMENT_TIMEOUT",
        "CCH_FIBER_SWAP_WAIT_TIMEOUT",
    ):
        monkeypatch.delenv(name, raising=False)

    config = FiberSwapSmokeConfig.from_env()

    assert config.lnd_container == "lnd-d"
    assert config.lnd_network == "testnet"
    assert config.lnd_d_pubkey == DEFAULT_LND_D_PUBKEY
    assert config.fiber_swap_lnd_pubkey == DEFAULT_FIBER_SWAP_LND_PUBKEY
    assert config.lnd_channel_point == DEFAULT_LND_CHANNEL_POINT
    assert config.bottle_pubkey == DEFAULT_BOTTLE_PUBKEY
    assert config.fiber_channel_id == DEFAULT_FIBER_CHANNEL_ID
    assert config.amount_sats == 100
    assert config.max_cch_fee_sats == 100
    assert config.max_fiber_fee == 100
    assert config.lnd_fee_limit_sats == 10
    assert config.payment_timeout == 120
    assert config.wait_timeout == 180


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("ABCDEF", "0xabcdef"),
        ("0xABCDEF", "0xabcdef"),
    ],
)
def test_canonical_hash_normalizes_prefix_and_case(value, expected):
    assert canonical_hash(value) == expected


def test_invoice_string_accepts_current_and_legacy_api_shapes():
    assert invoice_string("lntb...", "Lightning") == "lntb..."
    assert invoice_string({"Lightning": "lntb..."}, "Lightning") == "lntb..."
    assert invoice_string({"Fiber": "fibt..."}, "Fiber") == "fibt..."


def test_invoice_string_rejects_missing_invoice():
    with pytest.raises(AssertionError, match="missing Fiber invoice"):
        invoice_string({"Lightning": "lntb..."}, "Fiber")


def test_create_swap_order_recovers_ambiguous_post_by_payment_hash(
    monkeypatch,
    capsys,
):
    calls = []
    recovered = {
        "payment_hash": "0xabc",
        "outgoing_pay_req": "invoice",
        "status": "Pending",
    }

    def fake_api_json(_config, method, path, payload=None):
        calls.append((method, path, payload))
        if method == "POST":
            raise AssertionError("response lost")
        return recovered

    monkeypatch.setattr(smoke, "api_json", fake_api_json)

    assert create_swap_order(
        SimpleNamespace(),
        "/api/swap/btc-to-ckb",
        {"fiber_pay_req": "invoice"},
        "0xabc",
    ) == recovered
    assert calls == [
        ("POST", "/api/swap/btc-to-ckb", {"fiber_pay_req": "invoice"}),
        ("GET", "/api/order/0xabc", None),
    ]
    assert "recovered the existing order" in capsys.readouterr().out


def test_create_swap_order_preserves_post_error_when_reconciliation_fails(
    monkeypatch,
):
    def fake_api_json(_config, method, _path, _payload=None):
        if method == "POST":
            raise AssertionError("original POST error")
        raise AssertionError("order not found")

    monkeypatch.setattr(smoke, "api_json", fake_api_json)

    with pytest.raises(AssertionError, match="original POST error"):
        create_swap_order(SimpleNamespace(), "/swap", {}, "0xabc")


def test_validate_new_order_binds_invoice_direction_network_and_status():
    valid_order = {
        "payment_hash": "0xabc",
        "outgoing_pay_req": "invoice",
        "direction": "btc-to-ckb",
        "network": "testnet",
        "status": "Pending",
    }
    validate_new_order(
        valid_order,
        "0xabc",
        "invoice",
        "btc-to-ckb",
        "testnet",
    )

    with pytest.raises(AssertionError, match="outgoing invoice"):
        validate_new_order(
            {
                "payment_hash": "0xabc",
                "outgoing_pay_req": "different",
                "direction": "btc-to-ckb",
                "network": "testnet",
                "status": "Pending",
            },
            "0xabc",
            "invoice",
            "btc-to-ckb",
            "testnet",
        )

    with pytest.raises(AssertionError, match="direction mismatch"):
        validate_new_order(
            {**valid_order, "direction": "ckb-to-btc"},
            "0xabc",
            "invoice",
            "btc-to-ckb",
            "testnet",
        )

    with pytest.raises(AssertionError, match="network mismatch"):
        validate_new_order(
            {**valid_order, "network": "mainnet"},
            "0xabc",
            "invoice",
            "btc-to-ckb",
            "testnet",
        )

    with pytest.raises(AssertionError, match="not payable"):
        validate_new_order(
            {
                "payment_hash": "0xabc",
                "outgoing_pay_req": "invoice",
                "direction": "btc-to-ckb",
                "network": "testnet",
                "status": "Failed",
            },
            "0xabc",
            "invoice",
            "btc-to-ckb",
            "testnet",
        )


def test_cch_fee_limit_fails_closed_before_payment():
    config = SimpleNamespace(max_cch_fee_sats=100)

    require_cch_fee_within_limit(config, "BTC → cWBTC", 100)
    with pytest.raises(pytest.fail.Exception, match="refusing to pay"):
        require_cch_fee_within_limit(config, "BTC → cWBTC", 101)


def test_fiber_swap_fnn_prefers_configured_auth_token_file(monkeypatch, tmp_path):
    token_file = tmp_path / "fnn.token"
    token_file.write_text("file-token", encoding="utf-8")
    calls = []

    monkeypatch.setenv("CCH_FIBER_SWAP_FNN_AUTH_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("CCH_FIBER_SWAP_FNN_AUTH_TOKEN", "env-token")
    monkeypatch.setattr(
        smoke,
        "run_cmd",
        lambda args, timeout: calls.append((args, timeout)) or '{"ok":true}',
    )
    config = SimpleNamespace(fnn_cli="fnn-cli", f2_rpc="http://fnn", command_timeout=9)

    assert fiber_swap_fnn(config, ["info"]) == {"ok": True}
    assert calls == [
        (
            [
                "fnn-cli",
                "-u",
                "http://fnn",
                "-o",
                "json",
                "--no-banner",
                "--auth-token-file",
                str(token_file),
                "info",
            ],
            9,
        )
    ]
    assert "env-token" not in calls[0][0]


def test_fiber_swap_fnn_converts_env_token_to_temporary_file(monkeypatch):
    observed = {}

    monkeypatch.delenv("CCH_FIBER_SWAP_FNN_AUTH_TOKEN_FILE", raising=False)
    monkeypatch.delenv("CCH_SMOKE_FNN_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("CCH_FIBER_SWAP_FNN_AUTH_TOKEN", "secret-token")

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
    config = SimpleNamespace(fnn_cli="fnn-cli", f2_rpc="http://fnn", command_timeout=9)

    assert fiber_swap_fnn(config, ["info"]) == {"ok": True}
    assert "--auth-token" not in observed["args"]
    assert "secret-token" not in observed["args"]
    assert observed["token"] == "secret-token"
    assert observed["mode"] == 0o600
    assert observed["timeout"] == 9
    assert not observed["token_file"].exists()


def test_fiber_swap_fnn_removes_temporary_auth_file_after_failure(monkeypatch):
    observed = {}

    monkeypatch.delenv("CCH_FIBER_SWAP_FNN_AUTH_TOKEN_FILE", raising=False)
    monkeypatch.delenv("CCH_SMOKE_FNN_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("CCH_FIBER_SWAP_FNN_AUTH_TOKEN", "secret-token")

    def fake_run_cmd(args, timeout):
        token_file = Path(args[args.index("--auth-token-file") + 1])
        observed["token_file"] = token_file
        assert token_file.read_text(encoding="utf-8") == "secret-token"
        raise AssertionError("RPC failed")

    monkeypatch.setattr(smoke, "run_cmd", fake_run_cmd)
    config = SimpleNamespace(fnn_cli="fnn-cli", f2_rpc="http://fnn", command_timeout=9)

    with pytest.raises(AssertionError, match="RPC failed"):
        fiber_swap_fnn(config, ["info"])

    assert not observed["token_file"].exists()


def test_terminal_fiber_tlc_history_is_not_treated_as_inflight():
    channel = {
        "pending_tlcs": [
            {"id": "0x1", "status": {"Outbound": "RemoveAckConfirmed"}},
            {"id": "0x2", "status": {"Outbound": "Committed"}},
        ]
    }

    assert active_fiber_tlcs(channel) == [channel["pending_tlcs"][1]]


def test_wait_terminal_status_returns_success_immediately():
    result = wait_terminal_status(
        "order",
        lambda: {"status": "Success"},
        success="Success",
        failures={"Failed"},
        timeout=0.1,
        interval=0,
    )

    assert result == {"status": "Success"}


def test_wait_terminal_status_fails_fast_on_terminal_error():
    with pytest.raises(AssertionError, match="terminal failure Failed"):
        wait_terminal_status(
            "order",
            lambda: {"status": "Failed", "error": "route"},
            success="Success",
            failures={"Failed"},
            timeout=0.1,
            interval=0,
        )


def test_wait_terminal_status_retries_a_transient_read_error():
    values = iter([AssertionError("temporary 502"), {"status": "Success"}])

    def load():
        value = next(values)
        if isinstance(value, Exception):
            raise value
        return value

    assert wait_terminal_status(
        "order",
        load,
        success="Success",
        failures={"Failed"},
        timeout=0.1,
        interval=0,
    ) == {"status": "Success"}


def test_find_lnd_payment_limits_results_and_keeps_route_hops(monkeypatch):
    calls = []
    payment = {
        "payment_hash": "1234",
        "status": "SUCCEEDED",
        "htlcs": [{"route": {"hops": [{"chan_id": "42"}]}}],
    }

    def fake_lncli_json(_config, args):
        calls.append(args)
        return {"payments": [payment]}

    monkeypatch.setattr(smoke, "fiber_swap_lncli_json", fake_lncli_json)

    assert find_lnd_payment(SimpleNamespace(), "0x1234") == payment
    assert calls == [
        ["listpayments", "--include_incomplete", "--max_payments=100"]
    ]


def test_lnd_channel_route_ids_include_confirmed_and_alias_scids():
    assert lnd_channel_route_ids(
        {
            "scid": "10",
            "chan_id": "357cf3d52ef83675c997eb57a95f92373a1b03ae62f5a17c1b0b96d70dfdfd71",
            "alias_scids": [11, "12"],
            "zero_conf_confirmed_scid": "13",
            "peer_scid_alias": "14",
        }
    ) == {"10", "11", "12", "13", "14"}


def test_lnd_outgoing_chan_id_uses_new_lncli_scid_field():
    assert lnd_outgoing_chan_id(
        {
            "scid": "5637243394608660481",
            "chan_id": "357cf3d52ef83675c997eb57a95f92373a1b03ae62f5a17c1b0b96d70dfdfd71",
        }
    ) == (
        "5637243394608660481"
    )


def test_lnd_outgoing_chan_id_supports_legacy_decimal_chan_id():
    assert lnd_outgoing_chan_id({"chan_id": "123"}) == "123"


def test_lnd_balances_preserves_missing_legacy_scid(monkeypatch):
    monkeypatch.setattr(
        smoke,
        "wait_lnd_channel_quiescent",
        lambda _config: {
            "chan_id": "123",
            "channel_point": "funding:0",
            "local_balance": "1000",
            "remote_balance": "500",
        },
    )

    balances = lnd_balances(SimpleNamespace())

    assert "scid" not in balances
    assert lnd_outgoing_chan_id(balances) == "123"


@pytest.mark.parametrize("bad_scid", ["", "0", "not-a-scid", str(2**64)])
def test_lnd_outgoing_chan_id_fails_closed_on_invalid_scid(bad_scid):
    with pytest.raises(AssertionError, match="valid decimal scid"):
        lnd_outgoing_chan_id({"scid": bad_scid, "chan_id": "123"})


def test_decimal_scid_rejects_bolt02_channel_id():
    assert decimal_scid("357cf3d52ef83675c997eb57a95f9237") is None


def test_flow_2_polling_reports_order_failure_without_waiting_for_fiber(
    monkeypatch,
):
    config = SimpleNamespace(wait_timeout=0.1)
    monkeypatch.setattr(
        smoke,
        "fiber_swap_fnn",
        lambda *_args, **_kwargs: {
            "status": "Inflight",
            "failed_error": None,
        },
    )
    monkeypatch.setattr(
        smoke,
        "api_json",
        lambda *_args, **_kwargs: {"status": "Failed"},
    )
    monkeypatch.setattr(
        smoke,
        "fiber_swap_lncli_json",
        lambda *_args, **_kwargs: {"state": "OPEN", "amt_paid_sat": "0"},
    )

    with pytest.raises(AssertionError, match="terminal failure"):
        wait_flow_2_success(config, "0x01", 100)


def test_direct_lnd_payment_requires_one_hop_and_zero_route_fee(monkeypatch):
    config = SimpleNamespace(fiber_swap_lnd_pubkey=DEFAULT_FIBER_SWAP_LND_PUBKEY)
    monkeypatch.setattr(smoke, "get_lnd_channel", lambda _config: {"chan_id": "123"})
    payment = {
        "value_sat": "200",
        "fee_sat": "0",
        "htlcs": [
            {
                "status": "SUCCEEDED",
                "route": {
                    "hops": [
                        {
                            "pub_key": DEFAULT_FIBER_SWAP_LND_PUBKEY,
                            "chan_id": "123",
                        }
                    ]
                },
            }
        ],
    }

    assert verify_lnd_direct_payment(config, payment, 200) == (200, 0)


def test_settled_lnd_invoice_requires_all_parts_on_configured_channel():
    invoice = {
        "htlcs": [
            {"state": "SETTLED", "chan_id": "123", "amt_msat": "50000"},
            {"state": "SETTLED", "chan_id": "123", "amt_msat": "50000"},
        ]
    }
    verify_settled_invoice_channel(invoice, {"123"}, 100)

    invoice["htlcs"][1]["chan_id"] = "456"
    with pytest.raises(AssertionError, match="other than the configured"):
        verify_settled_invoice_channel(invoice, {"123"}, 100)


def test_settled_lnd_invoice_requires_full_principal_amount():
    invoice = {
        "htlcs": [
            {"state": "SETTLED", "chan_id": "123", "amt_msat": "99000"},
        ]
    }

    with pytest.raises(AssertionError, match="settled amount"):
        verify_settled_invoice_channel(invoice, {"123"}, 100)


def test_flow_summaries_explain_paths_balances_and_fee_owners(capsys):
    lnd_before = {
        "chan_id": "channel",
        "channel_point": "tx:1",
        "lnd-d": 10_000,
        "FiberSwap CCH LND": 5_000,
        "lnd-d spendable": 9_000,
        "FiberSwap CCH LND spendable": 4_000,
    }
    lnd_after_flow_1 = {
        **lnd_before,
        "lnd-d": 9_800,
        "FiberSwap CCH LND": 5_200,
    }
    fiber_before = {"channel_id": "fiber", "fiber2": 1_000, "Bottle": 500}
    fiber_after_flow_1 = {"channel_id": "fiber", "fiber2": 1_100, "Bottle": 400}

    print_flow_1_summary(
        payment_hash="0xflow1",
        principal=100,
        cch_fee=100,
        lnd_outflow=200,
        lightning_route_fee=0,
        lnd_before=lnd_before,
        lnd_after=lnd_after_flow_1,
        fiber_before=fiber_before,
        fiber_after=fiber_after_flow_1,
    )
    print_flow_2_summary(
        payment_hash="0xflow2",
        btc_principal=200,
        cch_fee=100,
        fiber_route_fee=1,
        fiber_total=301,
        lnd_before=lnd_after_flow_1,
        lnd_after=lnd_before,
        fiber_before=fiber_after_flow_1,
        fiber_after={"channel_id": "fiber", "fiber2": 799, "Bottle": 701},
    )

    output = capsys.readouterr().out
    assert " FLOW 1 · BTC → cWBTC ".center(100, "─") in output
    assert "lnd-d --BTC--> FiberSwap CCH LND" in output
    assert "after its LND receives BTC" in output
    assert "FiberSwap CCH tells its FNN to pay cWBTC" in output
    assert "FiberSwap FNN (CCH node) --cWBTC-->" in output
    assert "Bottle (Fiber trampoline) --cWBTC--> fiber2" in output
    assert "lnd-d paid 200 sats" in output
    assert "received by the FiberSwap CCH service" in output
    assert "Lightning route fee : 0 sats" in output
    assert " FLOW 1 COMPLETE ".center(100, "─") in output
    assert " FLOW 2 · cWBTC → BTC ".center(100, "═") in output
    assert "fiber2 --cWBTC--> Bottle (Fiber trampoline) --cWBTC-->" in output
    assert "after its FNN receives cWBTC" in output
    assert "FiberSwap CCH tells its LND to pay BTC" in output
    assert "FiberSwap CCH LND --BTC--> lnd-d" in output
    assert "fiber2 paid 301 raw cWBTC" in output
    assert "Fiber route fee     : 1 raw cWBTC" in output
    assert "Bottle is the first trampoline hop" in output
    assert "does not expose its final allocation" in output
    assert " FLOW 2 COMPLETE ".center(100, "═") in output
    assert "Before" in output and "After" in output and "Change" in output
