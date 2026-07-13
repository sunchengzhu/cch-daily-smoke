import pytest

from test_cch_daily_smoke import (
    active_lnd_channel,
    assert_balance_delta,
    format_mzbtc,
    print_asset_convention,
    print_flow_summary,
)


def test_flow_summary_prints_before_after_and_change(capsys):
    print_flow_summary(
        path="FLOW 1: fiber2 -> (fiber1/CCH -> lnd-a) -> lnd-b",
        payment_hash="0x1234",
        principal_sats=100,
        cch_fee_sats=10,
        source_paid=format_mzbtc(110),
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
    assert "Principal            : 100 sats ↔ 100 mzBTC units" in output
    assert "Source paid          : 110 mzBTC units" in output
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
        destination_received=format_mzbtc(100),
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


def test_format_mzbtc_uses_integer_units():
    assert format_mzbtc(100) == "100 mzBTC units"
    assert format_mzbtc(100_000_000) == "100,000,000 mzBTC units"


def test_asset_convention_is_printed_once(capsys):
    print_asset_convention()

    output = capsys.readouterr().out
    assert output.strip() == "资产换算（CCH Demo）：1 BTC = 1 mzBTC"


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
