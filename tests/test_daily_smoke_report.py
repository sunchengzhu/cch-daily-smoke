import html
import json
import re
from datetime import datetime
from unittest import mock

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


def github_summary(env):
    return report.build_github_summary(report.collect_report(env))


def summary_text(env):
    """Compare visible values independently of Markdown escaping."""
    return re.sub(r"\\([\\|*_\[\]])", r"\1", html.unescape(github_summary(env)))


def test_success_payload_has_one_compact_green_embed_and_full_report_link():
    payload = report.payload_from_env(complete_env())
    embed = payload["embeds"][0]
    fields = fields_by_name(payload)

    assert payload["allowed_mentions"] == {"parse": []}
    assert len(payload["embeds"]) == 1
    assert embed["color"] == report.SUCCESS_COLOR
    assert embed["title"] == "✅ CCH Daily Smoke · Testnet · #42"
    assert embed["url"].endswith("/actions/runs/123")
    assert embed["description"] == "3/3 smoke scenarios passed. · ⏱ 2m 5s"
    assert set(fields) == {"Run overview", "Checks", "Scenarios"}
    assert any("retry" in line.lower() and "2" in line for line in fields["Run overview"].splitlines())
    assert "FNN `v0.9.0`" in fields["Run overview"]
    assert "release" in fields["Run overview"]
    assert fields["Checks"] == "✅ All checks passed"
    assert embed["footer"]["text"] == "Click the title for the full report and logs"
    rendered = json.dumps(payload, ensure_ascii=False)
    for detail in (
        "codex/report", "0123456", "fnn-v0.9.0-x86_64-linux.tar.gz",
        "fiber2 paid", "lnd-b received", "raw cWBTC", "Topology", "Net:",
    ):
        assert detail not in rendered


def test_first_attempt_does_not_add_retry_or_repository_metadata():
    env = complete_env()
    env["CCH_REPORT_RUN_ATTEMPT"] = "1"

    overview = fields_by_name(report.payload_from_env(env))["Run overview"]
    assert "retry" not in overview
    assert "Branch" not in overview
    assert "0123456" not in overview


@pytest.mark.parametrize(
    ("started", "trigger", "expected"),
    [
        ("2026-09-12T07:10:03+00:00", "schedule", "15:10:03 CST · Delay 5h 10m 3s · GitHub fallback"),
        ("2026-09-12T02:00:04+00:00", "workflow_dispatch", "10:00:04 CST · Delay 4s · workflow_dispatch/API"),
        ("2026-09-12T01:59:59+00:00", "workflow_dispatch", "09:59:59 CST · Delay 0s · workflow_dispatch/API"),
        ("2026-09-13T02:00:00+00:00", "schedule", "10:00:00 CST · Delay 24h 0m 0s · GitHub fallback"),
    ],
)
def test_schedule_details_stay_in_ci_while_discord_shows_actual_start(started, trigger, expected):
    env = complete_env()
    env.update({
        "CCH_REPORT_SCHEDULED_DATE": "2026-09-12",
        "CCH_REPORT_STARTED_AT": str(int(datetime.fromisoformat(started).timestamp())),
        "CCH_REPORT_TRIGGER": trigger,
    })

    fields = fields_by_name(report.payload_from_env(env))

    assert "Scheduled 2026-09-12 10:00 CST · Actual start " + expected in summary_text(env)
    assert expected.split(" · Delay")[0] in fields["Run overview"]
    assert "Delay" not in fields["Run overview"]
    assert "Scheduled 2026-09-12 10:00" not in fields["Run overview"]


def test_absent_schedule_metadata_still_renders_a_compact_overview():
    overview = fields_by_name(report.payload_from_env(complete_env()))["Run overview"]

    assert "release" in overview
    assert "FNN `v0.9.0`" in overview
    assert "Delay" not in overview
    assert len(overview.splitlines()) <= 4


@pytest.mark.parametrize(
    ("day", "started"),
    [
        ("", "1789178400"), ("2026-09-12", ""),
        ("2026-02-30", "1789178400"), ("20260912", "1789178400"),
        ("2026-9-12", "1789178400"), (" 2026-09-12", "1789178400"),
        ("0000-09-12", "1789178400"), ("2026-09-12", "nan"),
        ("2026-09-12", "inf"), ("2026-09-12", "1789178400.5"),
        ("2026-09-12", "-1"), ("2026-09-12", "999999999999"),
        ("2026-09-12", "1" * 5000),
    ],
)
def test_invalid_schedule_metadata_does_not_invent_schedule_details(day, started):
    env = complete_env()
    env.update({"CCH_REPORT_SCHEDULED_DATE": day, "CCH_REPORT_STARTED_AT": started})

    overview = fields_by_name(report.payload_from_env(env))["Run overview"]

    assert "FNN `v0.9.0`" in overview
    assert "Delay" not in overview
    assert "Scheduled 2026" not in github_summary(env)


@pytest.mark.parametrize("trigger,label", [("schedule", "Scheduled"), ("workflow_dispatch", "Manual")])
def test_valid_actual_start_is_shown_without_scheduled_date(trigger, label):
    env = complete_env()
    env.update({
        "CCH_REPORT_STARTED_AT": str(int(datetime.fromisoformat("2026-09-16T03:56:23+00:00").timestamp())),
        "CCH_REPORT_TRIGGER": trigger,
    })

    overview = fields_by_name(report.payload_from_env(env))["Run overview"]

    assert label in overview
    assert "2026-09-16 11:56:23 CST" in overview


def test_schedule_gate_failure_is_visible_before_other_preflight_steps():
    env = complete_env()
    env["CCH_REPORT_GATE_OUTCOME"] = "failure"

    preflight = fields_by_name(report.payload_from_env(env))["Checks"]

    assert preflight.startswith("❌ Schedule gate")
    assert "Checkout" not in preflight
    assert "All checks passed" not in preflight
    assert "Schedule gate" in github_summary(env)


def test_discord_shows_only_failed_or_skipped_checks_and_ci_keeps_all_stages():
    env = complete_env()
    env["CCH_REPORT_DEPENDENCIES_OUTCOME"] = "failure"
    env["CCH_REPORT_UNIT_OUTCOME"] = "skipped"

    preflight = fields_by_name(report.payload_from_env(env))["Checks"]

    assert "All checks passed" not in preflight
    assert "Checkout" not in preflight
    assert "❌ Dependencies" in preflight
    assert "⏭️ Unit tests" in preflight
    summary = summary_text(env)
    for label in ("Checkout", "Python", "Dependencies", "Unit tests", "FNN auth", "FNN nodes", "LND liquidity", "Cleanup"):
        assert label in summary


def test_discord_scenarios_are_three_status_and_duration_lines():
    payload = report.payload_from_env(complete_env())
    fields = fields_by_name(payload)
    lines = fields["Scenarios"].splitlines()

    assert len(lines) == 3
    for line, (_key, label) in zip(lines, report.SCENARIOS):
        assert label in line
        assert "✅" in line
        assert "Passed" in line
        assert "1m 5s" in line
        assert "paid" not in line
        assert "fees" not in line.lower()


def test_ci_summary_preserves_full_run_and_scenario_details():
    env = complete_env()
    summary = summary_text(env)

    for value in (
        "codex/report", "0123456789abcdef", "fnn-v0.9.0-x86_64-linux.tar.gz",
        "v0.9.0", "release", "/actions/runs/123", "2m 5s",
    ):
        assert value in summary
    for key, label in report.SCENARIOS:
        assert label in summary
        data = json.loads(env[f"CCH_REPORT_{key.upper()}_JSON"])
        assert data["topology"] in summary
        for flow in data["flows"]:
            for value in flow.values():
                assert value in summary
        for group in ("fees", "net"):
            for name, value in data[group].items():
                assert name in summary
                assert value in summary
    assert "1m 5s" in summary


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
    assert "1/3 smoke scenarios passed" in embed["description"]
    direct, relay = fields["Scenarios"].splitlines()[1:]
    assert "❌" in direct and "Failed" in direct
    assert "⏭️" in relay and "Skipped" in relay
    summary = github_summary(env)
    assert "open the run log" in summary
    assert "earlier step" in summary


@pytest.mark.parametrize("failed_scenario", ("LOCAL", "DIRECT", "RELAY"))
def test_one_failed_smoke_still_reports_the_other_completed_scenarios(failed_scenario):
    env = complete_env()
    env["CCH_REPORT_JOB_RESULT"] = "failure"
    env[f"CCH_REPORT_{failed_scenario}_OUTCOME"] = "failure"
    env[f"CCH_REPORT_{failed_scenario}_JSON"] = ""

    payload = report.payload_from_env(env)
    lines = fields_by_name(payload)["Scenarios"].splitlines()

    assert payload["embeds"][0]["color"] == report.FAILURE_COLOR
    assert "2/3 smoke scenarios passed" in payload["embeds"][0]["description"]
    assert sum("❌" in line and "Failed" in line for line in lines) == 1
    assert sum("✅" in line and "Passed" in line for line in lines) == 2
    assert all("Skipped" not in line for line in lines)


def test_cancelled_run_does_not_claim_skipped_scenarios_passed():
    env = complete_env()
    env.update({
        "CCH_REPORT_JOB_RESULT": "cancelled",
        "CCH_REPORT_DIRECT_OUTCOME": "cancelled",
        "CCH_REPORT_DIRECT_JSON": "",
        "CCH_REPORT_RELAY_OUTCOME": "skipped",
        "CCH_REPORT_RELAY_JSON": "",
    })

    payload = report.payload_from_env(env)
    embed = payload["embeds"][0]

    assert embed["title"].startswith("🛑")
    assert "1/3 smoke scenarios passed" in embed["description"]
    assert "did not pass" in embed["description"]
    assert "Cancelled" in fields_by_name(payload)["Scenarios"]
    assert "Cancelled" in github_summary(env)


def test_gate_failure_reports_unstarted_smoke_stages_as_skipped():
    env = {
        "CCH_REPORT_JOB_RESULT": "failure",
        "CCH_REPORT_GATE_OUTCOME": "failure",
        "CCH_REPORT_SMOKE_JOB_RESULT": "skipped",
        "CCH_REPORT_LOCAL_OUTCOME": "",
    }
    fields = fields_by_name(report.payload_from_env(env))

    assert "❌ Schedule gate" in fields["Checks"]
    assert "⏭️ Checkout" in fields["Checks"]
    assert fields["Scenarios"].count("Skipped") == 3
    assert "Unknown" not in fields["Scenarios"]
    assert "earlier step" in github_summary(env)


def test_invalid_scenario_json_does_not_prevent_failure_report():
    env = complete_env()
    env["CCH_REPORT_JOB_RESULT"] = "failure"
    env["CCH_REPORT_LOCAL_OUTCOME"] = "failure"
    env["CCH_REPORT_LOCAL_JSON"] = "{not-json"

    fields = fields_by_name(report.payload_from_env(env))

    assert "summary unavailable" in fields["Scenarios"].splitlines()[0].lower()
    assert "invalid report JSON" in github_summary(env)
    assert "{not-json" not in github_summary(env)


@pytest.mark.parametrize("raw", ["", "null", "[]", "{}"])
def test_missing_scenario_details_are_explicit_even_if_step_succeeded(raw):
    env = complete_env()
    env["CCH_REPORT_LOCAL_JSON"] = raw

    local = fields_by_name(report.payload_from_env(env))["Scenarios"].splitlines()[0]

    assert "Passed" in local
    assert "summary unavailable" in local.lower()
    assert "unavailable" in github_summary(env).lower()


def test_report_accepts_string_fees_and_net():
    env = complete_env()
    data = json.loads(env["CCH_REPORT_LOCAL_JSON"])
    data["fees"] = "CCH 10 sats; routes 0"
    data["net"] = "principal round trip balanced"
    env["CCH_REPORT_LOCAL_JSON"] = json.dumps(data)

    summary = github_summary(env)

    assert "CCH 10 sats; routes 0" in summary
    assert "principal round trip balanced" in summary


def test_ci_summary_is_not_truncated_at_discord_field_limits():
    env = complete_env()
    data = json.loads(env["CCH_REPORT_LOCAL_JSON"])
    data["topology"] = "topology-start-" + "x" * 3000 + "-topology-end"
    data["fees"]["CCH"] = "fee-start-" + "y" * 3000 + "-fee-end"
    data["net"]["fiber2"] = "net-start-" + "z" * 3000 + "-net-end"
    env["CCH_REPORT_LOCAL_JSON"] = json.dumps(data)

    summary = github_summary(env)

    assert data["topology"] in summary
    assert data["fees"]["CCH"] in summary
    assert data["net"]["fiber2"] in summary
    assert len(summary) > 6000


def test_discord_field_and_embed_character_limits_are_enforced():
    env = complete_env()
    env["CCH_REPORT_FNN_PACKAGE"] = "p" * 5000
    env["CCH_REPORT_LOCAL_JSON"] = scenario_json("x" * 5000)
    env["CCH_REPORT_DIRECT_JSON"] = scenario_json("y" * 5000)
    env["CCH_REPORT_RELAY_JSON"] = scenario_json("z" * 5000)
    for key in ("FNN_VERSION", "FIBER_SOURCE", "RUN_NUMBER", "RUN_ATTEMPT", "TRIGGER"):
        env[f"CCH_REPORT_{key}"] = "😀@everyone`\n" * 2000

    embed = report.payload_from_env(env)["embeds"][0]
    fields = embed["fields"]
    length = lambda value: len(value.encode("utf-16-le")) // 2
    total = sum(length(embed[key]) for key in ("title", "description"))
    total += length(embed["footer"]["text"])
    total += sum(length(field["name"]) + length(field["value"]) for field in fields)

    assert length(embed["title"]) <= 256
    assert length(embed["description"]) <= 4096
    assert all(length(field["name"]) <= 256 for field in fields)
    assert all(length(field["value"]) <= 1024 for field in fields)
    assert total <= 6000


@pytest.mark.parametrize("duration", ["NaN", "Infinity", "-Infinity", "invalid"])
def test_invalid_duration_is_unavailable_instead_of_crashing(duration):
    env = complete_env()
    env["CCH_REPORT_TOTAL_SECONDS"] = duration

    assert "duration unavailable" in report.payload_from_env(env)["embeds"][0]["description"]
    assert "duration unavailable" in github_summary(env)


def test_summary_only_appends_full_report_without_webhook_or_network(monkeypatch, tmp_path):
    env = complete_env()
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    destination = tmp_path / "summary.md"
    destination.write_text("Existing summary\n", encoding="utf-8")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(destination))
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    request = mock.Mock(side_effect=AssertionError("summary-only must not use the network"))
    monkeypatch.setattr(report.urllib.request, "urlopen", request)

    report.main(["--summary-only"])

    actual = destination.read_text(encoding="utf-8")
    assert actual.startswith("Existing summary\n")
    assert github_summary(env).strip() in actual
    request.assert_not_called()


def test_summary_only_requires_a_summary_destination(monkeypatch):
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)

    with pytest.raises(SystemExit, match="GITHUB_STEP_SUMMARY"):
        report.main(["--summary-only"])


def test_dry_run_prints_compact_payload_without_credentials_or_network(monkeypatch, capsys):
    env = complete_env()
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    request = mock.Mock(side_effect=AssertionError("dry-run must not use the network"))
    monkeypatch.setattr(report.urllib.request, "urlopen", request)

    report.main(["--dry-run"])

    actual = json.loads(capsys.readouterr().out)
    assert actual == report.payload_from_env(env)
    request.assert_not_called()


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
        report.main([])
