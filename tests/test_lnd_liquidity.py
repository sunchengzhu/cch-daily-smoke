from types import SimpleNamespace

import test_cch_daily_smoke as smoke
from scripts.ensure_lnd_liquidity import minimum_spendable_for_load


def test_load_liquidity_keeps_base_floor_for_small_run():
    minimum, details = minimum_spendable_for_load(
        base_minimum_sats=1_000_000,
        load_mode="fixed-tps",
        tps=2,
        duration_seconds=300,
        amount_sats=100,
        max_inflight=100,
    )

    assert minimum == 1_000_000
    assert details == {
        "target_transactions": 600,
        "principal_sats": 60_000,
        "headroom_sats": 100_000,
    }


def test_load_liquidity_scales_with_total_one_way_volume():
    minimum, details = minimum_spendable_for_load(
        base_minimum_sats=1_000_000,
        load_mode="fixed-tps",
        tps=5,
        duration_seconds=14_400,
        amount_sats=100,
        max_inflight=100,
    )

    assert minimum == 8_640_000
    assert details == {
        "target_transactions": 72_000,
        "principal_sats": 7_200_000,
        "headroom_sats": 1_440_000,
    }


def test_load_liquidity_accounts_for_large_inflight_window():
    minimum, details = minimum_spendable_for_load(
        base_minimum_sats=1_000_000,
        load_mode="fixed-tps",
        tps=1,
        duration_seconds=10,
        amount_sats=100_000,
        max_inflight=100,
    )

    assert minimum == 11_000_000
    assert details["principal_sats"] == 1_000_000
    assert details["headroom_sats"] == 10_000_000


def test_top_up_includes_channel_reserve_when_balance_is_below_reserve(
    monkeypatch, capsys
):
    config = SimpleNamespace(lnd_topup_sats=5_000)
    calls = {}
    liquidity = iter(
        [
            {
                "local_balance": 100,
                "reserve_sats": 150_000,
                "spendable_sats": 0,
            },
            {
                "local_balance": 150_110,
                "reserve_sats": 150_000,
                "spendable_sats": 110,
            },
        ]
    )

    monkeypatch.setattr(
        smoke,
        "lnd_b_liquidity",
        lambda _config: next(liquidity),
    )

    def add_invoice(_config, node_name, amount_sats, memo):
        calls["invoice"] = (node_name, amount_sats, memo)
        return {"payment_request": "invoice", "r_hash": "hash"}

    monkeypatch.setattr(smoke, "add_lnd_invoice", add_invoice)
    monkeypatch.setattr(
        smoke,
        "pay_lnd_invoice",
        lambda _config, node_name, pay_req: calls.setdefault(
            "payment", (node_name, pay_req)
        ),
    )
    monkeypatch.setattr(
        smoke,
        "wait_lnd_invoice_settled",
        lambda _config, node_name, payment_hash, amount_sats: calls.setdefault(
            "settled", (node_name, payment_hash, amount_sats)
        ),
    )

    result = smoke.top_up_lnd_b_if_needed(config, needed_sats=110)

    assert calls["invoice"][0:2] == ("lnd-b", 150_010)
    assert calls["payment"] == ("lnd-a", "invoice")
    assert calls["settled"] == ("lnd-b", "hash", 150_010)
    assert result == {
        "amount_sats": 150_010,
        "previous_spendable_sats": 0,
        "updated_spendable_sats": 110,
        "reserve_sats": 150_000,
    }
    output = capsys.readouterr().out
    assert "[lnd-topup] insufficient" in output
    assert "transferring 150,010 sats from lnd-a" in output
    assert "[lnd-topup] completed" in output
    assert "spendable=110 sats" in output


def test_top_up_uses_large_configured_buffer(monkeypatch):
    config = SimpleNamespace(lnd_topup_sats=3_000_000)
    liquidity = iter(
        [
            {
                "local_balance": 100,
                "reserve_sats": 150_000,
                "spendable_sats": 0,
            },
            {
                "local_balance": 3_000_100,
                "reserve_sats": 150_000,
                "spendable_sats": 2_850_100,
            },
        ]
    )
    invoice_amounts = []
    monkeypatch.setattr(smoke, "lnd_b_liquidity", lambda _config: next(liquidity))
    monkeypatch.setattr(
        smoke,
        "add_lnd_invoice",
        lambda _config, _node_name, amount_sats, _memo: (
            invoice_amounts.append(amount_sats)
            or {"payment_request": "invoice", "r_hash": "hash"}
        ),
    )
    monkeypatch.setattr(smoke, "pay_lnd_invoice", lambda *_args: None)
    monkeypatch.setattr(smoke, "wait_lnd_invoice_settled", lambda *_args: None)

    result = smoke.top_up_lnd_b_if_needed(config, needed_sats=110)

    assert invoice_amounts == [3_000_000]
    assert result["updated_spendable_sats"] == 2_850_100


def test_top_up_is_skipped_when_spendable_balance_is_enough(monkeypatch):
    config = SimpleNamespace(lnd_topup_sats=5_000)
    monkeypatch.setattr(
        smoke,
        "lnd_b_liquidity",
        lambda _config: {
            "local_balance": 200_000,
            "reserve_sats": 150_000,
            "spendable_sats": 50_000,
        },
    )
    monkeypatch.setattr(
        smoke,
        "add_lnd_invoice",
        lambda *_args: (_ for _ in ()).throw(AssertionError("unexpected top-up")),
    )

    assert smoke.top_up_lnd_b_if_needed(config, needed_sats=110) is None
