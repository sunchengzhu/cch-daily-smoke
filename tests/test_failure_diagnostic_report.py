"""Failure reports retain attribution, supporting evidence and actionable steps."""

import json
from types import SimpleNamespace

import pytest

from scripts import send_daily_smoke_report as report
from test_daily_smoke_report import complete_env, fields_by_name


def relay_diagnostic():
    return {
        "scope": "local-to-relay",
        "phase": "first-hop-preflight",
        "summary": "lnd-c cannot reach relay LND.",
        "evidence": [
            "first-hop active=false; relay peer_connected=false",
            "second-hop graph policies enabled; graph evidence does not prove live reachability",
        ],
        "next_action": "Restore lnd-c connectivity to the relay peer, then verify the channel is active.",
        "fiberswap_assessment": "No FiberSwap-side fault identified; relay flow not exercised.",
    }


def failure_env(diagnostic=None):
    env = complete_env()
    env.update({
        "CCH_REPORT_JOB_RESULT": "failure",
        "CCH_REPORT_RELAY_OUTCOME": "failure",
        "CCH_REPORT_RELAY_JSON": "",
        "CCH_REPORT_FAILURE_CLASS": report.ENVIRONMENT_UNAVAILABLE,
        "CCH_REPORT_RELAY_DIAGNOSTIC": json.dumps(
            relay_diagnostic() if diagnostic is None else diagnostic
        ),
    })
    return env


def render(env):
    collected = report.collect_report(env)
    return (
        collected,
        report.build_discord_payload(collected),
        report.build_github_summary(collected),
    )


def test_offline_first_hop_reports_both_hop_evidence_and_action():
    collected, payload, summary = render(failure_env())
    diagnostic = fields_by_name(payload)["Failure diagnosis"]

    assert collected["scenarios"]["relay"]["diagnostic"] == relay_diagnostic()
    for expected in (
        "local-to-relay", "first-hop-preflight", "active=false",
        "second-hop graph policies enabled", "No FiberSwap-side fault identified",
        "Restore lnd-c connectivity", "FiberSwap direct passed; relay path not verified",
    ):
        assert expected in diagnostic
        assert expected in summary
    assert payload["embeds"][0]["color"] == report.ENVIRONMENT_COLOR
    assert "not a product regression" not in summary
    assert "no product assertion" not in summary


@pytest.mark.parametrize("fiberswap_disabled", [False, True])
def test_real_relay_collector_keeps_both_hops_in_compact_notification(monkeypatch, fiberswap_disabled):
    import test_fiber_swap_relay_daily_smoke as relay
    from test_relay_diagnostics import install_probes, phase_error, snapshots

    config = SimpleNamespace(
        lnd_d_pubkey="local-pubkey",
        relay_lnd_pubkey=relay.DEFAULT_RELAY_LND_PUBKEY,
        fiber_swap_lnd_pubkey="fiberswap-pubkey",
        lnd_channel_point=relay.DEFAULT_RELAY_CHANNEL_POINT,
        relay_to_fiber_swap_scid="5637388530143199233",
    )
    values = snapshots(config, active=fiberswap_disabled, connected=fiberswap_disabled)
    values["getchaninfo"]["node1_policy"]["disabled"] = fiberswap_disabled
    install_probes(monkeypatch, values)
    stage = "preflight.relay-fiberswap-link" if fiberswap_disabled else "preflight.local-relay-link"
    diagnosis = relay.collect_relay_failure_diagnostic(config, phase_error(stage))
    _, payload, _ = render(failure_env(diagnosis))
    compact = fields_by_name(payload)["Failure diagnosis"]

    assert f"active={str(fiberswap_disabled).lower()}" in compact
    assert f"FiberSwap disabled={str(fiberswap_disabled).lower()}" in compact
    assert "relay disabled=false" in compact
    if fiberswap_disabled:
        assert "disabled by FiberSwap" in compact
    else:
        assert "peer=absent" in compact
        assert "FiberSwap operations" in compact


def test_fiberswap_side_disabled_policy_names_the_implicated_end():
    diagnosis = {
        "scope": "fiberswap-relay-channel",
        "phase": "second-hop-policy",
        "summary": "FiberSwap-side policy is disabled.",
        "evidence": ["first-hop active=true", "FiberSwap policy disabled=true; relay policy disabled=false"],
        "next_action": "Ask the FiberSwap operator to check the channel with the relay.",
        "fiberswap_assessment": "FiberSwap's relay channel is implicated; direct success does not verify it.",
    }
    _, payload, summary = render(failure_env(diagnosis))
    compact = fields_by_name(payload)["Failure diagnosis"]

    for expected in ("FiberSwap-side policy is disabled", "FiberSwap policy disabled=true", "FiberSwap's relay channel is implicated", "Ask the FiberSwap operator"):
        assert expected in compact
        assert expected in summary
    assert "FiberSwap direct passed; relay path not verified" in compact


def test_later_unclassified_failure_does_not_claim_environment_or_product_exoneration():
    env = failure_env({
        "scope": "unknown",
        "phase": "flow-2-payment",
        "summary": "Payment result does not match the expected order.",
        "evidence": ["preflight passed; received amount mismatch"],
        "next_action": "Correlate the order and both payment hashes in FiberSwap and LND logs.",
        "fiberswap_assessment": "Unknown: FiberSwap or payment handling may be involved.",
    })
    env["CCH_REPORT_FAILURE_CLASS"] = ""
    _, payload, summary = render(env)

    assert payload["embeds"][0]["color"] == report.FAILURE_COLOR
    assert "flow-2-payment" in fields_by_name(payload)["Failure diagnosis"]
    assert "Unknown: FiberSwap or payment handling may be involved." in summary
    assert "Environment unavailable" not in summary


@pytest.mark.parametrize("outcome_key", [
    "CCH_REPORT_CLEANUP_OUTCOME", "CCH_REPORT_FNN_OUTCOME",
    "CCH_REPORT_LOCAL_OUTCOME", "CCH_REPORT_DIRECT_OUTCOME",
])
@pytest.mark.parametrize("outcome", ["failure", "cancelled"])
def test_other_failed_or_cancelled_steps_keep_a_red_verdict(outcome_key, outcome):
    env = failure_env()
    env[outcome_key] = outcome
    _, payload, summary = render(env)

    assert payload["embeds"][0]["color"] == report.FAILURE_COLOR
    assert "workflow did not pass" in payload["embeds"][0]["description"]
    assert "**Environment unavailable**" not in summary


def test_direct_failure_keeps_its_own_diagnostic_and_does_not_blame_everything_on_relay():
    env = failure_env()
    env["CCH_REPORT_DIRECT_OUTCOME"] = "failure"
    env["CCH_REPORT_DIRECT_DIAGNOSTIC"] = json.dumps({
        "scope": "fiberswap-api",
        "phase": "create-order",
        "summary": "FiberSwap API rejected the request.",
        "evidence": ["HTTP 503 during order creation"],
        "next_action": "Check FiberSwap API availability.",
        "fiberswap_assessment": "FiberSwap API endpoint is implicated.",
    })
    collected, payload, summary = render(env)
    compact = fields_by_name(payload)["Failure diagnosis"]

    assert collected["scenarios"]["direct"]["diagnostic"]["phase"] == "create-order"
    assert "Direct: fiberswap-api / create-order" in compact
    assert "HTTP 503" in compact
    assert "Check FiberSwap API availability" in compact
    assert "Relay: local-to-relay / first-hop-preflight" in compact
    assert "FiberSwap direct failed; see direct diagnostic" in compact
    assert "FiberSwap direct failed; see direct diagnostic" in summary
    assert payload["embeds"][0]["color"] == report.FAILURE_COLOR


@pytest.mark.parametrize("raw", [
    None, "", "{broken", "null", "[]", '"message"',
    json.dumps({**relay_diagnostic(), "evidence": "not a list"}),
    json.dumps({**relay_diagnostic(), "evidence": [None]}),
    json.dumps({**relay_diagnostic(), "scope": {"bad": "type"}}),
    json.dumps({**relay_diagnostic(), "fiberswap_assessment": ""}),
])
def test_missing_or_malformed_diagnostic_falls_back_to_explicit_unknown(raw):
    env = failure_env()
    if raw is None:
        env.pop("CCH_REPORT_RELAY_DIAGNOSTIC")
    else:
        env["CCH_REPORT_RELAY_DIAGNOSTIC"] = raw
    collected, payload, summary = render(env)
    compact = fields_by_name(payload)["Failure diagnosis"]

    assert collected["scenarios"]["relay"]["diagnostic"]["scope"] == "unknown"
    assert "unknown / unknown" in compact
    assert "Failure cause unknown" in compact
    assert "cannot determine FiberSwap's involvement" in summary
    assert "Inspect the failed step's log" in compact


def test_stale_environment_class_cannot_override_nonfailed_relay_or_job_cancellation():
    env = failure_env()
    env["CCH_REPORT_JOB_RESULT"] = "cancelled"
    assert report.payload_from_env(env)["embeds"][0]["color"] == report.FAILURE_COLOR
    env["CCH_REPORT_JOB_RESULT"] = "failure"
    env["CCH_REPORT_RELAY_OUTCOME"] = "skipped"
    assert report.payload_from_env(env)["embeds"][0]["color"] == report.FAILURE_COLOR


def test_long_diagnostics_keep_each_action_and_fit_utf16_embed_limits():
    diagnostic = {
        "scope": "scope-start-" + "😀" * 2000,
        "phase": "stage-start-" + "😀" * 2000,
        "summary": "summary-start-" + "😀" * 2000,
        "evidence": ["evidence-start-" + "😀" * 2000 + "-evidence-end"],
        "next_action": "action-start-" + "😀@everyone" * 2000 + "-action-end",
        "fiberswap_assessment": "assessment-start-" + "😀" * 2000,
    }
    env = failure_env(diagnostic)
    for key in ("LOCAL", "DIRECT"):
        env[f"CCH_REPORT_{key}_OUTCOME"] = "failure"
        env[f"CCH_REPORT_{key}_DIAGNOSTIC"] = json.dumps(diagnostic)
    _, payload, summary = render(env)
    embed = payload["embeds"][0]
    compact = fields_by_name(payload)["Failure diagnosis"]

    assert compact.count("action-start-") == 3
    assert compact.count("assessment-start-") == 3
    assert compact.count("scope-start-") == 3
    assert compact.count("stage-start-") == 3
    assert "FiberSwap direct failed; see direct diagnostic" in compact
    assert "-action-end" in summary
    assert "-evidence-end" in summary
    assert payload["allowed_mentions"] == {"parse": []}
    length = lambda value: len(value.encode("utf-16-le")) // 2
    assert all(length(field["value"]) <= 1024 for field in embed["fields"])
    total = sum(length(embed[key]) for key in ("title", "description"))
    total += length(embed["footer"]["text"])
    total += sum(length(field["name"]) + length(field["value"]) for field in embed["fields"])
    assert total <= 6000
