import json
from types import SimpleNamespace

import pytest

from smoke_report import SMOKE_REPORT_MARKER, append_flow_summary, emit_smoke_report
from test_cch_daily_smoke import build_cch_smoke_report
from test_fiber_swap_daily_smoke import build_fiber_swap_smoke_report
from test_fiber_swap_relay_daily_smoke import build_relay_fiber_swap_smoke_report


def sample_report():
    return {
        "duration_seconds": 12.34,
        "topology": "payer ↔ CCH ↔ receiver",
        "flows": [
            {
                "direction": "BTC → cWBTC",
                "paid": "payer 201 sats",
                "received": "receiver 100 raw cWBTC",
            }
        ],
        "fees": {"CCH": "100 sats"},
        "net": {"payer": "-201 sats"},
    }


def assert_report_schema(report):
    assert set(report) == {
        "duration_seconds",
        "topology",
        "flows",
        "fees",
        "net",
    }
    assert all(
        set(flow) == {"direction", "paid", "received"}
        for flow in report["flows"]
    )


def test_emit_smoke_report_prints_marker_and_sets_github_output(
    monkeypatch, tmp_path, capsys
):
    github_output = tmp_path / "github-output"
    github_output.write_text("existing=value\n", encoding="utf-8")
    monkeypatch.setenv("GITHUB_OUTPUT", str(github_output))

    payload = emit_smoke_report(sample_report())

    assert json.loads(payload) == sample_report()
    assert ": " not in payload
    assert ", " not in payload
    assert capsys.readouterr().out == f"{SMOKE_REPORT_MARKER}{payload}\n"
    assert github_output.read_text(encoding="utf-8") == (
        f"existing=value\nreport={payload}\n"
    )


def test_emit_smoke_report_rejects_an_incomplete_schema():
    report = sample_report()
    report.pop("net")

    with pytest.raises(ValueError, match="must contain exactly"):
        emit_smoke_report(report)


def test_flow_summary_appends_each_verified_payment(monkeypatch, tmp_path):
    github_summary = tmp_path / "summary.md"
    github_summary.write_text("# Run overview\n\n", encoding="utf-8")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(github_summary))
    fields = {
        "scenario": "Example",
        "direction": "BTC → cWBTC",
        "money_path": (("BTC leg", "payer → CCH"),),
        "paid": "payer paid 201 sats",
        "received": "receiver received 100 raw cWBTC",
        "fees": {"Fiber": "not exposed", "Lightning": "1 sat"},
        "balances": (
            (
                "Channel",
                "sats",
                {"channel_id": "metadata", "payer": 1_000, "payer spendable": 900},
                {"channel_id": "metadata", "payer": 799, "payer spendable": 699},
            ),
        ),
        "assertions": "Payment settled and balance deltas verified.",
    }

    first = append_flow_summary(number=1, payment_hash="0xfirst", **fields)
    assert github_summary.read_text(encoding="utf-8") == (
        "# Run overview\n\n" + first
    )
    second = append_flow_summary(number=2, payment_hash="0xsecond", **fields)

    assert github_summary.read_text(encoding="utf-8") == (
        "# Run overview\n\n" + first + second
    )
    assert "| payer | 1,000 | 799 | -201 |" in first
    assert "metadata" not in first
    assert "spendable" not in first
    assert "**Status:** ✅ Success" in first
    assert "**Payment hash:** 0xfirst" in first


def test_flow_summary_can_render_locally_and_escapes_markdown(monkeypatch):
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)

    markdown = append_flow_summary(
        scenario="Example",
        number=1,
        direction="BTC → cWBTC",
        money_path=(("Path", "payer <script>\n→ recipient"),),
        payment_hash="0x1234",
        paid="payer paid 1 sat",
        received="recipient received 1 sat",
        fees={},
        balances=(("Channel", "sats", {"node|name": 2}, {"node|name": 1}),),
        assertions="Passed",
    )

    assert "payer &lt;script&gt;<br>→ recipient" in markdown
    assert "| node\\|name | 2 | 1 | -1 |" in markdown


def test_local_cch_summary_report_uses_actual_flow_values():
    report = build_cch_smoke_report(
        duration_seconds=12.345,
        amount_sats=100,
        send_fiber_amount=200,
        receive_fiber_amount=100,
        lightning_amount=200,
        send_cch_fee=100,
        receive_cch_fee=100,
        send_fiber_fee=0,
        receive_fiber_fee=0,
        send_lightning_fee=0,
        receive_lightning_fee=0,
    )

    assert_report_schema(report)
    assert report["duration_seconds"] == 12.35
    assert report["flows"][0] == {
        "direction": "cWBTC → BTC",
        "paid": "fiber2 paid 200 raw cWBTC",
        "received": "lnd-b received 100 sats",
    }
    assert report["fees"] == {
        "CCH": "100 raw cWBTC + 100 sats",
        "Lightning": "0 sats",
        "Fiber": "0 raw cWBTC",
    }
    assert report["net"] == {
        "fiber2": "-100 raw cWBTC",
        "lnd-b": "-100 sats",
    }


def direct_flows():
    return (
        {
            "principal": 100,
            "cch_fee": 100,
            "lnd_outflow": 200,
            "lightning_route_fee": 0,
            "fiber_route_fee": None,
            "fiber_before": {"fiber2": 1_000},
            "lnd_before": {"lnd-d": 1_000},
        },
        {
            "btc_principal": 200,
            "cch_fee": 100,
            "fiber_route_fee": 1,
            "lightning_route_fee": 0,
            "fiber_total": 301,
            "fiber_after": {"fiber2": 799},
            "lnd_after": {"lnd-d": 1_000},
        },
    )


def test_direct_fiber_swap_summary_report_includes_fees_and_net():
    flow_1, flow_2 = direct_flows()

    report = build_fiber_swap_smoke_report(20, flow_1, flow_2)

    assert_report_schema(report)
    assert report["flows"][1] == {
        "direction": "cWBTC → BTC",
        "paid": "fiber2 paid 301 raw cWBTC",
        "received": "lnd-d received 200 sats",
    }
    assert report["fees"] == {
        "CCH": "100 sats + 100 raw cWBTC",
        "Lightning": "FLOW 1 0 sats; FLOW 2 0 sats",
        "Fiber": "FLOW 1 not exposed; FLOW 2 1 raw cWBTC",
    }
    assert report["net"] == {
        "fiber2": "-201 raw cWBTC",
        "lnd-d": "+0 sats",
    }


def test_relay_fiber_swap_summary_report_includes_hint_and_relay_net():
    flow_1, flow_2 = direct_flows()
    flow_1.update(
        {
            "lightning_route_fee": 1,
            "lnd_before": {"Local LND": 1_000},
        }
    )
    flow_2.update(
        {
            "lightning_hint_fee_msat": 1_000,
            "lnd_after": {"Local LND": 1_000},
        }
    )

    report = build_relay_fiber_swap_smoke_report(
        SimpleNamespace(lnd_container="lnd-c"),
        30,
        flow_1,
        flow_2,
    )

    assert_report_schema(report)
    assert report["topology"].startswith("lnd-c ↔ relay LND")
    assert report["fees"]["Lightning"] == (
        "FLOW 1 1 sat; FLOW 2 1 sat final-hop hint (total not exposed)"
    )
    assert report["net"] == {
        "fiber2": "-201 raw cWBTC",
        "lnd-c": "+0 sats",
    }
