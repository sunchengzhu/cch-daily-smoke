"""Daily CCH smoke check against an already deployed testnet environment."""

import json
import os
import shlex
import subprocess
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest

from smoke_report import emit_smoke_report

CWBTC_SCRIPT = {
    "code_hash": "0x25c29dc317811a6f6f3985a7a9ebc4838bd388d19d0feeecf0bcd60f6c0975bb",
    "hash_type": "type",
    "args": "0x9a1086531ed6dc69e0bd44cef5278e03faf3015b31aff60b08fb87663ce8507100000000",
}
DAILY_SMOKE_AMOUNT_SATS = 100
CCH_STARTUP_RETRY_DELAY_SECONDS = 1.0
CCH_STARTUP_RETRY_MAX_DELAY_SECONDS = 8.0
RAW_FNN_AUTH_ENV_VARS = (
    "CCH_SMOKE_FNN_AUTH_TOKEN",
    "CCH_FIBER_SWAP_FNN_AUTH_TOKEN",
)

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
            os.environ.get("CCH_SMOKE_UDT_SCRIPT_JSON", json.dumps(CWBTC_SCRIPT))
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
    safe_args = list(args)
    sensitive_values = [
        value
        for name in RAW_FNN_AUTH_ENV_VARS
        if (value := os.environ.get(name))
    ]
    if "--auth-token" in safe_args:
        token_index = safe_args.index("--auth-token") + 1
        if token_index < len(safe_args):
            sensitive_values.append(str(safe_args[token_index]))
            safe_args[token_index] = "***"

    # A manual raw-token fallback is converted to an auth file before this
    # call. Do not also leak the original value into the child environment.
    child_env = os.environ.copy()
    for name in RAW_FNN_AUTH_ENV_VARS:
        child_env.pop(name, None)

    command = " ".join(shlex.quote(str(a)) for a in safe_args)

    def redact(value):
        if value is None:
            return ""
        if isinstance(value, bytes):
            value = value.decode(errors="replace")
        value = str(value)
        for secret in sensitive_values:
            value = value.replace(secret, "***")
        return value

    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=child_env,
        )
    except subprocess.TimeoutExpired as exc:
        raise AssertionError(
            f"command timed out after {timeout}s: {command}\n"
            f"stdout:\n{redact(exc.stdout)}\nstderr:\n{redact(exc.stderr)}"
        ) from None

    if result.returncode != 0:
        raise AssertionError(
            f"command failed ({result.returncode}): {command}\n"
            f"stdout:\n{redact(result.stdout)}\nstderr:\n{redact(result.stderr)}"
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


@contextmanager
def fnn_auth_args(token_file_env, *token_envs):
    """Yield file-based fnn-cli auth arguments without putting a token in argv."""

    token_file = os.environ.get(token_file_env)
    if token_file:
        yield ["--auth-token-file", token_file]
        return

    token = next(
        (os.environ.get(name) for name in token_envs if os.environ.get(name)),
        None,
    )
    if not token:
        yield []
        return

    # Manual runs may still provide the token as an environment variable. Keep
    # the subprocess argv safe in that case, and always remove the temporary
    # credential file when the command returns or raises.
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix="cch-fnn-auth-",
        dir=os.environ.get("RUNNER_TEMP") or None,
    ) as auth_file:
        os.fchmod(auth_file.fileno(), 0o600)
        auth_file.write(token)
        auth_file.flush()
        yield ["--auth-token-file", auth_file.name]


def fnn(config, rpc_url, args, timeout=None):
    base_cmd = [config.fnn_cli, "-u", rpc_url, "-o", "json", "--no-banner"]
    with fnn_auth_args(
        "CCH_SMOKE_FNN_AUTH_TOKEN_FILE",
        "CCH_SMOKE_FNN_AUTH_TOKEN",
    ) as auth_args:
        cmd = base_cmd + auth_args + args
        return parse_json(run_cmd(cmd, timeout or config.command_timeout))


def cch_initialization_retry_reason(exc, method):
    """Describe a startup-time CCH error that is safe to retry unchanged."""

    message = str(exc).lower()
    if "cch startup recovery is still initializing" in message:
        return "startup recovery in progress"
    if "rpc error (code -32000): timeout" in message:
        return "legacy actor RPC timeout"
    if (
        method == "receive_btc"
        and "receive_btc order creation" in message
        and "already being recovered" in message
    ):
        return "receive_btc order creation is already being recovered"
    return None


def is_retryable_cch_initialization_error(exc, method):
    """Return whether the same startup-time CCH request can be retried safely."""

    return cch_initialization_retry_reason(exc, method) is not None


def call_cch_mutation_after_startup(config, args):
    """Retry one exact CCH mutation while startup recovery becomes ready.

    PR #1607 returns an explicit initialization error. v0.9.0-rc7 can instead
    hit the legacy one-second actor RPC timeout; its closed-port and durable
    creation guards make retrying the unchanged request safe.
    """

    method = args[1]
    deadline = time.monotonic() + config.wait_timeout
    retry_delay = CCH_STARTUP_RETRY_DELAY_SECONDS
    attempt = 0
    while True:
        attempt += 1
        try:
            return fnn(config, config.f1_rpc, args)
        except AssertionError as exc:
            retry_reason = cch_initialization_retry_reason(exc, method)
            if retry_reason is None:
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise
            sleep_seconds = min(retry_delay, remaining)
            print(
                f"CCH {method} retryable startup state after attempt {attempt}: "
                f"{retry_reason}; "
                f"retrying in {sleep_seconds:.1f}s"
            )
            time.sleep(sleep_seconds)
            retry_delay = min(
                retry_delay * 2,
                CCH_STARTUP_RETRY_MAX_DELAY_SECONDS,
            )


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


def receive_btc_amounts(reported_amount_sats, principal_sats, fee_sats, fiber_source):
    lightning_amount_sats = principal_sats + fee_sats
    expected_reported_amounts = (
        {lightning_amount_sats}
        if fiber_source in {"develop", "pr"}
        else {principal_sats, lightning_amount_sats}
    )
    if reported_amount_sats not in expected_reported_amounts:
        raise AssertionError(
            "unexpected receive_btc amount: "
            f"{reported_amount_sats}; expected one of "
            f"{sorted(expected_reported_amounts)}"
        )
    return principal_sats, lightning_amount_sats


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


def wait_fiber_payment_status(config, payment_hash, expected, rpc_url=None):
    payment_rpc = rpc_url or config.f2_rpc

    def check():
        payment = fnn(
            config,
            payment_rpc,
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


def wait_lnd_payment_succeeded(config, node_name, payment_hash):
    hash_without_prefix = payment_hash.removeprefix("0x")

    def check():
        payments = lncli_json(
            config,
            node_name,
            [
                "listpayments",
                "--include_incomplete",
                "--max_payments=100",
            ],
        )
        for payment in payments.get("payments", []):
            if payment.get("payment_hash") != hash_without_prefix:
                continue
            if payment.get("status") == "FAILED":
                pytest.fail(
                    f"{node_name} payment {payment_hash} failed: "
                    f"{payment.get('failure_reason')}"
                )
            return payment if payment.get("status") == "SUCCEEDED" else None
        return None

    return wait_until(
        f"{node_name} payment {payment_hash} to succeed",
        check,
        config.wait_timeout,
    )


def active_lnd_channel(
    node_channels,
    remote_pubkey,
    channel_id=None,
    channel_point=None,
):
    matches = [
        channel
        for channel in node_channels.get("channels", [])
        if channel.get("remote_pubkey") == remote_pubkey
        and channel.get("active", True)
        and (channel_id is None or channel.get("chan_id") == channel_id)
        and (
            channel_point is None
            or channel.get("channel_point") == channel_point
        )
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


def lnd_channel_balances_from_a(config, channel_point=None):
    lnd_b_pubkey = lncli_json(config, "lnd-b", ["getinfo"])["identity_pubkey"]
    channel = active_lnd_channel(
        lncli_json(config, "lnd-a", ["listchannels"]),
        lnd_b_pubkey,
        config.lnd_channel_id,
        channel_point,
    )
    return {
        "chan_id": channel["chan_id"],
        "channel_point": channel["channel_point"],
        "lnd_a": int(channel["local_balance"]),
        "lnd_b": int(channel["remote_balance"]),
        "pending_htlcs_count": len(channel.get("pending_htlcs") or []),
    }


def wait_lnd_channel_balance_delta(
    config,
    before,
    expected_lnd_a_delta,
    expected_lnd_b_delta,
    interval=0.5,
):
    """Wait for LND's channel snapshot to reflect a completed payment."""

    channel_point = before["channel_point"]
    expected_lnd_a = before["lnd_a"] + expected_lnd_a_delta
    expected_lnd_b = before["lnd_b"] + expected_lnd_b_delta
    deadline = time.monotonic() + config.wait_timeout
    last = None
    last_error = None
    while time.monotonic() < deadline:
        try:
            last = lnd_channel_balances_from_a(config, channel_point)
            last_error = None
        except pytest.fail.Exception as exc:
            last_error = str(exc)
            time.sleep(interval)
            continue
        except Exception as exc:  # listchannels/getinfo are read-only queries.
            last_error = f"{type(exc).__name__}: {exc}"
            time.sleep(interval)
            continue
        if (
            last["pending_htlcs_count"] == 0
            and last["lnd_a"] == expected_lnd_a
            and last["lnd_b"] == expected_lnd_b
        ):
            return last
        time.sleep(interval)
    raise AssertionError(
        "timed out waiting for LND channel balance update: "
        f"channel_point={channel_point}, "
        f"expected_lnd_a={expected_lnd_a}, expected_lnd_b={expected_lnd_b}, "
        f"last={last}, last_error={last_error}"
    )


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
        "pending_htlcs_count": len(channel.get("pending_htlcs") or []),
    }


def add_lnd_invoice(config, node_name, amount_sats, memo):
    return lncli_json(
        config,
        node_name,
        ["addinvoice", f"--amt={amount_sats}", f"--memo={memo}"],
    )


def pay_lnd_invoice(config, node_name, pay_req, timeout=None):
    return lncli_raw(
        config,
        node_name,
        ["payinvoice", pay_req, "--force"],
        timeout=timeout or max(config.command_timeout, 120),
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
            "no ready cWBTC Fiber channel from fiber2 to fiber1/CCH; "
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


def format_cwbtc(amount):
    return f"{amount:,} raw cWBTC"


def display_lightning_network(lnd_network):
    return "testnet3" if lnd_network == "testnet" else lnd_network


def print_asset_convention(lnd_network):
    print("\nAsset convention: 1 sat BTC = 1 raw cWBTC for this demo")
    print(
        "Bitcoin/Lightning network: "
        f"{display_lightning_network(lnd_network)} "
        f"(LND/lncli network={lnd_network})"
    )


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
    number,
    direction,
    path,
    payment_hash,
    principal_sats,
    source_paid,
    destination_received,
    cch_fee_text,
    fiber_route_fee_text,
    lightning_route_fee_text,
    fiber_channel_id,
    fiber_before,
    fiber_after,
    lnd_before,
    lnd_after,
    show_channel_details=False,
):
    border = "=" * 88
    print(f"\n{border}")
    print(f"FLOW {number} ({direction})")
    print(f"MONEY PATH: {path}")
    print(f"Payment hash: {payment_hash}")
    print(border)
    print(
        f"Principal            : {principal_sats:,} sats ↔ "
        f"{format_cwbtc(principal_sats)}"
    )
    print(f"WHO PAID             : {source_paid}")
    print(f"WHO RECEIVED         : {destination_received}")
    print("FEE OWNERSHIP:")
    print(f"  - CCH service fee     : {cch_fee_text}")
    print(f"  - Fiber route fee     : {fiber_route_fee_text}")
    print(f"  - Lightning route fee : {lightning_route_fee_text}")
    if show_channel_details:
        print(f"\nFiber channel: {fiber_channel_id}")
    print_balance_table(
        "Fiber balances",
        "raw cWBTC",
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


def build_cch_smoke_report(
    *,
    duration_seconds,
    amount_sats,
    send_fiber_amount,
    receive_fiber_amount,
    lightning_amount,
    send_cch_fee,
    receive_cch_fee,
    send_fiber_fee,
    receive_fiber_fee,
    send_lightning_fee,
    receive_lightning_fee,
):
    return {
        "duration_seconds": round(duration_seconds, 2),
        "topology": "fiber2 ↔ fiber1/CCH; lnd-a ↔ lnd-b (local CCH)",
        "flows": [
            {
                "direction": "cWBTC → BTC",
                "paid": f"fiber2 paid {send_fiber_amount:,} raw cWBTC",
                "received": f"lnd-b received {amount_sats:,} sats",
            },
            {
                "direction": "BTC → cWBTC",
                "paid": f"lnd-b paid {lightning_amount:,} sats",
                "received": (
                    f"fiber2 received {receive_fiber_amount:,} raw cWBTC"
                ),
            },
        ],
        "fees": {
            "CCH": (
                f"{send_cch_fee:,} raw cWBTC + {receive_cch_fee:,} sats"
            ),
            "Lightning": (
                f"{send_lightning_fee + receive_lightning_fee:,} sats"
            ),
            "Fiber": f"{send_fiber_fee + receive_fiber_fee:,} raw cWBTC",
        },
        "net": {
            "fiber2": (
                f"{receive_fiber_amount - send_fiber_amount:+,} raw cWBTC"
            ),
            "lnd-b": f"{amount_sats - lightning_amount:+,} sats",
        },
    }


def test_cch_daily_smoke_bidirectional():
    started_at = time.monotonic()
    config = CchSmokeConfig.from_env()
    amount_sats = DAILY_SMOKE_AMOUNT_SATS
    print_asset_convention(config.lnd_network)

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
    send_order = call_cch_mutation_after_startup(
        config,
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
    fiber_payment = wait_fiber_payment_status(
        config,
        send_payment_hash,
        "Success",
    )
    wait_cch_order_status(config, send_payment_hash, "Success")
    wait_lnd_invoice_settled(config, "lnd-b", send_payment_hash, amount_sats)
    lnd_payment = wait_lnd_payment_succeeded(config, "lnd-a", send_payment_hash)

    send_fiber_route_fee = hex_to_int(fiber_payment["fee"])
    send_lightning_route_fee = int(lnd_payment["fee_sat"])
    assert int(lnd_payment["value_sat"]) == amount_sats
    assert send_fiber_route_fee == 0, (
        "expected zero Fiber route fee on the direct fiber2/fiber1 channel; "
        f"got {send_fiber_route_fee} raw cWBTC"
    )
    assert send_lightning_route_fee == 0, (
        "expected zero Lightning route fee on the direct lnd-a/lnd-b channel; "
        f"got {send_lightning_route_fee} sats"
    )

    fiber_after = fiber_balances_from_f2_view(config, fiber_channel_id)
    lnd_after = wait_lnd_channel_balance_delta(
        config,
        lnd_before,
        -amount_sats,
        amount_sats,
    )
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
        number=1,
        direction="cWBTC → BTC",
        path=(
            "fiber2 --cWBTC--> fiber1/CCH == CCH swap == "
            "lnd-a --BTC--> lnd-b"
        ),
        payment_hash=send_payment_hash,
        principal_sats=amount_sats,
        source_paid=f"fiber2 paid {format_cwbtc(send_fiber_amount)}",
        destination_received=f"lnd-b received {amount_sats:,} sats",
        cch_fee_text=(
            f"{send_fee_sats:,} raw cWBTC; paid by fiber2, "
            "retained by CCH at fiber1/CCH"
        ),
        fiber_route_fee_text=(
            f"{send_fiber_route_fee:,} raw cWBTC; direct "
            "fiber2 → fiber1/CCH channel, no intermediary receives a fee"
        ),
        lightning_route_fee_text=(
            f"{send_lightning_route_fee:,} sats; direct "
            "lnd-a → lnd-b channel, no intermediary receives a fee"
        ),
        fiber_channel_id=fiber_channel_id,
        fiber_before=fiber_before,
        fiber_after=fiber_after,
        lnd_before=lnd_before,
        lnd_after=lnd_after,
        show_channel_details=config.debug,
    )

    # lnd-b -> (lnd-a -> fiber1/CCH) -> fiber2
    fiber_invoice, receive_payment_hash = create_fiber_invoice(config, amount_sats)
    receive_order = call_cch_mutation_after_startup(
        config,
        ["cch", "receive_btc", "--fiber-pay-req", fiber_invoice],
    )
    assert receive_order["payment_hash"] == receive_payment_hash
    assert same_script(receive_order["wrapped_btc_type_script"], config.udt_script)
    receive_fee_sats = hex_to_int(receive_order["fee_sats"])
    receive_reported_amount = hex_to_int(receive_order["amount_sats"])
    receive_fiber_amount, lightning_amount = receive_btc_amounts(
        receive_reported_amount,
        amount_sats,
        receive_fee_sats,
        os.environ.get("CCH_SMOKE_FNN_SOURCE", "release"),
    )

    fiber_before = fiber_balances_from_f2_view(config, fiber_channel_id)
    lnd_before = lnd_channel_balances_from_a(config)
    pay_lnd_invoice(config, "lnd-b", receive_order["incoming_invoice"]["Lightning"])

    wait_cch_order_status(config, receive_payment_hash, "Success")
    fiber_payment = wait_fiber_payment_status(
        config,
        receive_payment_hash,
        "Success",
        rpc_url=config.f1_rpc,
    )
    wait_fiber_invoice_status(config, receive_payment_hash, "Paid")
    wait_lnd_invoice_settled(config, "lnd-a", receive_payment_hash, lightning_amount)
    lnd_payment = wait_lnd_payment_succeeded(config, "lnd-b", receive_payment_hash)

    receive_fiber_route_fee = hex_to_int(fiber_payment["fee"])
    receive_lightning_route_fee = int(lnd_payment["fee_sat"])
    assert int(lnd_payment["value_sat"]) == lightning_amount
    assert receive_fiber_route_fee == 0, (
        "expected zero Fiber route fee on the direct fiber1/fiber2 channel; "
        f"got {receive_fiber_route_fee} raw cWBTC"
    )
    assert receive_lightning_route_fee == 0, (
        "expected zero Lightning route fee on the direct lnd-b/lnd-a channel; "
        f"got {receive_lightning_route_fee} sats"
    )

    fiber_after = fiber_balances_from_f2_view(config, fiber_channel_id)
    lnd_after = wait_lnd_channel_balance_delta(
        config,
        lnd_before,
        lightning_amount,
        -lightning_amount,
    )
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
        number=2,
        direction="BTC → cWBTC",
        path=(
            "lnd-b --BTC--> lnd-a/CCH == CCH swap == "
            "fiber1/CCH --cWBTC--> fiber2"
        ),
        payment_hash=receive_payment_hash,
        principal_sats=receive_fiber_amount,
        source_paid=f"lnd-b paid {lightning_amount:,} sats",
        destination_received=(
            f"fiber2 received {format_cwbtc(receive_fiber_amount)}"
        ),
        cch_fee_text=(
            f"{receive_fee_sats:,} sats; paid by lnd-b, "
            "retained by CCH at lnd-a/CCH"
        ),
        fiber_route_fee_text=(
            f"{receive_fiber_route_fee:,} raw cWBTC; direct "
            "fiber1/CCH → fiber2 channel, no intermediary receives a fee"
        ),
        lightning_route_fee_text=(
            f"{receive_lightning_route_fee:,} sats; direct "
            "lnd-b → lnd-a/CCH channel, no intermediary receives a fee"
        ),
        fiber_channel_id=fiber_channel_id,
        fiber_before=fiber_before,
        fiber_after=fiber_after,
        lnd_before=lnd_before,
        lnd_after=lnd_after,
        show_channel_details=config.debug,
    )

    print("\nCCH daily smoke completed: both directions passed.")
    emit_smoke_report(
        build_cch_smoke_report(
            duration_seconds=time.monotonic() - started_at,
            amount_sats=amount_sats,
            send_fiber_amount=send_fiber_amount,
            receive_fiber_amount=receive_fiber_amount,
            lightning_amount=lightning_amount,
            send_cch_fee=send_fee_sats,
            receive_cch_fee=receive_fee_sats,
            send_fiber_fee=send_fiber_route_fee,
            receive_fiber_fee=receive_fiber_route_fee,
            send_lightning_fee=send_lightning_route_fee,
            receive_lightning_fee=receive_lightning_route_fee,
        )
    )
