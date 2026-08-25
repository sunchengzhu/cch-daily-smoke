import sys
from types import SimpleNamespace

import pytest
import test_fiber_swap_relay_daily_smoke as relay

from test_fiber_swap_daily_smoke import DEFAULT_FIBER_SWAP_LND_PUBKEY
from test_fiber_swap_relay_daily_smoke import (
    DEFAULT_RELAY_CHANNEL_POINT,
    DEFAULT_RELAY_LND_PUBKEY,
    DEFAULT_RELAY_TO_FIBER_SWAP_SCID,
    RelayFiberSwapSmokeConfig,
    create_private_relay_lnd_invoice,
    expected_relay_route,
    format_msat_as_sats,
    print_relay_flow_1_summary,
    print_relay_flow_2_summary,
    relay_hint_fee_msat,
    validate_relay_to_fiber_swap_channel,
    verify_lnd_relay_payment,
    wait_relay_lnd_balance_delta,
    wait_relay_lnd_channel_quiescent,
)


def test_relay_config_uses_lnd_c_and_pinned_route_defaults(monkeypatch):
    monkeypatch.setenv("CCH_FIBER_SWAP_FNN_CLI", sys.executable)
    for name in (
        "CCH_FIBER_SWAP_RELAY_LND_CONTAINER",
        "CCH_FIBER_SWAP_RELAY_LOCAL_LND_PUBKEY",
        "CCH_FIBER_SWAP_RELAY_CHANNEL_POINT",
        "CCH_FIBER_SWAP_RELAY_NODE_PUBKEY",
        "CCH_FIBER_SWAP_RELAY_TO_FIBER_SWAP_SCID",
        "CCH_FIBER_SWAP_RELAY_LND_FEE_LIMIT_SATS",
    ):
        monkeypatch.delenv(name, raising=False)

    config = RelayFiberSwapSmokeConfig.from_env()

    assert config.lnd_container == "lnd-c"
    assert config.lnd_d_pubkey == ""
    assert config.lnd_channel_point == DEFAULT_RELAY_CHANNEL_POINT
    assert config.relay_lnd_pubkey == DEFAULT_RELAY_LND_PUBKEY
    assert config.relay_to_fiber_swap_scid == DEFAULT_RELAY_TO_FIBER_SWAP_SCID


def test_relay_config_rejects_invalid_public_channel_scid(monkeypatch):
    monkeypatch.setenv("CCH_FIBER_SWAP_FNN_CLI", sys.executable)
    monkeypatch.setenv("CCH_FIBER_SWAP_RELAY_TO_FIBER_SWAP_SCID", "bad")

    with pytest.raises(pytest.fail.Exception, match="decimal short channel ID"):
        RelayFiberSwapSmokeConfig.from_env()


def test_expected_relay_route_requires_exact_two_hop_path(monkeypatch):
    calls = []
    wanted = {
        "total_fees": "1",
        "hops": [
            {
                "pub_key": DEFAULT_RELAY_LND_PUBKEY,
                "chan_id": "111",
            },
            {
                "pub_key": DEFAULT_FIBER_SWAP_LND_PUBKEY,
                "chan_id": DEFAULT_RELAY_TO_FIBER_SWAP_SCID,
            },
        ],
    }

    def fake_lncli_json(_config, args):
        calls.append(args)
        return {
            "routes": [
                {
                    "total_fees": "2",
                    "hops": [
                        {
                            "pub_key": "wrong",
                            "chan_id": "999",
                        }
                    ],
                },
                wanted,
            ]
        }

    monkeypatch.setattr(relay, "fiber_swap_lncli_json", fake_lncli_json)
    config = SimpleNamespace(
        lnd_fee_limit_sats=80,
        relay_lnd_pubkey=DEFAULT_RELAY_LND_PUBKEY,
        fiber_swap_lnd_pubkey=DEFAULT_FIBER_SWAP_LND_PUBKEY,
        relay_to_fiber_swap_scid=DEFAULT_RELAY_TO_FIBER_SWAP_SCID,
    )

    assert expected_relay_route(config, 200, "111", {"111", "112"}) == wanted
    assert calls == [
        [
            "queryroutes",
            "--amt=200",
            "--fee_limit=80",
            "--outgoing_chan_id=111",
            DEFAULT_FIBER_SWAP_LND_PUBKEY,
        ]
    ]


def test_expected_relay_route_rejects_zero_fee(monkeypatch):
    monkeypatch.setattr(
        relay,
        "fiber_swap_lncli_json",
        lambda *_args: {
            "routes": [
                {
                    "total_fees": "0",
                    "hops": [
                        {
                            "pub_key": DEFAULT_RELAY_LND_PUBKEY,
                            "chan_id": "111",
                        },
                        {
                            "pub_key": DEFAULT_FIBER_SWAP_LND_PUBKEY,
                            "chan_id": DEFAULT_RELAY_TO_FIBER_SWAP_SCID,
                        },
                    ],
                }
            ]
        },
    )
    config = SimpleNamespace(
        lnd_fee_limit_sats=80,
        relay_lnd_pubkey=DEFAULT_RELAY_LND_PUBKEY,
        fiber_swap_lnd_pubkey=DEFAULT_FIBER_SWAP_LND_PUBKEY,
        relay_to_fiber_swap_scid=DEFAULT_RELAY_TO_FIBER_SWAP_SCID,
    )

    with pytest.raises(AssertionError, match="positive relay fee"):
        expected_relay_route(config, 200, "111", {"111"})


def test_expected_relay_route_rejects_a_sub_sat_fee(monkeypatch):
    route = {
        "total_fees": "0",
        "total_fees_msat": "500",
        "hops": [
            {
                "pub_key": DEFAULT_RELAY_LND_PUBKEY,
                "chan_id": "111",
            },
            {
                "pub_key": DEFAULT_FIBER_SWAP_LND_PUBKEY,
                "chan_id": DEFAULT_RELAY_TO_FIBER_SWAP_SCID,
            },
        ],
    }
    monkeypatch.setattr(
        relay,
        "fiber_swap_lncli_json",
        lambda *_args: {"routes": [route]},
    )
    config = SimpleNamespace(
        lnd_fee_limit_sats=80,
        relay_lnd_pubkey=DEFAULT_RELAY_LND_PUBKEY,
        fiber_swap_lnd_pubkey=DEFAULT_FIBER_SWAP_LND_PUBKEY,
        relay_to_fiber_swap_scid=DEFAULT_RELAY_TO_FIBER_SWAP_SCID,
    )

    with pytest.raises(AssertionError, match="whole-sat Lightning fee"):
        expected_relay_route(config, 200, "111", {"111"})


def test_relay_payment_requires_same_two_hop_route_and_positive_fee():
    config = SimpleNamespace(
        lnd_fee_limit_sats=80,
        relay_lnd_pubkey=DEFAULT_RELAY_LND_PUBKEY,
        fiber_swap_lnd_pubkey=DEFAULT_FIBER_SWAP_LND_PUBKEY,
        relay_to_fiber_swap_scid=DEFAULT_RELAY_TO_FIBER_SWAP_SCID,
    )
    payment = {
        "value_sat": "200",
        "fee_sat": "1",
        "htlcs": [
            {
                "status": "SUCCEEDED",
                "route": {
                    "hops": [
                        {
                            "pub_key": DEFAULT_RELAY_LND_PUBKEY,
                            "chan_id": "111",
                        },
                        {
                            "pub_key": DEFAULT_FIBER_SWAP_LND_PUBKEY,
                            "chan_id": DEFAULT_RELAY_TO_FIBER_SWAP_SCID,
                        },
                    ]
                },
            }
        ],
    }

    assert verify_lnd_relay_payment(config, payment, 200, {"111"}) == (201, 1)

    payment["htlcs"][0]["route"]["hops"][1]["chan_id"] = "999"
    with pytest.raises(AssertionError):
        verify_lnd_relay_payment(config, payment, 200, {"111"})


def test_relay_payment_rejects_a_sub_sat_fee():
    config = SimpleNamespace(
        lnd_fee_limit_sats=80,
        relay_lnd_pubkey=DEFAULT_RELAY_LND_PUBKEY,
        fiber_swap_lnd_pubkey=DEFAULT_FIBER_SWAP_LND_PUBKEY,
        relay_to_fiber_swap_scid=DEFAULT_RELAY_TO_FIBER_SWAP_SCID,
    )
    payment = {
        "value_sat": "200",
        "fee_sat": "0",
        "fee_msat": "500",
        "htlcs": [
            {
                "status": "SUCCEEDED",
                "route": {
                    "hops": [
                        {
                            "pub_key": DEFAULT_RELAY_LND_PUBKEY,
                            "chan_id": "111",
                        },
                        {
                            "pub_key": DEFAULT_FIBER_SWAP_LND_PUBKEY,
                            "chan_id": DEFAULT_RELAY_TO_FIBER_SWAP_SCID,
                        },
                    ]
                },
            }
        ],
    }

    with pytest.raises(AssertionError, match="whole-sat Lightning fee"):
        verify_lnd_relay_payment(config, payment, 200, {"111"})


def test_relay_hint_fee_includes_base_and_proportional_fee():
    hint = {
        "fee_base_msat": "1000",
        "fee_proportional_millionths": "1000",
    }

    assert relay_hint_fee_msat(hint, 200) == 1200


def test_msat_fee_format_is_concise():
    assert format_msat_as_sats(1000) == "1 sat"
    assert format_msat_as_sats(1200) == "1.200 sats"


def test_relay_fee_summaries_distinguish_unknown_and_known_fee_parts(capsys):
    config = SimpleNamespace(lnd_container="lnd-c")
    lnd_before = {
        "channel_point": "funding:1",
        "Local LND": 10_000,
        "Relay LND": 20_000,
    }
    lnd_after = {
        "channel_point": "funding:1",
        "Local LND": 9_799,
        "Relay LND": 20_201,
    }
    fiber_before = {"channel_id": "fiber", "fiber2": 1_000, "Bottle": 500}
    fiber_after = {"channel_id": "fiber", "fiber2": 1_100, "Bottle": 400}

    print_relay_flow_1_summary(
        config,
        payment_hash="0xflow1",
        principal=100,
        cch_fee=100,
        lnd_outflow=201,
        lightning_route_fee=1,
        lnd_before=lnd_before,
        lnd_after=lnd_after,
        fiber_before=fiber_before,
        fiber_after=fiber_after,
    )
    print_relay_flow_2_summary(
        config,
        payment_hash="0xflow2",
        btc_principal=201,
        cch_fee=100,
        fiber_route_fee=1,
        fiber_total=302,
        relay_hint_fee_msat_value=1000,
        lnd_before=lnd_after,
        lnd_after=lnd_before,
        fiber_before=fiber_after,
        fiber_after={"channel_id": "fiber", "fiber2": 798, "Bottle": 702},
    )

    output = capsys.readouterr().out
    assert "Fiber route fee     : amount not exposed by the external API" in output
    assert "1 sat final-hop fee (invoice hint)" in output
    assert "paid by FiberSwap CCH LND; total not exposed" in output


def test_private_relay_invoice_uses_24_hour_expiry_and_relay_route_hint(
    monkeypatch,
):
    calls = []
    monkeypatch.setattr(
        relay,
        "get_relay_lnd_channel",
        lambda _config: {"scid": "111"},
    )

    def fake_lncli_json(_config, args):
        calls.append(args)
        if args[0] == "addinvoice":
            return {"payment_request": "lntb-relay"}
        return {
            "destination": "local-pubkey",
            "num_satoshis": "200",
            "payment_hash": "abcd",
            "route_hints": [
                {
                    "hop_hints": [
                        {
                            "node_id": DEFAULT_RELAY_LND_PUBKEY,
                            "chan_id": "111",
                            "fee_base_msat": "1000",
                            "fee_proportional_millionths": "1",
                        }
                    ]
                }
            ],
        }

    monkeypatch.setattr(relay, "fiber_swap_lncli_json", fake_lncli_json)
    config = SimpleNamespace(relay_lnd_pubkey=DEFAULT_RELAY_LND_PUBKEY)

    pay_req, payment_hash, hints, fee_msat = create_private_relay_lnd_invoice(
        config,
        200,
        "local-pubkey",
    )

    assert pay_req == "lntb-relay"
    assert payment_hash == "0xabcd"
    assert hints[0]["node_id"] == DEFAULT_RELAY_LND_PUBKEY
    assert fee_msat == 1000
    assert "--expiry=86400" in calls[0]
    assert "--private" in calls[0]


def test_public_relay_channel_requires_expected_endpoints_and_enabled_policies(
    monkeypatch,
):
    calls = []
    channel = {
        "channel_id": DEFAULT_RELAY_TO_FIBER_SWAP_SCID,
        "node1_pub": DEFAULT_RELAY_LND_PUBKEY,
        "node2_pub": DEFAULT_FIBER_SWAP_LND_PUBKEY,
        "node1_policy": {"disabled": False},
        "node2_policy": {"disabled": False},
    }
    monkeypatch.setattr(
        relay,
        "fiber_swap_lncli_json",
        lambda _config, args: calls.append(args) or channel,
    )
    config = SimpleNamespace(
        relay_to_fiber_swap_scid=DEFAULT_RELAY_TO_FIBER_SWAP_SCID,
        relay_lnd_pubkey=DEFAULT_RELAY_LND_PUBKEY,
        fiber_swap_lnd_pubkey=DEFAULT_FIBER_SWAP_LND_PUBKEY,
    )

    assert validate_relay_to_fiber_swap_channel(config) == channel
    assert calls == [["getchaninfo", DEFAULT_RELAY_TO_FIBER_SWAP_SCID]]

    channel["node1_policy"]["disabled"] = True
    with pytest.raises(AssertionError, match="disabled node1_policy"):
        validate_relay_to_fiber_swap_channel(config)


def test_wait_relay_channel_retries_a_transient_inactive_state(monkeypatch):
    values = iter(
        [
            AssertionError("temporarily inactive"),
            {"pending_htlcs": [], "active": True},
        ]
    )

    def fake_get_channel(_config):
        value = next(values)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(relay, "get_relay_lnd_channel", fake_get_channel)
    monkeypatch.setattr(relay.time, "sleep", lambda _seconds: None)

    assert wait_relay_lnd_channel_quiescent(
        SimpleNamespace(wait_timeout=0.1)
    ) == {"pending_htlcs": [], "active": True}


def test_relay_lnd_balance_wait_retries_stale_and_pending_snapshots(monkeypatch):
    config = SimpleNamespace(wait_timeout=1)
    before = {
        "channel_point": "funding:1",
        "Local LND": 1_000,
        "Relay LND": 500,
    }
    snapshots = iter(
        [
            {
                "channel_point": "funding:1",
                "local_balance": "1000",
                "remote_balance": "500",
                "pending_htlcs": [],
            },
            {
                "channel_point": "funding:1",
                "local_balance": "799",
                "remote_balance": "701",
                "pending_htlcs": [{"incoming": False}],
            },
            {
                "channel_point": "funding:1",
                "local_balance": "799",
                "remote_balance": "701",
                "pending_htlcs": [],
            },
        ]
    )
    monkeypatch.setattr(
        relay,
        "get_relay_lnd_channel",
        lambda _config: next(snapshots),
    )
    monkeypatch.setattr(relay.time, "sleep", lambda _seconds: None)

    result = wait_relay_lnd_balance_delta(config, before, -201, 201)

    assert result["Local LND"] == 799
    assert result["Relay LND"] == 701
