"""Daily FiberSwap CCH smoke check through a public relay LND."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, fields

import pytest

from smoke_report import emit_smoke_report
from test_cch_daily_smoke import assert_balance_delta, hex_to_int
from test_fiber_swap_daily_smoke import (
    FiberSwapSmokeConfig,
    canonical_hash,
    create_fiber_invoice,
    create_swap_order,
    decode_lnd_invoice,
    decimal_scid,
    fiber_balances,
    fiber_swap_fnn,
    fiber_swap_lncli_json,
    fiber_swap_lncli_raw,
    invoice_string,
    lnd_channel_balance_snapshot,
    lnd_channel_route_ids,
    lnd_outgoing_chan_id,
    parse_fiber_invoice,
    print_asset_convention,
    print_balance_table,
    print_fiber_swap_topology_key,
    print_flow_footer,
    print_flow_header,
    require_cch_fee_within_limit,
    require_spendable,
    validate_new_order,
    verify_settled_invoice_channel,
    wait_fiber_channel_quiescent,
    wait_fiber_invoice_paid,
    wait_flow_2_success,
    wait_lnd_payment_succeeded,
    wait_lnd_balance_snapshot_delta,
    wait_order_success,
)


pytestmark = pytest.mark.skipif(
    os.environ.get("CCH_FIBER_SWAP_RELAY_SMOKE_ENABLED") != "1",
    reason=(
        "set CCH_FIBER_SWAP_RELAY_SMOKE_ENABLED=1 to run the FiberSwap "
        "relay-LND live smoke"
    ),
)


DEFAULT_RELAY_LND_PUBKEY = (
    "03676dd479c6c5422bdcec4691f42f1a6c6b386519b431542e03f31e773d570a41"
)
DEFAULT_RELAY_CHANNEL_POINT = (
    "0cedcc728ba1a51582c6650fc14f1a862912ee4ea400a94049c83326b7e5408e:1"
)
DEFAULT_RELAY_TO_FIBER_SWAP_SCID = "5637388530143199233"


@dataclass(frozen=True)
class RelayFiberSwapSmokeConfig(FiberSwapSmokeConfig):
    relay_lnd_pubkey: str
    relay_to_fiber_swap_scid: str

    @classmethod
    def from_env(cls) -> "RelayFiberSwapSmokeConfig":
        base = FiberSwapSmokeConfig.from_env()
        values = {
            field.name: getattr(base, field.name)
            for field in fields(FiberSwapSmokeConfig)
        }
        values.update(
            lnd_container=os.environ.get(
                "CCH_FIBER_SWAP_RELAY_LND_CONTAINER", "lnd-c"
            ),
            # Optional identity pin. The active channel/outpoint and peer are
            # always pinned even when the local pubkey is not configured.
            lnd_d_pubkey=os.environ.get(
                "CCH_FIBER_SWAP_RELAY_LOCAL_LND_PUBKEY", ""
            ),
            lnd_channel_point=os.environ.get(
                "CCH_FIBER_SWAP_RELAY_CHANNEL_POINT",
                DEFAULT_RELAY_CHANNEL_POINT,
            ),
            lnd_fee_limit_sats=int(
                os.environ.get(
                    "CCH_FIBER_SWAP_RELAY_LND_FEE_LIMIT_SATS",
                    str(base.lnd_fee_limit_sats),
                )
            ),
            relay_lnd_pubkey=os.environ.get(
                "CCH_FIBER_SWAP_RELAY_NODE_PUBKEY",
                DEFAULT_RELAY_LND_PUBKEY,
            ),
            relay_to_fiber_swap_scid=os.environ.get(
                "CCH_FIBER_SWAP_RELAY_TO_FIBER_SWAP_SCID",
                DEFAULT_RELAY_TO_FIBER_SWAP_SCID,
            ),
        )
        config = cls(**values)
        if config.lnd_fee_limit_sats <= 0:
            pytest.fail(
                "CCH_FIBER_SWAP_RELAY_LND_FEE_LIMIT_SATS must be positive"
            )
        if decimal_scid(config.relay_to_fiber_swap_scid) is None:
            pytest.fail(
                "CCH_FIBER_SWAP_RELAY_TO_FIBER_SWAP_SCID must be a non-zero "
                "decimal short channel ID"
            )
        return config


def get_relay_lnd_channel(config: RelayFiberSwapSmokeConfig):
    channels = fiber_swap_lncli_json(config, ["listchannels"]).get("channels", [])
    matching_peer = [
        channel
        for channel in channels
        if channel.get("remote_pubkey") == config.relay_lnd_pubkey
    ]
    matches = [
        channel
        for channel in matching_peer
        if channel.get("channel_point") == config.lnd_channel_point
    ]
    if len(matches) != 1 or matches[0].get("active") is not True:
        available = [
            {
                "channel_point": channel.get("channel_point"),
                "scid": channel.get("scid"),
                "chan_id": channel.get("chan_id"),
                "active": channel.get("active"),
                "local_balance": channel.get("local_balance"),
                "remote_balance": channel.get("remote_balance"),
            }
            for channel in matching_peer
        ]
        raise AssertionError(
            "expected the configured local-LND/relay-LND channel to exist "
            f"and be active at {config.lnd_channel_point}; available: "
            f"{json.dumps(available, sort_keys=True)}"
        )
    return matches[0]


def wait_relay_lnd_channel_quiescent(config: RelayFiberSwapSmokeConfig):
    deadline = time.monotonic() + config.wait_timeout
    last_pending = None
    last_error = None
    while time.monotonic() < deadline:
        try:
            channel = get_relay_lnd_channel(config)
            last_error = None
        except Exception as exc:  # listchannels is a read-only query.
            last_error = f"{type(exc).__name__}: {exc}"
            time.sleep(2)
            continue
        last_pending = channel.get("pending_htlcs") or []
        if not last_pending:
            return channel
        time.sleep(2)
    raise AssertionError(
        "timed out waiting for local-LND/relay-LND HTLCs to settle; "
        f"pending HTLCs: {json.dumps(last_pending, sort_keys=True)}; "
        f"last read error: {last_error}"
    )


def relay_lnd_balances(config: RelayFiberSwapSmokeConfig):
    channel = wait_relay_lnd_channel_quiescent(config)
    return lnd_channel_balance_snapshot(channel, "Local LND", "Relay LND")


def wait_relay_lnd_balance_delta(
    config,
    before,
    expected_local_delta,
    expected_relay_delta,
    interval=0.5,
):
    return wait_lnd_balance_snapshot_delta(
        config,
        before,
        load_channel=get_relay_lnd_channel,
        local_label="Local LND",
        remote_label="Relay LND",
        expected_local_delta=expected_local_delta,
        expected_remote_delta=expected_relay_delta,
        channel_description="local-LND/relay-LND channel",
        interval=interval,
    )


def validate_relay_to_fiber_swap_channel(config: RelayFiberSwapSmokeConfig):
    channel = fiber_swap_lncli_json(
        config, ["getchaninfo", config.relay_to_fiber_swap_scid]
    )
    assert str(channel.get("channel_id")) == config.relay_to_fiber_swap_scid
    endpoints = {channel.get("node1_pub"), channel.get("node2_pub")}
    assert endpoints == {
        config.relay_lnd_pubkey,
        config.fiber_swap_lnd_pubkey,
    }, (
        "configured public relay channel has unexpected endpoints: "
        f"{json.dumps(channel, sort_keys=True)}"
    )
    for side in ("node1_policy", "node2_policy"):
        policy = channel.get(side)
        assert isinstance(policy, dict), (
            f"configured public relay channel has no {side}: {channel}"
        )
        assert policy.get("disabled") is not True, (
            f"configured public relay channel has disabled {side}: {channel}"
        )
    return channel


def expected_relay_route(
    config: RelayFiberSwapSmokeConfig,
    amount: int,
    outgoing_chan_id: str,
    local_route_ids: set[str],
):
    result = fiber_swap_lncli_json(
        config,
        [
            "queryroutes",
            f"--amt={amount}",
            f"--fee_limit={config.lnd_fee_limit_sats}",
            f"--outgoing_chan_id={outgoing_chan_id}",
            config.fiber_swap_lnd_pubkey,
        ],
    )
    for route in result.get("routes", []):
        hops = route.get("hops") or []
        if (
            len(hops) == 2
            and hops[0].get("pub_key") == config.relay_lnd_pubkey
            and str(hops[0].get("chan_id")) in local_route_ids
            and hops[1].get("pub_key") == config.fiber_swap_lnd_pubkey
            and str(hops[1].get("chan_id"))
            == config.relay_to_fiber_swap_scid
        ):
            fee = int(route.get("total_fees", "0"))
            fee_msat_value = route.get("total_fees_msat")
            fee_msat = int(
                fee_msat_value
                if fee_msat_value not in {None, ""}
                else fee * 1000
            )
            assert 0 < fee_msat <= config.lnd_fee_limit_sats * 1000, (
                "expected a positive relay fee within the configured limit; "
                f"route={json.dumps(route, sort_keys=True)}"
            )
            assert fee_msat % 1000 == 0, (
                "relay smoke requires a whole-sat Lightning fee because its "
                "channel balance and reverse-payment checks use sat precision; "
                f"got {fee_msat} msat"
            )
            return route
    raise AssertionError(
        "no exact local LND → relay LND → FiberSwap CCH LND route found: "
        f"{json.dumps(result, sort_keys=True)}"
    )


def verify_lnd_relay_payment(
    config: RelayFiberSwapSmokeConfig,
    payment: dict,
    expected_value: int,
    local_route_ids: set[str],
):
    value = int(payment.get("value_sat", "0"))
    fee = int(payment.get("fee_sat", "0"))
    fee_msat_value = payment.get("fee_msat")
    fee_msat = int(
        fee_msat_value if fee_msat_value not in {None, ""} else fee * 1000
    )
    assert value == expected_value
    assert 0 < fee_msat <= config.lnd_fee_limit_sats * 1000, (
        "expected a positive Lightning relay fee within the configured "
        f"limit; got {fee_msat} msat"
    )
    assert fee_msat % 1000 == 0, (
        "relay smoke requires a whole-sat Lightning fee because its channel "
        "balance and reverse-payment checks use sat precision; "
        f"got {fee_msat} msat"
    )
    fee_from_msat = fee_msat // 1000
    assert fee == fee_from_msat, (
        "LND returned inconsistent relay fee values: "
        f"fee_sat={fee}, fee_msat={fee_msat}"
    )
    succeeded = [
        htlc for htlc in payment.get("htlcs", []) if htlc.get("status") == "SUCCEEDED"
    ]
    assert succeeded, f"local LND payment has no successful HTLC: {payment}"
    for htlc in succeeded:
        hops = (htlc.get("route") or {}).get("hops") or []
        assert len(hops) == 2, f"expected an exact two-hop relay route: {hops}"
        assert hops[0].get("pub_key") == config.relay_lnd_pubkey
        assert str(hops[0].get("chan_id")) in local_route_ids
        assert hops[1].get("pub_key") == config.fiber_swap_lnd_pubkey
        assert (
            str(hops[1].get("chan_id"))
            == config.relay_to_fiber_swap_scid
        )
    return value + fee, fee


def relay_hint_fee_msat(hint: dict, amount_sats: int) -> int:
    base = int(hint.get("fee_base_msat", 0))
    ppm = int(hint.get("fee_proportional_millionths", 0))
    return base + amount_sats * 1000 * ppm // 1_000_000


def format_msat_as_sats(value: int) -> str:
    if value % 1000 == 0:
        sats = value // 1000
        unit = "sat" if sats == 1 else "sats"
        return f"{sats:,} {unit}"
    return f"{value / 1000:,.3f} sats"


def create_private_relay_lnd_invoice(
    config: RelayFiberSwapSmokeConfig,
    amount: int,
    local_lnd_pubkey: str,
):
    target_channel = get_relay_lnd_channel(config)
    target_route_ids = lnd_channel_route_ids(target_channel)
    assert target_route_ids, (
        "configured local-LND/relay-LND channel exposes no usable SCID or "
        f"alias: {json.dumps(target_channel, sort_keys=True)}"
    )
    result = fiber_swap_lncli_json(
        config,
        [
            "addinvoice",
            f"--amt={amount}",
            f"--memo=fiber-swap-relay-flow2-{int(time.time())}",
            "--expiry=86400",
            "--private",
        ],
    )
    decoded = fiber_swap_lncli_json(
        config, ["decodepayreq", result["payment_request"]]
    )
    payment_hash = canonical_hash(decoded["payment_hash"])
    assert decoded["destination"] == local_lnd_pubkey
    assert int(decoded["num_satoshis"]) == amount
    hints = [
        hop
        for route in decoded.get("route_hints", [])
        for hop in route.get("hop_hints", [])
        if hop.get("node_id") == config.relay_lnd_pubkey
        and str(hop.get("chan_id")) in target_route_ids
    ]
    assert hints, (
        "local LND private invoice has no route hint through the configured "
        f"relay-LND channel (route ids {sorted(target_route_ids)}); "
        f"decoded invoice: {json.dumps(decoded, sort_keys=True)}"
    )
    fees = [relay_hint_fee_msat(hint, amount) for hint in hints]
    assert all(fee > 0 for fee in fees), (
        "relay-LND invoice hint must require a non-zero Lightning routing "
        f"fee to exercise the FNN v0.9+ CCH fee path; hints={hints}"
    )
    return result["payment_request"], payment_hash, hints, max(fees)


def print_relay_topology_key(config: RelayFiberSwapSmokeConfig):
    print(
        "  Relay LND                : public Lightning forwarding node between "
        f"{config.lnd_container} and FiberSwap CCH LND"
    )


def print_relay_flow_1_summary(
    config: RelayFiberSwapSmokeConfig,
    *,
    payment_hash: str,
    principal: int,
    cch_fee: int,
    lnd_outflow: int,
    lightning_route_fee: int,
    lnd_before: dict,
    lnd_after: dict,
    fiber_before: dict,
    fiber_after: dict,
):
    print_flow_header(
        1,
        "BTC → cWBTC via relay LND",
        (
            (
                "BTC leg",
                f"{config.lnd_container} --BTC--> Relay LND --BTC--> "
                "FiberSwap CCH LND",
            ),
            (
                "CCH action",
                "after its LND receives BTC, FiberSwap CCH tells its FNN "
                "to pay cWBTC",
            ),
            (
                "cWBTC leg",
                "FiberSwap FNN (CCH node) --cWBTC--> "
                "Bottle (Fiber trampoline) --cWBTC--> fiber2",
            ),
        ),
        payment_hash,
    )
    print(f"WHO PAID      : {config.lnd_container} paid {lnd_outflow:,} sats")
    print(f"WHO RECEIVED  : fiber2 received {principal:,} raw cWBTC")
    print("FEE OWNERSHIP:")
    print(
        f"  - CCH service fee     : {cch_fee:,} sats; paid by "
        f"{config.lnd_container}"
    )
    print(
        f"  - Lightning route fee : {lightning_route_fee:,} sats; paid by "
        f"{config.lnd_container} to the relay route"
    )
    print(
        "  - Fiber route fee     : amount not exposed by the external API; "
        "paid by the FiberSwap CCH service through its FNN"
    )
    print_balance_table(
        f"{config.lnd_container} ↔ Relay LND channel",
        "sats",
        lnd_before,
        lnd_after,
    )
    print_balance_table("Fiber channel", "raw cWBTC", fiber_before, fiber_after)
    print_flow_footer(1)


def print_relay_flow_2_summary(
    config: RelayFiberSwapSmokeConfig,
    *,
    payment_hash: str,
    btc_principal: int,
    cch_fee: int,
    fiber_route_fee: int,
    fiber_total: int,
    relay_hint_fee_msat_value: int,
    lnd_before: dict,
    lnd_after: dict,
    fiber_before: dict,
    fiber_after: dict,
):
    print_flow_header(
        2,
        "cWBTC → BTC via relay LND",
        (
            (
                "cWBTC leg",
                "fiber2 --cWBTC--> Bottle (Fiber trampoline) --cWBTC--> "
                "FiberSwap FNN (CCH node)",
            ),
            (
                "CCH action",
                "after its FNN receives cWBTC, FiberSwap CCH tells its LND "
                "to pay BTC",
            ),
            (
                "BTC leg",
                "FiberSwap CCH LND --BTC--> Relay LND --BTC--> "
                f"{config.lnd_container}",
            ),
        ),
        payment_hash,
    )
    print(f"WHO PAID      : fiber2 paid {fiber_total:,} raw cWBTC")
    print(
        f"WHO RECEIVED  : {config.lnd_container} received "
        f"{btc_principal:,} sats"
    )
    print("FEE OWNERSHIP:")
    print(
        f"  - CCH service fee     : {cch_fee:,} raw cWBTC; paid by fiber2"
    )
    print(
        f"  - Fiber route fee     : {fiber_route_fee:,} raw cWBTC; paid by "
        "fiber2 across the trampoline route"
    )
    print(
        "  - Lightning route fee : "
        f"{format_msat_as_sats(relay_hint_fee_msat_value)} final-hop fee "
        "(invoice hint); paid by FiberSwap CCH LND; total not exposed"
    )
    print_balance_table(
        f"{config.lnd_container} ↔ Relay LND channel",
        "sats",
        lnd_before,
        lnd_after,
    )
    print_balance_table("Fiber channel", "raw cWBTC", fiber_before, fiber_after)
    print_flow_footer(2)


def run_relay_flow_1_btc_to_cwbtc(config: RelayFiberSwapSmokeConfig):
    """Pay BTC from the local LND through relay LND and receive cWBTC."""

    principal = config.amount_sats
    fiber_before = fiber_balances(config)
    lnd_before = relay_lnd_balances(config)
    target_channel = get_relay_lnd_channel(config)
    local_route_ids = lnd_channel_route_ids(target_channel)
    outgoing_chan_id = lnd_outgoing_chan_id(target_channel)
    require_spendable(
        "local LND outbound BTC (principal preflight)",
        lnd_before["Local LND spendable"],
        principal,
    )
    require_spendable("Bottle outbound cWBTC", fiber_before["Bottle"], principal)

    fiber_invoice, payment_hash = create_fiber_invoice(config, principal)
    order = create_swap_order(
        config,
        "/api/swap/btc-to-ckb",
        {"fiber_pay_req": fiber_invoice},
        payment_hash,
    )
    validate_new_order(
        order,
        payment_hash,
        fiber_invoice,
        "btc-to-ckb",
        config.lnd_network,
    )
    lightning_invoice = invoice_string(order["incoming_invoice"], "Lightning")
    decoded = decode_lnd_invoice(config, lightning_invoice)
    assert decoded["payment_hash"] == payment_hash
    assert decoded["destination"] == config.fiber_swap_lnd_pubkey

    cch_fee = hex_to_int(order["fee_sats"])
    require_cch_fee_within_limit(config, "BTC → cWBTC via relay LND", cch_fee)
    lightning_total = hex_to_int(order["amount_sats"])
    assert lightning_total == principal + cch_fee
    assert decoded["amount"] == lightning_total
    expected_relay_route(
        config, lightning_total, outgoing_chan_id, local_route_ids
    )

    require_spendable(
        "local LND outbound BTC",
        lnd_before["Local LND spendable"],
        lightning_total + config.lnd_fee_limit_sats,
    )
    try:
        fiber_swap_lncli_raw(
            config,
            [
                "payinvoice",
                "--force",
                f"--timeout={config.payment_timeout}s",
                f"--fee_limit={config.lnd_fee_limit_sats}",
                f"--outgoing_chan_id={outgoing_chan_id}",
                lightning_invoice,
            ],
            timeout=max(config.payment_timeout + 30, config.command_timeout),
        )
    except AssertionError as exc:
        if "command timed out" not in str(exc):
            raise
        print(
            "lncli response timed out after payment submission; reconciling "
            f"Lightning payment {payment_hash} by hash"
        )
    lnd_payment = wait_lnd_payment_succeeded(
        config,
        payment_hash,
        config.lnd_container,
    )
    lnd_outflow, lightning_route_fee = verify_lnd_relay_payment(
        config,
        lnd_payment,
        lightning_total,
        local_route_ids,
    )
    wait_order_success(config, payment_hash)
    wait_fiber_invoice_paid(config, payment_hash)

    fiber_after = fiber_balances(config)
    lnd_after = wait_relay_lnd_balance_delta(
        config,
        lnd_before,
        -lnd_outflow,
        lnd_outflow,
    )
    assert_balance_delta(
        "FLOW 1 fiber2",
        fiber_before["fiber2"],
        fiber_after["fiber2"],
        principal,
        f"fiber_channel_id={config.fiber_channel_id}",
    )
    assert_balance_delta(
        "FLOW 1 Bottle",
        fiber_before["Bottle"],
        fiber_after["Bottle"],
        -principal,
        f"fiber_channel_id={config.fiber_channel_id}",
    )
    assert_balance_delta(
        "FLOW 1 local LND",
        lnd_before["Local LND"],
        lnd_after["Local LND"],
        -lnd_outflow,
        f"lnd_channel_point={config.lnd_channel_point}",
    )
    assert_balance_delta(
        "FLOW 1 relay LND",
        lnd_before["Relay LND"],
        lnd_after["Relay LND"],
        lnd_outflow,
        f"lnd_channel_point={config.lnd_channel_point}",
    )

    print_relay_flow_1_summary(
        config,
        payment_hash=payment_hash,
        principal=principal,
        cch_fee=cch_fee,
        lnd_outflow=lnd_outflow,
        lightning_route_fee=lightning_route_fee,
        lnd_before=lnd_before,
        lnd_after=lnd_after,
        fiber_before=fiber_before,
        fiber_after=fiber_after,
    )
    return {
        "payment_hash": payment_hash,
        "principal": principal,
        "cch_fee": cch_fee,
        "lnd_outflow": lnd_outflow,
        "lightning_route_fee": lightning_route_fee,
        "fiber_route_fee": None,
        "fiber_before": fiber_before,
        "fiber_after": fiber_after,
        "lnd_before": lnd_before,
        "lnd_after": lnd_after,
    }


def run_relay_flow_2_cwbtc_to_btc(
    config: RelayFiberSwapSmokeConfig,
    flow_1: dict,
    local_lnd_pubkey: str,
):
    """Pay cWBTC and receive BTC through relay LND on the local LND."""

    btc_principal = flow_1["lnd_outflow"]
    fiber_before = fiber_balances(config)
    lnd_before = relay_lnd_balances(config)
    require_spendable(
        "fiber2 outbound cWBTC (principal and route-fee preflight)",
        fiber_before["fiber2"],
        btc_principal + config.max_fiber_fee,
    )
    require_spendable(
        "relay LND outbound BTC on the final channel",
        lnd_before["Relay LND spendable"],
        btc_principal,
    )

    (
        btc_invoice,
        payment_hash,
        _route_hints,
        hint_fee_msat,
    ) = create_private_relay_lnd_invoice(
        config,
        btc_principal,
        local_lnd_pubkey,
    )
    order = create_swap_order(
        config,
        "/api/swap/ckb-to-btc",
        {"btc_pay_req": btc_invoice, "currency": config.currency},
        payment_hash,
    )
    validate_new_order(
        order,
        payment_hash,
        btc_invoice,
        "ckb-to-btc",
        config.lnd_network,
    )
    fiber_invoice = invoice_string(order["incoming_invoice"], "Fiber")
    parsed_fiber_invoice = parse_fiber_invoice(config, fiber_invoice)
    assert parsed_fiber_invoice["payment_hash"] == payment_hash

    cch_fee = hex_to_int(order["fee_sats"])
    require_cch_fee_within_limit(config, "cWBTC → BTC via relay LND", cch_fee)
    fiber_invoice_amount = hex_to_int(order["amount_sats"])
    assert fiber_invoice_amount == btc_principal + cch_fee
    assert parsed_fiber_invoice["amount"] == fiber_invoice_amount

    require_spendable(
        "fiber2 outbound cWBTC",
        fiber_before["fiber2"],
        fiber_invoice_amount + config.max_fiber_fee,
    )
    payment_args = [
        "payment",
        "send_payment",
        "--invoice",
        fiber_invoice,
        "--trampoline-hops",
        json.dumps([f"0x{config.bottle_pubkey}"], separators=(",", ":")),
        "--max-fee-amount",
        str(config.max_fiber_fee),
        "--timeout",
        str(config.payment_timeout),
        "--udt-type-script",
        json.dumps(config.udt_script, separators=(",", ":")),
    ]
    dry_run = fiber_swap_fnn(config, payment_args + ["--dry-run", "true"])
    assert canonical_hash(dry_run["payment_hash"]) == payment_hash
    assert dry_run.get("failed_error") is None, f"Fiber dry-run failed: {dry_run}"

    try:
        payment = fiber_swap_fnn(config, payment_args)
    except AssertionError as exc:
        if "command timed out" not in str(exc):
            raise
        print(
            "fnn-cli response timed out after payment submission; reconciling "
            f"Fiber payment {payment_hash} by hash"
        )
    else:
        assert canonical_hash(payment["payment_hash"]) == payment_hash
    fiber_payment, _final_order, lnd_invoice = wait_flow_2_success(
        config,
        payment_hash,
        btc_principal,
        config.lnd_container,
    )
    fiber_route_fee = hex_to_int(fiber_payment.get("fee", 0))
    assert fiber_route_fee <= config.max_fiber_fee

    target_route_ids = lnd_channel_route_ids(get_relay_lnd_channel(config))
    verify_settled_invoice_channel(
        lnd_invoice,
        target_route_ids,
        btc_principal,
        channel_description="configured local-LND/relay-LND channel",
        lnd_label=config.lnd_container,
    )

    fiber_after = fiber_balances(config)
    lnd_after = wait_relay_lnd_balance_delta(
        config,
        lnd_before,
        btc_principal,
        -btc_principal,
    )
    fiber_total = fiber_invoice_amount + fiber_route_fee
    assert_balance_delta(
        "FLOW 2 fiber2",
        fiber_before["fiber2"],
        fiber_after["fiber2"],
        -fiber_total,
        f"fiber_channel_id={config.fiber_channel_id}",
    )
    assert_balance_delta(
        "FLOW 2 Bottle",
        fiber_before["Bottle"],
        fiber_after["Bottle"],
        fiber_total,
        f"fiber_channel_id={config.fiber_channel_id}",
    )
    assert_balance_delta(
        "FLOW 2 local LND",
        lnd_before["Local LND"],
        lnd_after["Local LND"],
        btc_principal,
        f"lnd_channel_point={config.lnd_channel_point}",
    )
    assert_balance_delta(
        "FLOW 2 relay LND",
        lnd_before["Relay LND"],
        lnd_after["Relay LND"],
        -btc_principal,
        f"lnd_channel_point={config.lnd_channel_point}",
    )
    assert lnd_after["Local LND"] == flow_1["lnd_before"]["Local LND"]
    assert lnd_after["Relay LND"] == flow_1["lnd_before"]["Relay LND"]

    print_relay_flow_2_summary(
        config,
        payment_hash=payment_hash,
        btc_principal=btc_principal,
        cch_fee=cch_fee,
        fiber_route_fee=fiber_route_fee,
        fiber_total=fiber_total,
        relay_hint_fee_msat_value=hint_fee_msat,
        lnd_before=lnd_before,
        lnd_after=lnd_after,
        fiber_before=fiber_before,
        fiber_after=fiber_after,
    )
    return {
        "payment_hash": payment_hash,
        "btc_principal": btc_principal,
        "cch_fee": cch_fee,
        "fiber_route_fee": fiber_route_fee,
        "lightning_hint_fee_msat": hint_fee_msat,
        "fiber_total": fiber_total,
        "fiber_after": fiber_after,
        "lnd_after": lnd_after,
    }


def build_relay_fiber_swap_smoke_report(
    config,
    duration_seconds,
    flow_1,
    flow_2,
):
    fiber2_net = (
        flow_2["fiber_after"]["fiber2"] - flow_1["fiber_before"]["fiber2"]
    )
    local_lnd_net = (
        flow_2["lnd_after"]["Local LND"]
        - flow_1["lnd_before"]["Local LND"]
    )
    flow_1_fiber_fee = (
        "not exposed"
        if flow_1["fiber_route_fee"] is None
        else f"{flow_1['fiber_route_fee']:,} raw cWBTC"
    )
    return {
        "duration_seconds": round(duration_seconds, 2),
        "topology": (
            f"{config.lnd_container} ↔ relay LND ↔ FiberSwap CCH LND; "
            "fiber2 ↔ Bottle ↔ FiberSwap CCH FNN"
        ),
        "flows": [
            {
                "direction": "BTC → cWBTC",
                "paid": (
                    f"{config.lnd_container} paid "
                    f"{flow_1['lnd_outflow']:,} sats"
                ),
                "received": (
                    f"fiber2 received {flow_1['principal']:,} raw cWBTC"
                ),
            },
            {
                "direction": "cWBTC → BTC",
                "paid": (
                    f"fiber2 paid {flow_2['fiber_total']:,} raw cWBTC"
                ),
                "received": (
                    f"{config.lnd_container} received "
                    f"{flow_2['btc_principal']:,} sats"
                ),
            },
        ],
        "fees": {
            "CCH": (
                f"{flow_1['cch_fee']:,} sats + "
                f"{flow_2['cch_fee']:,} raw cWBTC"
            ),
            "Lightning": (
                "FLOW 1 "
                f"{format_msat_as_sats(flow_1['lightning_route_fee'] * 1000)}; "
                "FLOW 2 "
                f"{format_msat_as_sats(flow_2['lightning_hint_fee_msat'])} "
                "final-hop hint (total not exposed)"
            ),
            "Fiber": (
                f"FLOW 1 {flow_1_fiber_fee}; "
                f"FLOW 2 {flow_2['fiber_route_fee']:,} raw cWBTC"
            ),
        },
        "net": {
            "fiber2": f"{fiber2_net:+,} raw cWBTC",
            config.lnd_container: f"{local_lnd_net:+,} sats",
        },
    }


def test_fiber_swap_via_relay_lnd_bidirectional():
    started_at = time.monotonic()
    config = RelayFiberSwapSmokeConfig.from_env()

    info = fiber_swap_lncli_json(config, ["getinfo"])
    local_lnd_pubkey = info["identity_pubkey"]
    if config.lnd_d_pubkey:
        assert local_lnd_pubkey == config.lnd_d_pubkey
    assert info.get("synced_to_chain") is True
    assert info.get("synced_to_graph") is True
    fiber_swap_fnn(config, ["info"])
    wait_relay_lnd_channel_quiescent(config)
    validate_relay_to_fiber_swap_channel(config)
    wait_fiber_channel_quiescent(config)

    print_asset_convention(config.lnd_network)
    print_fiber_swap_topology_key()
    print_relay_topology_key(config)
    flow_1 = run_relay_flow_1_btc_to_cwbtc(config)
    flow_2 = run_relay_flow_2_cwbtc_to_btc(
        config,
        flow_1,
        local_lnd_pubkey,
    )

    expected_fiber_net = flow_1["principal"] - flow_2["fiber_total"]
    actual_fiber_net = (
        flow_2["fiber_after"]["fiber2"] - flow_1["fiber_before"]["fiber2"]
    )
    assert actual_fiber_net == expected_fiber_net

    print("\n" + "=" * 100)
    print("DAILY ROUND SUMMARY: both FiberSwap CCH relay-LND directions passed")
    print(
        f"{config.lnd_container} liquidity      : net 0 sats "
        "(FLOW 2 restored FLOW 1's value plus relay fee)"
    )
    print(
        f"fiber2 liquidity       : {actual_fiber_net:+,} raw cWBTC "
        "(both CCH fees, FLOW 1 Lightning relay fee, and aggregate Fiber "
        "routing fee are real costs)"
    )
    print("=" * 100)
    emit_smoke_report(
        build_relay_fiber_swap_smoke_report(
            config,
            time.monotonic() - started_at,
            flow_1,
            flow_2,
        )
    )
