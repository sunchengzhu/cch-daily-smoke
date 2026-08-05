from types import SimpleNamespace

from scripts import ensure_fiber_liquidity as liquidity


def test_fiber_liquidity_scales_with_one_way_volume():
    minimum, details = liquidity.minimum_fiber1_for_load(
        load_mode="fixed-tps",
        tps=2,
        duration_seconds=300,
        amount_sats=100,
    )

    assert minimum == 66_000
    assert details == {
        "target_transactions": 600,
        "principal_sats": 60_000,
        "headroom_sats": 6_000,
    }


def test_fiber1_top_up_transfers_only_the_shortfall(monkeypatch):
    config = SimpleNamespace(
        f1_rpc="f1",
        f2_rpc="f2",
        currency="Fibt",
        udt_script={"code_hash": "code", "hash_type": "type", "args": "args"},
    )
    channels = iter(
        [
            {
                "channel_id": "0xchannel",
                "local_balance": "0x11170",
                "remote_balance": "0x17fc",
            },
            {
                "channel_id": "0xchannel",
                "local_balance": "0x2ee0",
                "remote_balance": "0x101d0",
            },
        ]
    )
    monkeypatch.setattr(liquidity, "get_fiber_channel", lambda *_a, **_k: next(channels))
    calls = []

    def fake_fnn(_config, rpc, args):
        calls.append((rpc, args))
        if args[:2] == ["invoice", "new_invoice"]:
            return {
                "invoice_address": "invoice",
                "invoice": {"data": {"payment_hash": "0xabc"}},
            }
        return {"payment_hash": "0xabc"}

    monkeypatch.setattr(liquidity, "fnn", fake_fnn)
    waited = []
    monkeypatch.setattr(
        liquidity,
        "wait_fiber_payment_status",
        lambda _config, payment_hash, status: waited.append((payment_hash, status)),
    )

    result = liquidity.top_up_fiber1_if_needed(config, 66_000)

    assert result == {
        "amount_sats": 59_860,
        "previous_fiber1_balance": 6_140,
        "updated_fiber1_balance": 66_000,
    }
    assert calls[0][0] == "f1"
    assert calls[0][1][0:4] == ["invoice", "new_invoice", "--amount", "59860"]
    assert calls[1] == (
        "f2",
        ["payment", "send_payment", "--invoice", "invoice"],
    )
    assert waited == [("0xabc", "Success")]
