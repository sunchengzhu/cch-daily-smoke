"""Keep relay topology and post-failure attribution independent."""

from types import SimpleNamespace

import pytest

import test_fiber_swap_relay_daily_smoke as relay
from smoke_failure_class import EnvironmentUnavailable, classify_failure, failure_phase


FIRST_SCID = "5636281321933307905"
SECOND_SCID = "5637388530143199233"


@pytest.fixture
def config():
    return SimpleNamespace(
        lnd_d_pubkey="local-pubkey",
        relay_lnd_pubkey=relay.DEFAULT_RELAY_LND_PUBKEY,
        fiber_swap_lnd_pubkey="fiberswap-pubkey",
        lnd_channel_point=relay.DEFAULT_RELAY_CHANNEL_POINT,
        relay_to_fiber_swap_scid=SECOND_SCID,
        lnd_network="testnet",
        wait_timeout=0.01,
    )


def snapshots(config, active=False, connected=False):
    return {
        "getinfo": {
            "identity_pubkey": config.lnd_d_pubkey,
            "synced_to_chain": True,
            "synced_to_graph": True,
        },
        "listpeers": {
            "peers": [{"pub_key": config.relay_lnd_pubkey}] if connected else [],
        },
        "listchannels": {"channels": [{
            "remote_pubkey": config.relay_lnd_pubkey,
            "channel_point": config.lnd_channel_point,
            "scid": FIRST_SCID,
            "active": active,
            "pending_htlcs": [],
        }]},
        "getnodeinfo": {"node": {"addresses": [
            {"network": "tcp", "addr": "203.0.113.10:10011"},
        ]}},
        "getchaninfo": {
            "channel_id": SECOND_SCID,
            "node1_pub": config.fiber_swap_lnd_pubkey,
            "node2_pub": config.relay_lnd_pubkey,
            "node1_policy": {"disabled": False},
            "node2_policy": {"disabled": False},
        },
    }


def install_probes(monkeypatch, values):
    calls = []

    def probe(_config, args, timeout=None):
        calls.append((args, timeout))
        value = values[args[0]]
        if isinstance(value, BaseException):
            raise value
        return value

    monkeypatch.setattr(relay, "fiber_swap_lncli_json", probe)
    return calls


def phase_error(phase, message="primary failure"):
    try:
        with failure_phase(phase):
            raise AssertionError(message)
    except AssertionError as error:
        return error


def test_entry_validates_second_hop_independently_before_payment(monkeypatch, config):
    calls = install_probes(monkeypatch, snapshots(config, active=True, connected=True))
    monkeypatch.setattr(relay, "fiber_swap_fnn", lambda *_args: {})
    monkeypatch.setattr(relay, "wait_fiber_channel_quiescent", lambda *_args: None)
    for name in ("print_asset_convention", "print_fiber_swap_topology_key", "print_relay_topology_key"):
        monkeypatch.setattr(relay, name, lambda *_args: None)
    stopped = AssertionError("stop before live payment")

    def flow1(_config, second_scid):
        assert second_scid == SECOND_SCID
        assert second_scid != FIRST_SCID
        raise stopped

    monkeypatch.setattr(relay, "run_relay_flow_1_btc_to_cwbtc", flow1)
    with pytest.raises(AssertionError) as caught:
        relay._run_relay_smoke(config, 0)
    assert caught.value is stopped
    assert (["getchaninfo", SECOND_SCID], None) in calls
    assert not any(args == ["getchaninfo", FIRST_SCID] for args, _ in calls)


def test_first_hop_failure_still_probes_second_hop_and_addresses(monkeypatch, config):
    calls = install_probes(monkeypatch, snapshots(config))
    error = phase_error("preflight.local-relay-link", "first hop inactive")

    diagnostic = relay.collect_relay_failure_diagnostic(config, error)

    assert diagnostic["scope"] == "local-relay-link"
    assert "inactive" in diagnostic["summary"]
    assert "absent" in diagnostic["summary"]
    compact = diagnostic["evidence"][0]
    assert len(compact) <= 180
    assert "peer=absent, active=false" in compact
    assert "second hop: endpoints match" in compact
    assert "relay disabled=false" in compact
    assert "FiberSwap disabled=false" in compact
    assert "graph only" in compact
    assert "Runner: test" in diagnostic["next_action"]
    assert "FiberSwap operations" in diagnostic["next_action"]
    assert "Rainbow peer address" in diagnostic["next_action"]
    evidence = "\n".join(diagnostic["evidence"])
    assert "203.0.113.10:10011" in evidence
    assert "SCID=" + SECOND_SCID in evidence
    assert "endpoints match" in evidence
    assert "relay disabled=false" in evidence
    assert "FiberSwap disabled=false" in evidence
    assert "live connectivity remains unknown" in evidence
    assert "undetermined" in diagnostic["fiberswap_assessment"]
    assert (["getchaninfo", SECOND_SCID], 5) in calls
    assert len(calls) == 4
    assert all(timeout == 5 for _args, timeout in calls)


@pytest.mark.parametrize("side,owner", [("node1", "FiberSwap"), ("node2", "relay")])
def test_disabled_policy_is_attributed_to_its_endpoint(monkeypatch, config, side, owner):
    values = snapshots(config, active=True, connected=True)
    values["getchaninfo"][f"{side}_policy"]["disabled"] = True
    install_probes(monkeypatch, values)

    diagnostic = relay.collect_relay_failure_diagnostic(
        config, phase_error("preflight.relay-fiberswap-link")
    )

    assert diagnostic["summary"] == f"Second-hop graph policy is disabled by {owner}."
    assert f"{owner} disabled=true" in "\n".join(diagnostic["evidence"])
    assert "does not prove an API fault" in diagnostic["next_action"]
    assert f"announced by {owner}" in diagnostic["fiberswap_assessment"]
    assert "does not prove a FiberSwap API fault" in diagnostic["fiberswap_assessment"]
    assert f"{owner} disabled=true" in diagnostic["evidence"][0]
    assert len(diagnostic["evidence"][0]) <= 180


def test_zombie_edge_is_graph_evidence_not_a_fiberswap_verdict(monkeypatch, config):
    values = snapshots(config, active=True, connected=True)
    values["getchaninfo"] = AssertionError("edge marked as zombie")
    install_probes(monkeypatch, values)

    diagnostic = relay.collect_relay_failure_diagnostic(
        config, phase_error("preflight.relay-fiberswap-link", "edge marked as zombie")
    )

    assert diagnostic["scope"] == "relay-fiberswap-link"
    assert "graph edge" in diagnostic["summary"]
    assert "peer=present, active=true" in diagnostic["evidence"][0]
    assert "second hop: zombie in local graph" in diagnostic["evidence"][0]
    assert "zombie" in "\n".join(diagnostic["evidence"])
    assert "not proof of a service failure" in diagnostic["next_action"]
    assert "undetermined" in diagnostic["fiberswap_assessment"]


def test_missing_first_channel_is_configuration_evidence(monkeypatch, config):
    values = snapshots(config)
    values["listchannels"] = {"channels": []}
    install_probes(monkeypatch, values)
    error = phase_error("preflight.local-relay-link")

    diagnostic = relay.collect_relay_failure_diagnostic(config, error)

    assert diagnostic["scope"] == "configuration"
    assert "not proof of a disconnected peer" in diagnostic["next_action"]
    with pytest.raises(AssertionError) as caught:
        relay.get_relay_lnd_channel(config)
    assert not isinstance(caught.value, EnvironmentUnavailable)
    assert not isinstance(caught.value, relay.RelayChannelInactive)


def test_business_failure_is_not_excused_by_later_inactive_snapshot(monkeypatch, config):
    install_probes(monkeypatch, snapshots(config))
    error = phase_error("flow1", "payment balance mismatch")

    diagnostic = relay.collect_relay_failure_diagnostic(config, error)

    assert diagnostic["scope"] == "swap-flow"
    assert diagnostic["summary"] == "payment balance mismatch"
    assert classify_failure(error) is None
    assert "inactive" not in diagnostic["summary"]


def test_inactive_first_hop_classification_is_limited_to_preflight(monkeypatch, config):
    install_probes(monkeypatch, snapshots(config))
    monkeypatch.setattr(relay, "fiber_swap_fnn", lambda *_args: {})
    monkeypatch.setattr(relay.time, "sleep", lambda *_args: None)
    with pytest.raises(EnvironmentUnavailable):
        relay._run_relay_smoke(config, 0)
    with pytest.raises(relay.RelayChannelInactive) as caught:
        relay.get_relay_lnd_channel(config)
    assert classify_failure(caught.value) is None


def test_nested_cli_failure_keeps_preflight_scope(monkeypatch, config):
    install_probes(monkeypatch, snapshots(config))
    try:
        with failure_phase("preflight.relay-fiberswap-link"):
            with failure_phase("local-lnd:getchaninfo"):
                raise AssertionError("edge marked as zombie")
    except AssertionError as error:
        diagnostic = relay.collect_relay_failure_diagnostic(config, error)
    assert diagnostic["scope"] == "relay-fiberswap-link"
    assert "local-lnd:getchaninfo" in diagnostic["phase"]


def test_all_diagnostic_probes_are_independent_when_queries_fail(monkeypatch, config):
    values = snapshots(config)
    values["listpeers"] = AssertionError("read timed out")
    values["listchannels"] = ValueError("malformed JSON")
    values["getnodeinfo"] = AssertionError("node not found")
    calls = install_probes(monkeypatch, values)

    diagnostic = relay.collect_relay_failure_diagnostic(config, phase_error("flow1"))

    assert len(calls) == 4
    assert any("endpoints match" in line for line in diagnostic["evidence"])
    assert sum("probe unavailable" in line for line in diagnostic["evidence"]) == 3


def test_compact_two_hop_snapshot_precedes_business_evidence(monkeypatch, config):
    install_probes(monkeypatch, snapshots(config))
    monkeypatch.setattr(
        relay,
        "build_fiber_swap_failure_diagnostic",
        lambda *_args: {"scope": "fiberswap-api", "evidence": ["Original API evidence"]},
    )

    diagnostic = relay.collect_relay_failure_diagnostic(
        config, phase_error("fiberswap-api", "API failed")
    )

    assert diagnostic["evidence"][0].startswith("First hop:")
    assert "second hop:" in diagnostic["evidence"][0]
    assert len(diagnostic["evidence"][0]) <= 180
    assert diagnostic["evidence"][1] == "Original API evidence"
    assert any("Relay advertised addresses" in line for line in diagnostic["evidence"])


@pytest.mark.parametrize("failed_reporter", ["emit_failure_class", "collect_relay_failure_diagnostic", "emit_failure_diagnostic"])
def test_reporting_failure_preserves_original_exception(monkeypatch, config, failed_reporter):
    original = AssertionError("original business failure")
    monkeypatch.setattr(relay.RelayFiberSwapSmokeConfig, "from_env", lambda: config)

    def fail_run(*_args):
        raise original

    def fail_report(*_args):
        raise OSError("diagnostic output unavailable")

    monkeypatch.setattr(relay, "_run_relay_smoke", fail_run)
    monkeypatch.setattr(relay, "emit_failure_class", lambda *_args: None)
    monkeypatch.setattr(relay, "collect_relay_failure_diagnostic", lambda *_args: {})
    monkeypatch.setattr(relay, "emit_failure_diagnostic", lambda *_args: None)
    monkeypatch.setattr(relay, failed_reporter, fail_report)

    with pytest.raises(AssertionError) as caught:
        relay.test_fiber_swap_via_relay_lnd_bidirectional()
    assert caught.value is original
    assert caught.value.__traceback__.tb_next is not None


def test_success_does_not_probe_diagnostics(monkeypatch, config):
    monkeypatch.setattr(relay.RelayFiberSwapSmokeConfig, "from_env", lambda: config)
    monkeypatch.setattr(relay, "_run_relay_smoke", lambda *_args: None)

    def unexpected_probe(*_args):
        raise AssertionError("diagnostics must run only on failure")

    monkeypatch.setattr(relay, "collect_relay_failure_diagnostic", unexpected_probe)
    relay.test_fiber_swap_via_relay_lnd_bidirectional()
