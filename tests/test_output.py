from test_cch_daily_smoke import print_flow_summary


def test_flow_summary_prints_before_after_and_change(capsys):
    print_flow_summary(
        path="FLOW 1: fiber2 -> (fiber1/CCH -> lnd-a) -> lnd-b",
        payment_hash="0x1234",
        principal_sats=100,
        cch_fee_sats=10,
        source_paid="110 mzBTC min units",
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
    assert "Source paid          : 110 mzBTC min units" in output
    assert "Destination received : 100 sats" in output
    assert "fiber2" in output and "-110" in output
    assert "fiber1/CCH" in output and "+110" in output
    assert "lnd-a" in output and "-100" in output
    assert "lnd-b" in output and "+100" in output
