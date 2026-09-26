from types import SimpleNamespace

import pytest

import relay_reconnect as recovery
import test_fiber_swap_relay_daily_smoke as relay

PK = relay.DEFAULT_RELAY_LND_PUBKEY
OLD = "16.147.215.150:10011"
NEW = "44.244.96.157:10011"


def node(timestamp, addresses, pubkey=PK):
    return {"node": {"pub_key": pubkey, "last_update": timestamp,
                     "addresses": [{"network": "tcp", "addr": address} for address in addresses]}}


@pytest.mark.parametrize("address", ["172.31.251.159:9735", "127.0.0.1:10011", "0.0.0.0:10011", "169.254.169.254:80", "224.0.0.1:9735", "example.com:9735", "44.244.96.157:0", "44.244.96.157:65536", None])
def test_only_public_literal_addresses_are_candidates(address):
    assert recovery.public_tcp_address(address) is None


def test_public_ipv6_and_ipv4():
    assert recovery.public_tcp_address(NEW) == NEW
    assert recovery.public_tcp_address("[2606:4700:4700::1111]:9735") == "[2606:4700:4700::1111]:9735"


def test_newer_gossip_repairs_stale_address_without_trying_old_one():
    calls, dials = [], []
    snapshots = {"lnd-c": node(1790251599, [OLD, "172.31.251.159:9735"]),
                 "lnd-d": node(1790413506, [NEW, "172.31.251.159:9735"])}

    def query(source, args, timeout):
        calls.append((source, args, timeout))
        if args == ["listpeers"]:
            return {"peers": [{"pub_key": PK}] if dials else []}
        return snapshots[source]

    notes = recovery.recover_relay_peer(PK, ["lnd-c", "lnd-d"], query, lambda addr, timeout: dials.append(addr))
    assert dials == [NEW]
    assert "original channel activation still required" in notes[-1]
    assert any("1790413506" in note and "lnd-d" in note for note in notes)
    assert all(timeout <= 5 for _, _, timeout in calls)


def test_cli_success_without_peer_is_not_recovery_and_addresses_are_deduplicated():
    dials = []

    def query(source, args, timeout):
        if args == ["listpeers"]:
            return {"peers": []}
        return node(10 if source == "lnd-d" else 9, [NEW, OLD, NEW])

    notes = recovery.recover_relay_peer(PK, ["lnd-c", "lnd-d"], query, lambda addr, timeout: dials.append(addr))
    assert dials == [NEW, OLD]
    assert "unsuccessful" in notes[-1]


def test_bad_source_does_not_prevent_other_source_recovery():
    dials = []

    def query(source, args, timeout):
        if args == ["listpeers"]:
            return {"peers": [{"pub_key": PK}] if dials else []}
        if source == "lnd-d":
            raise TimeoutError("query timeout")
        return node(1, [NEW])

    notes = recovery.recover_relay_peer(PK, ["lnd-c", "lnd-d"], query, lambda addr, timeout: dials.append(addr))
    assert dials == [NEW]
    assert any("query timeout" in note for note in notes)


def test_identity_mismatch_is_not_dialed():
    dials = []
    def query(source, args, timeout):
        return {"peers": []} if args == ["listpeers"] else node(10, [NEW], "wrong-key")
    notes = recovery.recover_relay_peer(PK, ["lnd-c", "lnd-d"], query, lambda *args: dials.append(args))
    assert not dials
    assert any("identity does not match" in note for note in notes)


def test_connected_peer_is_not_disconnected_or_redialed():
    calls = []
    def query(source, args, timeout):
        calls.append(args)
        return {"peers": [{"pub_key": PK}]}
    notes = recovery.recover_relay_peer(PK, ["lnd-c", "lnd-d"], query, lambda *_: pytest.fail("must not dial"))
    assert calls == [["listpeers"]]
    assert "already connected" in notes[0]


def test_failed_peer_read_skips_all_mutations():
    def query(*_):
        raise TimeoutError("cannot inspect peer state")
    notes = recovery.recover_relay_peer(PK, ["lnd-c"], query, lambda *_: pytest.fail("must not dial"))
    assert "skipped" in notes[0]


def test_total_deadline_limits_retries(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(recovery.time, "monotonic", lambda: now[0])
    dials = []
    def query(source, args, timeout):
        return {"peers": []} if args == ["listpeers"] else node(1, [NEW, OLD])
    def connect(addr, timeout):
        dials.append(addr)
        now[0] += timeout
        raise TimeoutError("dial timed out")
    notes = recovery.recover_relay_peer(PK, ["lnd-c"], query, connect, budget=5)
    assert dials == [NEW]
    assert now[0] == 5
    assert "unsuccessful" in notes[-1]


def test_no_more_than_four_candidates():
    dials = []
    def query(source, args, timeout):
        return {"peers": []} if args == ["listpeers"] else node(1, [f"44.244.96.{n}:10011" for n in range(1, 8)])
    recovery.recover_relay_peer(PK, ["lnd-c"], query, lambda addr, timeout: dials.append(addr))
    assert len(dials) == 4


def test_adapter_uses_secondary_container_and_pinned_pubkey(monkeypatch):
    config = SimpleNamespace(lnd_container="lnd-c", relay_lnd_pubkey=PK)
    monkeypatch.delenv("CCH_FIBER_SWAP_LND_D_LNCLI_PREFIX", raising=False)
    monkeypatch.setenv("CCH_FIBER_SWAP_RELAY_GOSSIP_LND_CONTAINER", "lnd-d")
    def inactive(_):
        raise relay.RelayChannelInactive("inactive")
    monkeypatch.setattr(relay, "get_relay_lnd_channel", inactive)
    dials = []
    def query(selected, args, timeout):
        if args == ["listpeers"]:
            assert selected.lnd_container == "lnd-c"
            return {"peers": [{"pub_key": PK}] if dials else []}
        return node(10, [NEW]) if selected.lnd_container == "lnd-d" else node(1, [OLD])
    def connect(selected, args, timeout):
        assert selected is config
        assert args == ["connect", "--timeout", "10s", f"{PK}@{NEW}"]
        assert timeout <= 12
        dials.append(args)
    monkeypatch.setattr(relay, "fiber_swap_lncli_json", query)
    monkeypatch.setattr(relay, "fiber_swap_lncli_raw", connect)
    relay.reconnect_inactive_relay(config)
    assert len(dials) == 1


def test_missing_channel_never_triggers_reconnect(monkeypatch):
    def missing(_):
        raise AssertionError("channel missing")
    monkeypatch.setattr(relay, "get_relay_lnd_channel", missing)
    monkeypatch.setattr(relay, "recover_relay_peer", lambda *_: pytest.fail("must not recover"))
    with pytest.raises(AssertionError, match="channel missing"):
        relay.reconnect_inactive_relay(SimpleNamespace())


def test_active_channel_needs_no_recovery(monkeypatch):
    monkeypatch.setattr(relay, "get_relay_lnd_channel", lambda _: {"active": True})
    assert relay.reconnect_inactive_relay(SimpleNamespace()) == []


def test_reconnect_never_bypasses_original_channel_check(monkeypatch):
    config = SimpleNamespace(lnd_d_pubkey="local")
    monkeypatch.setattr(relay, "fiber_swap_lncli_json", lambda *_: {
        "identity_pubkey": "local", "synced_to_chain": True, "synced_to_graph": True,
    })
    monkeypatch.setattr(relay, "fiber_swap_fnn", lambda *_: {})
    notes = ["Pinned relay peer connected; original channel activation still required."]
    monkeypatch.setattr(relay, "reconnect_inactive_relay", lambda _: notes)
    def inactive(_):
        raise relay.RelayChannelInactive("original channel still inactive")
    monkeypatch.setattr(relay, "wait_relay_lnd_channel_quiescent", inactive)
    monkeypatch.setattr(relay, "run_relay_flow_1_btc_to_cwbtc", lambda *_: pytest.fail("must not pay"))
    with pytest.raises(relay.EnvironmentUnavailable, match="original channel still inactive") as caught:
        relay._run_relay_smoke(config, 0)
    assert caught.value.relay_reconnect_notes == notes


def test_custom_cli_prefix_cannot_be_mistaken_for_independent_source(monkeypatch):
    config = SimpleNamespace(lnd_container="lnd-c", relay_lnd_pubkey=PK)
    monkeypatch.setenv("CCH_FIBER_SWAP_LND_D_LNCLI_PREFIX", "custom-lncli")
    def inactive(_):
        raise relay.RelayChannelInactive("inactive")
    monkeypatch.setattr(relay, "get_relay_lnd_channel", inactive)
    def recover(pubkey, sources, query, connect):
        assert sources == ["lnd-c"]
        return []
    monkeypatch.setattr(relay, "recover_relay_peer", recover)
    assert relay.reconnect_inactive_relay(config) == []
