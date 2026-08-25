import json

import pytest

from scripts import send_daily_smoke_report as report


def scenario_json(topology="fiber2 → CCH → lnd-b"):
    return json.dumps(
        {
            "duration_seconds": 65.2,
            "topology": topology,
            "flows": [
                {
                    "direction": "cWBTC → BTC",
                    "paid": "fiber2 paid 110 raw cWBTC",
                    "received": "lnd-b received 100 sats",
                },
                {
                    "direction": "BTC → cWBTC",
                    "paid": "lnd-b paid 110 sats",
                    "received": "fiber2 received 100 raw cWBTC",
                },
            ],
            "fees": {"CCH": "10 sats", "Lightning": "1 sat", "Fiber": "0"},
            "net": {"fiber2": "0 raw cWBTC", "lnd-b": "-1 sat"},
        }
    )


def complete_env():
    return {
        "CCH_REPORT_JOB_RESULT": "success",
        "CCH_REPORT_BRANCH": "codex/report",
        "CCH_REPORT_FIBER_SOURCE": "release",
        "CCH_REPORT_FNN_VERSION": "v0.9.0",
        "CCH_REPORT_FNN_PACKAGE": "fnn-v0.9.0-x86_64-linux.tar.gz",
        "CCH_REPORT_TOTAL_SECONDS": "125.4",
        "CCH_REPORT_CHECKOUT_OUTCOME": "success",
        "CCH_REPORT_PYTHON_OUTCOME": "success",
        "CCH_REPORT_DEPENDENCIES_OUTCOME": "success",
        "CCH_REPORT_UNIT_OUTCOME": "success",
        "CCH_REPORT_AUTH_OUTCOME": "success",
        "CCH_REPORT_FNN_OUTCOME": "success",
        "CCH_REPORT_LIQUIDITY_OUTCOME": "success",
        "CCH_REPORT_CLEANUP_OUTCOME": "success",
        "CCH_REPORT_LOCAL_OUTCOME": "success",
        "CCH_REPORT_DIRECT_OUTCOME": "success",
        "CCH_REPORT_RELAY_OUTCOME": "success",
        "CCH_REPORT_LOCAL_JSON": scenario_json(),
        "CCH_REPORT_DIRECT_JSON": scenario_json(
            "fiber2 → FiberSwap ↔ lnd-d"
        ),
        "CCH_REPORT_RELAY_JSON": scenario_json(
            "fiber2 → FiberSwap ↔ relay LND ↔ lnd-c"
        ),
        "CCH_REPORT_RUN_URL": "https://github.com/example/repo/actions/runs/123",
        "CCH_REPORT_RUN_NUMBER": "42",
        "CCH_REPORT_RUN_ATTEMPT": "2",
        "CCH_REPORT_SHA": "0123456789abcdef",
    }


def fields_by_name(payload):
    return {
        field["name"]: field["value"]
        for field in payload["embeds"][0]["fields"]
    }


def test_success_payload_has_one_green_embed_and_key_details():
    payload = report.payload_from_env(complete_env())
    embed = payload["embeds"][0]
    fields = fields_by_name(payload)

    assert payload["allowed_mentions"] == {"parse": []}
    assert len(payload["embeds"]) == 1
    assert embed["color"] == report.SUCCESS_COLOR
    assert embed["title"] == "✅ CCH Daily Smoke · Testnet · #42"
    assert embed["url"].endswith("/actions/runs/123")
    assert embed["description"] == "3/3 smoke scenarios passed."
    assert "View full run log" not in embed["description"]
    assert "2m 5s" in fields["Run overview"]
    assert "`codex/report`" in fields["Run overview"]
    assert "`0123456`" in fields["Run overview"]
    assert "retry `2`" in fields["Run overview"]
    assert "FNN `v0.9.0`" in fields["Run overview"]
    assert "fnn-v0.9.0-x86_64-linux.tar.gz" in fields["Run overview"]
    assert fields["Preflight"] == (
        "✅ **All passed** · Checkout · Python · Dependencies · Unit tests · "
        "FNN auth · FNN nodes · LND liquidity · Cleanup"
    )


def test_first_attempt_uses_a_short_single_line_ref_summary():
    env = complete_env()
    env["CCH_REPORT_RUN_ATTEMPT"] = "1"

    overview = fields_by_name(report.payload_from_env(env))["Run overview"]
    ref_line = overview.splitlines()[1]

    assert ref_line == "Branch `codex/report` · `0123456`"
    assert "attempt" not in ref_line
    assert "retry" not in ref_line


def test_preflight_keeps_per_stage_status_when_results_are_mixed():
    env = complete_env()
    env["CCH_REPORT_DEPENDENCIES_OUTCOME"] = "failure"
    env["CCH_REPORT_UNIT_OUTCOME"] = "skipped"

    preflight = fields_by_name(report.payload_from_env(env))["Preflight"]

    assert "All passed" not in preflight
    assert "✅ Checkout" in preflight
    assert "❌ Dependencies" in preflight
    assert "⏭️ Unit tests" in preflight


def test_scenario_fields_render_flows_fees_net_and_duration():
    payload = report.payload_from_env(complete_env())
    fields = fields_by_name(payload)
    local = fields["✅ Local CCH"]
    relay = fields["✅ FiberSwap · via relay LND"]

    assert "⏱ 1m 5s" in local
    assert "**Topology:** fiber2 → CCH → lnd-b" in local
    assert "**cWBTC → BTC** · fiber2 paid 110 raw cWBTC" in local
    assert "→ lnd-b received 100 sats" in local
    assert local.startswith("**Passed** · ⏱")
    assert "**Fees:** CCH: 10 sats · Lightning: 1 sat · Fiber: 0" in local
    assert "**Net:** fiber2: 0 raw cWBTC · lnd-b: -1 sat" in local
    assert "relay LND" in relay


def test_failure_and_skipped_steps_are_clear_without_summaries():
    env = complete_env()
    env.update(
        {
            "CCH_REPORT_JOB_RESULT": "failure",
            "CCH_REPORT_DIRECT_OUTCOME": "failure",
            "CCH_REPORT_DIRECT_JSON": "",
            "CCH_REPORT_RELAY_OUTCOME": "skipped",
            "CCH_REPORT_RELAY_JSON": "",
        }
    )

    payload = report.payload_from_env(env)
    embed = payload["embeds"][0]
    fields = fields_by_name(payload)

    assert embed["color"] == report.FAILURE_COLOR
    assert embed["title"].startswith("❌")
    assert "did not pass" in embed["description"]
    assert "**Failed**" in fields["❌ FiberSwap · direct LND"]
    assert "open the run log" in fields["❌ FiberSwap · direct LND"]
    assert "**Skipped**" in fields["⏭️ FiberSwap · via relay LND"]
    assert "earlier step" in fields["⏭️ FiberSwap · via relay LND"]


def test_invalid_scenario_json_does_not_prevent_failure_report():
    env = complete_env()
    env["CCH_REPORT_JOB_RESULT"] = "failure"
    env["CCH_REPORT_LOCAL_OUTCOME"] = "failure"
    env["CCH_REPORT_LOCAL_JSON"] = "{not-json"

    fields = fields_by_name(report.payload_from_env(env))

    assert "invalid report JSON" in fields["❌ Local CCH"]


def test_report_accepts_string_fees_and_net():
    env = complete_env()
    data = json.loads(env["CCH_REPORT_LOCAL_JSON"])
    data["fees"] = "CCH 10 sats; routes 0"
    data["net"] = "principal round trip balanced"
    env["CCH_REPORT_LOCAL_JSON"] = json.dumps(data)

    local = fields_by_name(report.payload_from_env(env))["✅ Local CCH"]

    assert "**Fees:** CCH 10 sats; routes 0" in local
    assert "**Net:** principal round trip balanced" in local


def test_discord_field_and_embed_character_limits_are_enforced():
    env = complete_env()
    env["CCH_REPORT_FNN_PACKAGE"] = "p" * 5000
    env["CCH_REPORT_LOCAL_JSON"] = scenario_json("x" * 5000)
    env["CCH_REPORT_DIRECT_JSON"] = scenario_json("y" * 5000)
    env["CCH_REPORT_RELAY_JSON"] = scenario_json("z" * 5000)

    embed = report.payload_from_env(env)["embeds"][0]
    fields = embed["fields"]
    total = (
        len(embed["title"])
        + len(embed["description"])
        + len(embed["footer"]["text"])
        + sum(len(field["name"]) + len(field["value"]) for field in fields)
    )

    assert all(len(field["name"]) <= 256 for field in fields)
    assert all(len(field["value"]) <= 1024 for field in fields)
    assert total <= 6000


def test_send_discord_webhook_posts_json_with_mentions_disabled(monkeypatch):
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b""

    def fake_urlopen(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr(report.urllib.request, "urlopen", fake_urlopen)
    payload = report.payload_from_env(complete_env())

    report.send_discord_webhook("https://discord.example/webhook", payload)

    request = captured["request"]
    posted = json.loads(request.data.decode("utf-8"))
    assert captured["timeout"] == 20
    assert request.get_method() == "POST"
    assert request.get_header("Content-type") == "application/json"
    assert posted["allowed_mentions"] == {"parse": []}


def test_main_requires_webhook_url(monkeypatch):
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)

    with pytest.raises(SystemExit, match="DISCORD_WEBHOOK_URL is required"):
        report.main()
