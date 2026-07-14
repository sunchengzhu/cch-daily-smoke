"""Daily CCH smoke check against an already deployed testnet environment."""

import json
import os
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

MZBTC_SCRIPT = {
    "code_hash": "0x25c29dc317811a6f6f3985a7a9ebc4838bd388d19d0feeecf0bcd60f6c0975bb",
    "hash_type": "type",
    "args": "0x7275c8fb7feb81d22a47aa582c4f2487d771a1933957fe8fee9b363603487b1a00000000",
}

pytestmark = pytest.mark.skipif(
    os.environ.get("CCH_SMOKE_ENABLED") != "1",
    reason="set CCH_SMOKE_ENABLED=1 to run the live CCH daily smoke check",
)


@dataclass
class CchSmokeConfig:
    fnn_cli: str
    f1_rpc: str
    f2_rpc: str
    currency: str
    udt_script: dict
    amount_sats: int
    lnd_a_container: str
    lnd_b_container: str
    lnd_dir: str
    lnd_network: str
    channel_id: str | None
    lnd_channel_id: str | None
    lnd_topup_sats: int
    command_timeout: int
    wait_timeout: int
    debug: bool

    @classmethod
    def from_env(cls):
        fnn_cli = os.environ.get("CCH_SMOKE_FNN_CLI") or os.environ.get("FNN_CLI")
        if not fnn_cli:
            pytest.fail("CCH_SMOKE_FNN_CLI or FNN_CLI must point to fnn-cli")
        if not Path(fnn_cli).exists():
            pytest.fail(f"fnn-cli not found: {fnn_cli}")

        udt_script = json.loads(
            os.environ.get("CCH_SMOKE_UDT_SCRIPT_JSON", json.dumps(MZBTC_SCRIPT))
        )
        return cls(
            fnn_cli=fnn_cli,
            f1_rpc=os.environ.get("CCH_SMOKE_F1_RPC", "http://127.0.0.1:8227"),
            f2_rpc=os.environ.get("CCH_SMOKE_F2_RPC", "http://127.0.0.1:8229"),
            currency=os.environ.get("CCH_SMOKE_CURRENCY", "Fibt"),
            udt_script=udt_script,
            amount_sats=int(os.environ.get("CCH_SMOKE_AMOUNT_SATS", "100")),
            lnd_a_container=os.environ.get("CCH_SMOKE_LND_A_CONTAINER", "lnd-a"),
            lnd_b_container=os.environ.get("CCH_SMOKE_LND_B_CONTAINER", "lnd-b"),
            lnd_dir=os.environ.get("CCH_SMOKE_LND_DIR", "/data/.lnd"),
            lnd_network=os.environ.get("CCH_SMOKE_LND_NETWORK", "testnet4"),
            channel_id=os.environ.get("CCH_SMOKE_FIBER_CHANNEL_ID") or None,
            lnd_channel_id=os.environ.get("CCH_SMOKE_LND_CHANNEL_ID") or None,
            lnd_topup_sats=int(
                os.environ.get("CCH_SMOKE_LND_TOPUP_SATS", "3000000")
            ),
            command_timeout=int(os.environ.get("CCH_SMOKE_COMMAND_TIMEOUT", "60")),
            wait_timeout=int(os.environ.get("CCH_SMOKE_WAIT_TIMEOUT", "180")),
            debug=(
                os.environ.get("CCH_SMOKE_DEBUG", "").lower()
                in {"1", "true", "yes"}
            ),
        )


def run_cmd(args, timeout):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        safe_args = list(args)
        if "--auth-token" in safe_args:
            token_index = safe_args.index("--auth-token") + 1
            if token_index < len(safe_args):
                safe_args[token_index] = "***"
        command = " ".join(shlex.quote(str(a)) for a in safe_args)
        raise AssertionError(
            f"command failed ({result.returncode}): {command}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result.stdout.strip()


def parse_json(output):
    output = output.strip()
    if not output:
        return None
    try:
        return json.loads(output)
    except json.JSONDecodeError:
        starts = [pos for pos in (output.find("{"), output.find("[")) if pos >= 0]
        if not starts:
            raise
        return json.loads(output[min(starts) :])


def fnn(config, rpc_url, args, timeout=None):
    cmd = [config.fnn_cli, "-u", rpc_url, "-o", "json", "--no-banner"]
    if os.environ.get("CCH_SMOKE_FNN_AUTH_TOKEN"):
        cmd.extend(["--auth-token", os.environ["CCH_SMOKE_FNN_AUTH_TOKEN"]])
    if os.environ.get("CCH_SMOKE_FNN_AUTH_TOKEN_FILE"):
        cmd.extend(["--auth-token-file", os.environ["CCH_SMOKE_FNN_AUTH_TOKEN_FILE"]])
    cmd.extend(args)
    return parse_json(run_cmd(cmd, timeout or config.command_timeout))


def lncli_prefix(config, node_name):
    env_name = f"CCH_SMOKE_{node_name.upper().replace('-', '_')}_LNCLI_PREFIX"
    if os.environ.get(env_name):
        return shlex.split(os.environ[env_name])

    container = (
        config.lnd_a_container if node_name == "lnd-a" else config.lnd_b_container
    )
    return [
        "docker",
        "exec",
        container,
        "lncli",
        f"--lnddir={config.lnd_dir}",
        f"--network={config.lnd_network}",
    ]


def lncli_json(config, node_name, args, timeout=None):
    return parse_json(
        run_cmd(
            lncli_prefix(config, node_name) + args, timeout or config.command_timeout
        )
    )


def lncli_raw(config, node_name, args, timeout=None):
    return run_cmd(
        lncli_prefix(config, node_name) + args, timeout or config.command_timeout
    )


def hex_to_int(value):
    if isinstance(value, int):
        return value
    return int(value, 16) if str(value).startswith("0x") else int(value)


def same_script(left, right):
    return all(
        left.get(key) == right.get(key) for key in ("code_hash", "hash_type", "args")
    )


def state_name(channel):
    state = channel.get("state")
    return state.get("state_name") if isinstance(state, dict) else state


def wait_until(description, fn, timeout, interval=2):
    deadline = time.time() + timeout
    last_value = None
    while time.time() < deadline:
        last_value = fn()
        if last_value:
            return last_value
        time.sleep(interval)
    pytest.fail(f"timed out waiting for {description}; last value: {last_value}")


def wait_cch_order_status(config, payment_hash, expected):
    def check():
        order = fnn(
            config,
            config.f1_rpc,
            ["cch", "get_cch_order", "--payment-hash", payment_hash],
        )
        return order if order.get("status") == expected else None

    return wait_until(
        f"CCH order {payment_hash} to become {expected}",
        check,
        config.wait_timeout,
    )


def wait_fiber_payment_status(config, payment_hash, expected):
    def check():
        payment = fnn(
            config,
            config.f2_rpc,
            ["payment", "get_payment", "--payment-hash", payment_hash],
        )
        return payment if payment.get("status") == expected else None

    return wait_until(
        f"Fiber payment {payment_hash} to become {expected}",
        check,
        config.wait_timeout,
    )


def wait_fiber_invoice_status(config, payment_hash, expected):
    def check():
        invoice = fnn(
            config,
            config.f2_rpc,
            ["invoice", "get_invoice", "--payment-hash", payment_hash],
        )
        return invoice if invoice.get("status") == expected else None

    return wait_until(
        f"Fiber invoice {payment_hash} to become {expected}",
        check,
        config.wait_timeout,
    )


def wait_lnd_invoice_settled(config, node_name, payment_hash, expected_paid_sats):
    hash_without_prefix = payment_hash.removeprefix("0x")

    def check():
        invoice = lncli_json(config, node_name, ["lookupinvoice", hash_without_prefix])
        if (
            invoice.get("state") == "SETTLED"
            and int(invoice.get("amt_paid_sat", "0")) == expected_paid_sats
        ):
            return invoice
        return None

    return wait_until(
        f"{node_name} invoice {payment_hash} to settle for {expected_paid_sats} sats",
        check,
        config.wait_timeout,
    )


def active_lnd_channel(node_channels, remote_pubkey, channel_id=None):
    matches = [
        channel
        for channel in node_channels.get("channels", [])
        if channel.get("remote_pubkey") == remote_pubkey
        and channel.get("active", True)
        and (channel_id is None or channel.get("chan_id") == channel_id)
    ]
    if not matches:
        available = [
            {
                "chan_id": channel.get("chan_id"),
                "channel_point": channel.get("channel_point"),
                "remote_pubkey": channel.get("remote_pubkey"),
                "active": channel.get("active"),
            }
            for channel in node_channels.get("channels", [])
        ]
        pytest.fail(
            f"no active LND channel found for remote pubkey {remote_pubkey}; "
            f"available channels: {json.dumps(available, sort_keys=True)}"
        )
    return matches[0]


def lnd_channel_balances_from_a(config):
    lnd_b_pubkey = lncli_json(config, "lnd-b", ["getinfo"])["identity_pubkey"]
    channel = active_lnd_channel(
        lncli_json(config, "lnd-a", ["listchannels"]),
        lnd_b_pubkey,
        config.lnd_channel_id,
    )
    return {
        "chan_id": channel["chan_id"],
        "channel_point": channel["channel_point"],
        "lnd_a": int(channel["local_balance"]),
        "lnd_b": int(channel["remote_balance"]),
    }


def lnd_b_liquidity(config):
    lnd_a_pubkey = lncli_json(config, "lnd-a", ["getinfo"])["identity_pubkey"]
    channel = active_lnd_channel(
        lncli_json(config, "lnd-b", ["listchannels"]),
        lnd_a_pubkey,
        config.lnd_channel_id,
    )
    local_balance = int(channel["local_balance"])
    reserve = int(channel.get("local_chan_reserve_sat") or 0)
    return {
        "local_balance": local_balance,
        "reserve_sats": reserve,
        "spendable_sats": max(0, local_balance - reserve),
    }


def add_lnd_invoice(config, node_name, amount_sats, memo):
    return lncli_json(
        config,
        node_name,
        ["addinvoice", f"--amt={amount_sats}", f"--memo={memo}"],
    )


def pay_lnd_invoice(config, node_name, pay_req):
    return lncli_raw(
        config,
        node_name,
        ["payinvoice", pay_req, "--force"],
        timeout=max(config.command_timeout, 120),
    )


def top_up_lnd_b_if_needed(config, needed_sats):
    liquidity = lnd_b_liquidity(config)
    spendable = liquidity["spendable_sats"]
    if spendable >= needed_sats:
        return None

    reserve_shortfall = max(
        0,
        liquidity["reserve_sats"] + needed_sats - liquidity["local_balance"],
    )
    topup_sats = max(config.lnd_topup_sats, reserve_shortfall)
    print(
        "[lnd-topup] insufficient lnd-b outbound liquidity: "
        f"local={liquidity['local_balance']:,} sats, "
        f"reserve={liquidity['reserve_sats']:,} sats, "
        f"spendable={spendable:,} sats, needed={needed_sats:,} sats; "
        f"transferring {topup_sats:,} sats from lnd-a",
        flush=True,
    )
    invoice = add_lnd_invoice(
        config,
        "lnd-b",
        topup_sats,
        f"cch-smoke-topup-{int(time.time())}",
    )
    pay_lnd_invoice(config, "lnd-a", invoice["payment_request"])
    wait_lnd_invoice_settled(config, "lnd-b", invoice["r_hash"], topup_sats)
    updated = lnd_b_liquidity(config)
    print(
        "[lnd-topup] completed: "
        f"local={updated['local_balance']:,} sats, "
        f"reserve={updated['reserve_sats']:,} sats, "
        f"spendable={updated['spendable_sats']:,} sats",
        flush=True,
    )
    if updated["spendable_sats"] < needed_sats:
        raise AssertionError(
            "lnd-b outbound liquidity is still insufficient after top-up: "
            f"spendable={updated['spendable_sats']} sats, needed={needed_sats} sats"
        )
    return {
        "amount_sats": topup_sats,
        "previous_spendable_sats": spendable,
        "updated_spendable_sats": updated["spendable_sats"],
        "reserve_sats": liquidity["reserve_sats"],
    }


def f1_pubkey(config):
    return fnn(config, config.f1_rpc, ["info"])["pubkey"]


def get_fiber_channel(config, minimum_f2_local=0, channel_id=None):
    expected_channel_id = channel_id or config.channel_id
    channels = fnn(
        config,
        config.f2_rpc,
        ["channel", "list_channels", "--pubkey", f1_pubkey(config)],
    )["channels"]

    candidates = []
    for channel in channels:
        if expected_channel_id and channel.get("channel_id") != expected_channel_id:
            continue
        if state_name(channel) != "ChannelReady":
            continue
        if not same_script(
            channel.get("funding_udt_type_script") or {}, config.udt_script
        ):
            continue
        if hex_to_int(channel["local_balance"]) < minimum_f2_local:
            continue
        candidates.append(channel)

    if not candidates:
        available = [
            {
                "channel_id": channel.get("channel_id"),
                "state": state_name(channel),
                "local_balance": channel.get("local_balance"),
                "udt_args": (channel.get("funding_udt_type_script") or {}).get(
                    "args"
                ),
            }
            for channel in channels
        ]
        pytest.fail(
            "no ready mzBTC Fiber channel from fiber2 to fiber1/CCH; "
            f"available channels: {json.dumps(available, sort_keys=True)}"
        )
    return candidates[0]


def fiber_balances_from_f2_view(config, channel_id):
    channel = get_fiber_channel(config, channel_id=channel_id)
    return {
        "fiber2": hex_to_int(channel["local_balance"]),
        "fiber1_cch": hex_to_int(channel["remote_balance"]),
    }


def assert_balance_delta(label, before, after, expected_delta, details=None):
    actual_delta = after - before
    message = (
        f"{label} balance delta mismatch: before={before}, after={after}, "
        f"actual_delta={actual_delta}, expected_delta={expected_delta}"
    )
    if details:
        message += f"; {details}"
    assert actual_delta == expected_delta, message


def format_mzbtc(amount):
    return f"{amount:,} mzBTC units"


def print_asset_convention():
    print("\n资产换算（CCH Demo）：1 BTC = 1 mzBTC")


def print_balance_table(title, unit, rows):
    print(f"\n{title} ({unit})")
    print(f"{'Node':<16}{'Before':>16}{'After':>16}{'Change':>16}")
    print("-" * 64)
    for node, before, after in rows:
        before_text = f"{before:,}"
        after_text = f"{after:,}"
        change_text = f"{after - before:+,}"
        print(f"{node:<16}{before_text:>16}{after_text:>16}{change_text:>16}")


def print_flow_summary(
    path,
    payment_hash,
    principal_sats,
    cch_fee_sats,
    source_paid,
    destination_received,
    fiber_channel_id,
    fiber_before,
    fiber_after,
    lnd_before,
    lnd_after,
    show_channel_details=False,
):
    border = "=" * 88
    print(f"\n{border}")
    print(path)
    print(border)
    print(f"Payment hash         : {payment_hash}")
    print(
        f"Principal            : {principal_sats:,} sats ↔ "
        f"{format_mzbtc(principal_sats)}"
    )
    print(f"CCH fee              : {cch_fee_sats:,} sats")
    print(f"Source paid          : {source_paid}")
    print(f"Destination received : {destination_received}")
    if show_channel_details:
        print(f"\nFiber channel: {fiber_channel_id}")
    print_balance_table(
        "Fiber balances",
        "mzBTC units",
        [
            ("fiber2", fiber_before["fiber2"], fiber_after["fiber2"]),
            (
                "fiber1/CCH",
                fiber_before["fiber1_cch"],
                fiber_after["fiber1_cch"],
            ),
        ],
    )

    if show_channel_details:
        print(f"\nLND channel: {lnd_before['chan_id']}")
        print(f"LND outpoint: {lnd_before['channel_point']}")
    print_balance_table(
        "LND balances",
        "sats",
        [
            ("lnd-a", lnd_before["lnd_a"], lnd_after["lnd_a"]),
            ("lnd-b", lnd_before["lnd_b"], lnd_after["lnd_b"]),
        ],
    )


def create_fiber_invoice(config, amount_sats):
    invoice = fnn(
        config,
        config.f2_rpc,
        [
            "invoice",
            "new_invoice",
            "--amount",
            str(amount_sats),
            "--currency",
            config.currency,
            "--udt-type-script",
            json.dumps(config.udt_script, separators=(",", ":")),
            "--hash-algorithm",
            "sha256",
        ],
    )
    return invoice["invoice_address"], invoice["invoice"]["data"]["payment_hash"]


def test_cch_daily_smoke_bidirectional():
    config = CchSmokeConfig.from_env()
    amount_sats = config.amount_sats
    assert amount_sats > 0
    print_asset_convention()

    channel = get_fiber_channel(config)
    fiber_channel_id = channel["channel_id"]

    # fiber2 -> (fiber1/CCH -> lnd-a) -> lnd-b
    lnd_before = lnd_channel_balances_from_a(config)
    btc_invoice = add_lnd_invoice(
        config,
        "lnd-b",
        amount_sats,
        f"cch-smoke-fiber-to-lnd-{int(time.time())}",
    )
    send_order = fnn(
        config,
        config.f1_rpc,
        [
            "cch",
            "send_btc",
            "--btc-pay-req",
            btc_invoice["payment_request"],
            "--currency",
            config.currency,
        ],
    )
    assert same_script(send_order["wrapped_btc_type_script"], config.udt_script)
    send_payment_hash = send_order["payment_hash"]
    send_fee_sats = hex_to_int(send_order["fee_sats"])
    send_fiber_amount = hex_to_int(send_order["amount_sats"])
    assert send_fiber_amount == amount_sats + send_fee_sats

    fiber_before = fiber_balances_from_f2_view(config, fiber_channel_id)
    payment = fnn(
        config,
        config.f2_rpc,
        [
            "payment",
            "send_payment",
            "--invoice",
            send_order["incoming_invoice"]["Fiber"],
        ],
    )
    assert payment["payment_hash"] == send_payment_hash
    wait_fiber_payment_status(config, send_payment_hash, "Success")
    wait_cch_order_status(config, send_payment_hash, "Success")
    wait_lnd_invoice_settled(config, "lnd-b", send_payment_hash, amount_sats)

    fiber_after = fiber_balances_from_f2_view(config, fiber_channel_id)
    lnd_after = lnd_channel_balances_from_a(config)
    fiber_details = f"fiber_channel_id={fiber_channel_id}"
    lnd_details = (
        f"lnd_channel_id={lnd_before['chan_id']}, "
        f"lnd_outpoint={lnd_before['channel_point']}"
    )
    assert_balance_delta(
        "fiber -> lnd fiber2",
        fiber_before["fiber2"],
        fiber_after["fiber2"],
        -send_fiber_amount,
        fiber_details,
    )
    assert_balance_delta(
        "fiber -> lnd fiber1/CCH",
        fiber_before["fiber1_cch"],
        fiber_after["fiber1_cch"],
        send_fiber_amount,
        fiber_details,
    )
    assert_balance_delta(
        "fiber -> lnd lnd-a",
        lnd_before["lnd_a"],
        lnd_after["lnd_a"],
        -amount_sats,
        lnd_details,
    )
    assert_balance_delta(
        "fiber -> lnd lnd-b",
        lnd_before["lnd_b"],
        lnd_after["lnd_b"],
        amount_sats,
        lnd_details,
    )
    print_flow_summary(
        path="FLOW 1: fiber2 -> (fiber1/CCH -> lnd-a) -> lnd-b",
        payment_hash=send_payment_hash,
        principal_sats=amount_sats,
        cch_fee_sats=send_fee_sats,
        source_paid=format_mzbtc(send_fiber_amount),
        destination_received=f"{amount_sats:,} sats",
        fiber_channel_id=fiber_channel_id,
        fiber_before=fiber_before,
        fiber_after=fiber_after,
        lnd_before=lnd_before,
        lnd_after=lnd_after,
        show_channel_details=config.debug,
    )

    # lnd-b -> (lnd-a -> fiber1/CCH) -> fiber2
    fiber_invoice, receive_payment_hash = create_fiber_invoice(config, amount_sats)
    receive_order = fnn(
        config,
        config.f1_rpc,
        ["cch", "receive_btc", "--fiber-pay-req", fiber_invoice],
    )
    assert receive_order["payment_hash"] == receive_payment_hash
    assert same_script(receive_order["wrapped_btc_type_script"], config.udt_script)
    receive_fee_sats = hex_to_int(receive_order["fee_sats"])
    receive_fiber_amount = hex_to_int(receive_order["amount_sats"])
    assert receive_fiber_amount == amount_sats
    lightning_amount = receive_fiber_amount + receive_fee_sats

    fiber_before = fiber_balances_from_f2_view(config, fiber_channel_id)
    lnd_before = lnd_channel_balances_from_a(config)
    pay_lnd_invoice(config, "lnd-b", receive_order["incoming_invoice"]["Lightning"])

    wait_cch_order_status(config, receive_payment_hash, "Success")
    wait_fiber_invoice_status(config, receive_payment_hash, "Paid")
    wait_lnd_invoice_settled(config, "lnd-a", receive_payment_hash, lightning_amount)

    fiber_after = fiber_balances_from_f2_view(config, fiber_channel_id)
    lnd_after = lnd_channel_balances_from_a(config)
    fiber_details = f"fiber_channel_id={fiber_channel_id}"
    lnd_details = (
        f"lnd_channel_id={lnd_before['chan_id']}, "
        f"lnd_outpoint={lnd_before['channel_point']}"
    )
    assert_balance_delta(
        "lnd -> fiber fiber2",
        fiber_before["fiber2"],
        fiber_after["fiber2"],
        receive_fiber_amount,
        fiber_details,
    )
    assert_balance_delta(
        "lnd -> fiber fiber1/CCH",
        fiber_before["fiber1_cch"],
        fiber_after["fiber1_cch"],
        -receive_fiber_amount,
        fiber_details,
    )
    assert_balance_delta(
        "lnd -> fiber lnd-a",
        lnd_before["lnd_a"],
        lnd_after["lnd_a"],
        lightning_amount,
        lnd_details,
    )
    assert_balance_delta(
        "lnd -> fiber lnd-b",
        lnd_before["lnd_b"],
        lnd_after["lnd_b"],
        -lightning_amount,
        lnd_details,
    )
    print_flow_summary(
        path="FLOW 2: lnd-b -> (lnd-a -> fiber1/CCH) -> fiber2",
        payment_hash=receive_payment_hash,
        principal_sats=receive_fiber_amount,
        cch_fee_sats=receive_fee_sats,
        source_paid=f"{lightning_amount:,} sats",
        destination_received=format_mzbtc(receive_fiber_amount),
        fiber_channel_id=fiber_channel_id,
        fiber_before=fiber_before,
        fiber_after=fiber_after,
        lnd_before=lnd_before,
        lnd_after=lnd_after,
        show_channel_details=config.debug,
    )

    print("\nCCH daily smoke completed: both directions passed.")
