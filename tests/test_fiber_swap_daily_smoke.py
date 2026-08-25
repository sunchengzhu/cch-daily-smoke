"""Daily FiberSwap CCH smoke check through local fiber2 and lnd-d."""

from __future__ import annotations

import json
import os
import shlex
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import pytest

from test_cch_daily_smoke import (
    CWBTC_SCRIPT,
    assert_balance_delta,
    fnn_auth_args,
    hex_to_int,
    parse_json,
    run_cmd,
    same_script,
    state_name,
)


pytestmark = pytest.mark.skipif(
    os.environ.get("CCH_FIBER_SWAP_SMOKE_ENABLED") != "1",
    reason="set CCH_FIBER_SWAP_SMOKE_ENABLED=1 to run the FiberSwap live smoke",
)


DEFAULT_FIBER_SWAP_LND_PUBKEY = (
    "03c7796e27b079b25ea5c9f02dbb289ab1720248ecdd25dceed7cd489f2fe6290d"
)
DEFAULT_LND_D_PUBKEY = (
    "027431fbdbbc67df1ed0cf30568fe8ab04ef2fbff296ebcc0c826dc01e923799f5"
)
DEFAULT_BOTTLE_PUBKEY = (
    "02b6d4e3ab86a2ca2fad6fae0ecb2e1e559e0b911939872a90abdda6d20302be71"
)
DEFAULT_LND_CHANNEL_POINT = (
    "70fdfd0dd7960b1b7ca1f562ae031b3a37925fa957eb97c97536f82ed5f37c35:1"
)
DEFAULT_FIBER_CHANNEL_ID = (
    "0x4b6513e2d1ad8341366d3b0733f00306aee7e2db21e669a30aba5d33a1ed5536"
)


@dataclass(frozen=True)
class FiberSwapSmokeConfig:
    fnn_cli: str
    f2_rpc: str
    api_base_url: str
    currency: str
    udt_script: dict
    amount_sats: int
    max_cch_fee_sats: int
    max_fiber_fee: int
    lnd_fee_limit_sats: int
    payment_timeout: int
    lnd_container: str
    lnd_dir: str
    lnd_network: str
    lnd_d_pubkey: str
    fiber_swap_lnd_pubkey: str
    lnd_channel_point: str
    bottle_pubkey: str
    fiber_channel_id: str
    command_timeout: int
    wait_timeout: int
    debug: bool

    @classmethod
    def from_env(cls) -> "FiberSwapSmokeConfig":
        fnn_cli = os.environ.get("CCH_FIBER_SWAP_FNN_CLI") or os.environ.get(
            "CCH_SMOKE_FNN_CLI"
        )
        if not fnn_cli:
            pytest.fail("CCH_FIBER_SWAP_FNN_CLI must point to fnn-cli")
        if not Path(fnn_cli).exists():
            pytest.fail(f"fnn-cli not found: {fnn_cli}")

        config = cls(
            fnn_cli=fnn_cli,
            f2_rpc=os.environ.get(
                "CCH_FIBER_SWAP_F2_RPC", "http://127.0.0.1:8229"
            ),
            api_base_url=os.environ.get(
                "CCH_FIBER_SWAP_API_BASE_URL", "https://fiber-swap-api.retric.uk"
            ).rstrip("/"),
            currency=os.environ.get("CCH_FIBER_SWAP_CURRENCY", "Fibt"),
            udt_script=json.loads(
                os.environ.get(
                    "CCH_FIBER_SWAP_UDT_SCRIPT_JSON", json.dumps(CWBTC_SCRIPT)
                )
            ),
            amount_sats=int(os.environ.get("CCH_FIBER_SWAP_AMOUNT_SATS", "100")),
            max_cch_fee_sats=int(
                os.environ.get("CCH_FIBER_SWAP_MAX_CCH_FEE_SATS", "100")
            ),
            max_fiber_fee=int(
                os.environ.get("CCH_FIBER_SWAP_MAX_FIBER_FEE", "100")
            ),
            lnd_fee_limit_sats=int(
                os.environ.get("CCH_FIBER_SWAP_LND_FEE_LIMIT_SATS", "10")
            ),
            payment_timeout=int(
                os.environ.get("CCH_FIBER_SWAP_PAYMENT_TIMEOUT", "120")
            ),
            lnd_container=os.environ.get(
                "CCH_FIBER_SWAP_LND_D_CONTAINER", "lnd-d"
            ),
            lnd_dir=os.environ.get("CCH_FIBER_SWAP_LND_DIR", "/data/.lnd"),
            lnd_network=os.environ.get("CCH_FIBER_SWAP_LND_NETWORK", "testnet"),
            lnd_d_pubkey=os.environ.get(
                "CCH_FIBER_SWAP_LND_D_PUBKEY", DEFAULT_LND_D_PUBKEY
            ),
            fiber_swap_lnd_pubkey=os.environ.get(
                "CCH_FIBER_SWAP_LND_REMOTE_PUBKEY", DEFAULT_FIBER_SWAP_LND_PUBKEY
            ),
            lnd_channel_point=os.environ.get(
                "CCH_FIBER_SWAP_LND_CHANNEL_POINT", DEFAULT_LND_CHANNEL_POINT
            ),
            bottle_pubkey=os.environ.get(
                "CCH_FIBER_SWAP_BOTTLE_PUBKEY", DEFAULT_BOTTLE_PUBKEY
            ),
            fiber_channel_id=os.environ.get(
                "CCH_FIBER_SWAP_FIBER_CHANNEL_ID", DEFAULT_FIBER_CHANNEL_ID
            ),
            command_timeout=int(
                os.environ.get("CCH_FIBER_SWAP_COMMAND_TIMEOUT", "60")
            ),
            wait_timeout=int(os.environ.get("CCH_FIBER_SWAP_WAIT_TIMEOUT", "180")),
            debug=os.environ.get("CCH_FIBER_SWAP_DEBUG", "").lower()
            in {"1", "true", "yes"},
        )
        for name, value in (
            ("CCH_FIBER_SWAP_AMOUNT_SATS", config.amount_sats),
            ("CCH_FIBER_SWAP_MAX_CCH_FEE_SATS", config.max_cch_fee_sats),
            ("CCH_FIBER_SWAP_MAX_FIBER_FEE", config.max_fiber_fee),
            ("CCH_FIBER_SWAP_LND_FEE_LIMIT_SATS", config.lnd_fee_limit_sats),
            ("CCH_FIBER_SWAP_PAYMENT_TIMEOUT", config.payment_timeout),
            ("CCH_FIBER_SWAP_COMMAND_TIMEOUT", config.command_timeout),
            ("CCH_FIBER_SWAP_WAIT_TIMEOUT", config.wait_timeout),
        ):
            if value <= 0:
                pytest.fail(f"{name} must be positive")
        if config.payment_timeout >= config.wait_timeout:
            pytest.fail(
                "CCH_FIBER_SWAP_PAYMENT_TIMEOUT must be less than "
                "CCH_FIBER_SWAP_WAIT_TIMEOUT"
            )
        return config


def canonical_hash(value: str) -> str:
    return f"0x{str(value).lower().removeprefix('0x')}"


def invoice_string(value, kind: str) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        invoice = value.get(kind)
        if isinstance(invoice, str):
            return invoice
    raise AssertionError(f"missing {kind} invoice in API response: {value!r}")


def fiber_swap_fnn(config: FiberSwapSmokeConfig, args: list[str], timeout=None):
    base_cmd = [
        config.fnn_cli,
        "-u",
        config.f2_rpc,
        "-o",
        "json",
        "--no-banner",
    ]
    with fnn_auth_args(
        "CCH_FIBER_SWAP_FNN_AUTH_TOKEN_FILE",
        "CCH_FIBER_SWAP_FNN_AUTH_TOKEN",
        "CCH_SMOKE_FNN_AUTH_TOKEN",
    ) as auth_args:
        cmd = base_cmd + auth_args + args
        return parse_json(run_cmd(cmd, timeout or config.command_timeout))


def fiber_swap_lncli_prefix(config: FiberSwapSmokeConfig) -> list[str]:
    override = os.environ.get("CCH_FIBER_SWAP_LND_D_LNCLI_PREFIX")
    if override:
        return shlex.split(override)
    return [
        "docker",
        "exec",
        config.lnd_container,
        "lncli",
        f"--lnddir={config.lnd_dir}",
        f"--network={config.lnd_network}",
    ]


def fiber_swap_lncli_json(
    config: FiberSwapSmokeConfig, args: list[str], timeout=None
):
    return parse_json(
        run_cmd(
            fiber_swap_lncli_prefix(config) + args,
            timeout or config.command_timeout,
        )
    )


def fiber_swap_lncli_raw(config: FiberSwapSmokeConfig, args: list[str], timeout=None):
    return run_cmd(
        fiber_swap_lncli_prefix(config) + args,
        timeout or config.command_timeout,
    )


def api_json(
    config: FiberSwapSmokeConfig,
    method: str,
    path: str,
    payload: dict | None = None,
):
    data = None
    headers = {
        "Accept": "application/json",
        "User-Agent": "cch-daily-smoke/1.0",
    }
    if payload is not None:
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        f"{config.api_base_url}{path}",
        data=data,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(
            request, timeout=config.command_timeout
        ) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise AssertionError(
            f"FiberSwap API {method} {path} returned HTTP {exc.code}: {body}"
        ) from exc
    except urllib.error.URLError as exc:
        raise AssertionError(
            f"FiberSwap API {method} {path} failed: {exc.reason}"
        ) from exc
    except TimeoutError as exc:
        raise AssertionError(
            f"FiberSwap API {method} {path} timed out"
        ) from exc
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise AssertionError(
            f"FiberSwap API {method} {path} returned invalid JSON: {body}"
        ) from exc


def create_swap_order(
    config: FiberSwapSmokeConfig,
    path: str,
    payload: dict,
    payment_hash: str,
):
    """Create an order, reconciling a possibly lost POST response by hash."""

    try:
        return api_json(config, "POST", path, payload)
    except AssertionError as post_error:
        try:
            order = api_json(config, "GET", f"/api/order/{payment_hash}")
        except AssertionError:
            raise post_error
        print(
            "FiberSwap order POST response was ambiguous; recovered the "
            f"existing order by payment hash {payment_hash}"
        )
        return order


def validate_new_order(
    order: dict,
    payment_hash: str,
    outgoing_pay_req: str,
    direction: str,
    network: str,
):
    assert canonical_hash(order.get("payment_hash")) == payment_hash, (
        "FiberSwap order payment hash does not match the submitted invoice"
    )
    assert order.get("outgoing_pay_req") == outgoing_pay_req, (
        "FiberSwap order outgoing invoice does not match the submitted invoice"
    )
    assert order.get("direction") == direction, (
        f"FiberSwap order direction mismatch: {order.get('direction')!r} != "
        f"{direction!r}"
    )
    assert order.get("network") == network, (
        f"FiberSwap order network mismatch: {order.get('network')!r} != "
        f"{network!r}"
    )
    assert order.get("status") == "Pending", (
        f"FiberSwap order is not payable: status={order.get('status')!r}"
    )


def require_cch_fee_within_limit(
    config: FiberSwapSmokeConfig,
    direction: str,
    fee_sats: int,
):
    if not 0 <= fee_sats <= config.max_cch_fee_sats:
        pytest.fail(
            f"{direction} CCH service fee {fee_sats} exceeds configured "
            f"limit {config.max_cch_fee_sats}; refusing to pay"
        )


def wait_terminal_status(
    description,
    load,
    *,
    success,
    failures,
    timeout,
    interval=2,
):
    deadline = time.monotonic() + timeout
    last = None
    last_error = None
    while time.monotonic() < deadline:
        try:
            last = load()
            last_error = None
        except Exception as exc:  # Read-only polling tolerates transient errors.
            last_error = f"{type(exc).__name__}: {exc}"
            time.sleep(interval)
            continue
        status = last.get("status")
        if status == success:
            return last
        if status in failures:
            raise AssertionError(
                f"{description} entered terminal failure {status}: "
                f"{json.dumps(last, sort_keys=True)}"
            )
        time.sleep(interval)
    raise AssertionError(
        f"timed out waiting for {description} to become {success}; "
        f"last value: {last}; last read error: {last_error}"
    )


def wait_order_success(config: FiberSwapSmokeConfig, payment_hash: str):
    return wait_terminal_status(
        f"FiberSwap order {payment_hash}",
        lambda: api_json(config, "GET", f"/api/order/{payment_hash}"),
        success="Success",
        failures={"Failed"},
        timeout=config.wait_timeout,
    )


def wait_fiber_payment_success(config: FiberSwapSmokeConfig, payment_hash: str):
    return wait_terminal_status(
        f"Fiber payment {payment_hash}",
        lambda: fiber_swap_fnn(
            config,
            ["payment", "get_payment", "--payment-hash", payment_hash],
        ),
        success="Success",
        failures={"Failed"},
        timeout=config.wait_timeout,
    )


def wait_fiber_invoice_paid(config: FiberSwapSmokeConfig, payment_hash: str):
    return wait_terminal_status(
        f"Fiber invoice {payment_hash}",
        lambda: fiber_swap_fnn(
            config,
            ["invoice", "get_invoice", "--payment-hash", payment_hash],
        ),
        success="Paid",
        failures={"Cancelled", "Expired"},
        timeout=config.wait_timeout,
    )


def find_lnd_payment(config: FiberSwapSmokeConfig, payment_hash: str):
    wanted = canonical_hash(payment_hash).removeprefix("0x")
    payments = fiber_swap_lncli_json(
        config,
        ["listpayments", "--include_incomplete", "--max_payments=100"],
    ).get("payments", [])
    return next(
        (
            payment
            for payment in payments
            if canonical_hash(payment.get("payment_hash", ""))
            == canonical_hash(wanted)
        ),
        None,
    )


def wait_lnd_payment_succeeded(
    config: FiberSwapSmokeConfig,
    payment_hash: str,
    lnd_label: str = "lnd-d",
):
    deadline = time.monotonic() + config.wait_timeout
    last = None
    last_error = None
    while time.monotonic() < deadline:
        try:
            last = find_lnd_payment(config, payment_hash)
            last_error = None
        except Exception as exc:  # listpayments is a read-only query.
            last_error = f"{type(exc).__name__}: {exc}"
            time.sleep(2)
            continue
        if last:
            if last.get("status") == "SUCCEEDED":
                return last
            if last.get("status") == "FAILED":
                raise AssertionError(
                    f"{lnd_label} payment failed: "
                    + json.dumps(last, sort_keys=True)
                )
        time.sleep(2)
    raise AssertionError(
        f"timed out waiting for {lnd_label} payment {payment_hash}; "
        f"last value: {last}; last read error: {last_error}"
    )


def wait_lnd_invoice_settled(
    config: FiberSwapSmokeConfig, payment_hash: str, expected_sats: int
):
    wanted = canonical_hash(payment_hash).removeprefix("0x")
    deadline = time.monotonic() + config.wait_timeout
    last = None
    last_error = None
    while time.monotonic() < deadline:
        try:
            last = fiber_swap_lncli_json(config, ["lookupinvoice", wanted])
            last_error = None
        except Exception as exc:  # lookupinvoice is a read-only query.
            last_error = f"{type(exc).__name__}: {exc}"
            time.sleep(2)
            continue
        state = last.get("state")
        if state == "SETTLED":
            paid = int(last.get("amt_paid_sat", "0"))
            assert paid == expected_sats, (
                f"lnd-d invoice paid amount mismatch: {paid} != {expected_sats}"
            )
            return last
        if state in {"CANCELED", "EXPIRED"}:
            raise AssertionError(
                f"lnd-d invoice entered terminal state {state}: "
                f"{json.dumps(last, sort_keys=True)}"
            )
        time.sleep(2)
    raise AssertionError(
        f"timed out waiting for lnd-d invoice {payment_hash}; "
        f"last value: {last}; last read error: {last_error}"
    )


def wait_flow_2_success(
    config: FiberSwapSmokeConfig,
    payment_hash: str,
    expected_sats: int,
    lnd_label: str = "lnd-d",
):
    """Poll all three FLOW 2 participants and fail on any terminal error."""

    wanted = canonical_hash(payment_hash).removeprefix("0x")
    deadline = time.monotonic() + config.wait_timeout
    fiber_payment = None
    order = None
    lnd_invoice = None
    read_errors: dict[str, str] = {}

    while time.monotonic() < deadline:
        reads = (
            (
                "Fiber payment",
                lambda: fiber_swap_fnn(
                    config,
                    [
                        "payment",
                        "get_payment",
                        "--payment-hash",
                        payment_hash,
                    ],
                ),
            ),
            (
                "FiberSwap order",
                lambda: api_json(
                    config, "GET", f"/api/order/{payment_hash}"
                ),
            ),
            (
                f"{lnd_label} invoice",
                lambda: fiber_swap_lncli_json(
                    config, ["lookupinvoice", wanted]
                ),
            ),
        )
        values = {
            "Fiber payment": fiber_payment,
            "FiberSwap order": order,
            f"{lnd_label} invoice": lnd_invoice,
        }
        for label, load in reads:
            try:
                values[label] = load()
                read_errors.pop(label, None)
            except Exception as exc:  # All three operations are read-only.
                read_errors[label] = f"{type(exc).__name__}: {exc}"

        fiber_payment = values["Fiber payment"]
        order = values["FiberSwap order"]
        lnd_invoice = values[f"{lnd_label} invoice"]
        summary = {
            "fiber": {
                "status": (fiber_payment or {}).get("status"),
                "failed_error": (fiber_payment or {}).get("failed_error"),
                "fee": (fiber_payment or {}).get("fee"),
            },
            "order": {"status": (order or {}).get("status")},
            lnd_label: {
                "state": (lnd_invoice or {}).get("state"),
                "amt_paid_sat": (lnd_invoice or {}).get("amt_paid_sat"),
            },
            "read_errors": read_errors,
        }

        fiber_status = (fiber_payment or {}).get("status")
        order_status = (order or {}).get("status")
        lnd_state = (lnd_invoice or {}).get("state")
        if (
            fiber_status == "Failed"
            or order_status == "Failed"
            or lnd_state in {"CANCELED", "EXPIRED"}
        ):
            raise AssertionError(
                "FLOW 2 entered a terminal failure: "
                + json.dumps(summary, sort_keys=True)
            )

        if (
            fiber_status == "Success"
            and order_status == "Success"
            and lnd_state == "SETTLED"
        ):
            paid = int(lnd_invoice.get("amt_paid_sat", "0"))
            assert paid == expected_sats, (
                f"{lnd_label} invoice paid amount mismatch: "
                f"{paid} != {expected_sats}"
            )
            return fiber_payment, order, lnd_invoice

        time.sleep(2)

    raise AssertionError(
        "timed out waiting for FLOW 2 to succeed: "
        + json.dumps(summary, sort_keys=True)
    )


def get_lnd_channel(config: FiberSwapSmokeConfig):
    channels = fiber_swap_lncli_json(config, ["listchannels"]).get("channels", [])
    matching_peer = [
        channel
        for channel in channels
        if channel.get("remote_pubkey") == config.fiber_swap_lnd_pubkey
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
        pytest.fail(
            "expected the configured lnd-d/FiberSwap CCH LND channel to exist "
            "and be active at "
            f"{config.lnd_channel_point}; available: "
            f"{json.dumps(available, sort_keys=True)}"
        )
    return matches[0]


def decimal_scid(value) -> str | None:
    """Normalize a non-zero uint64 SCID and reject BOLT02 channel IDs."""

    text = str(value or "")
    if not text.isdecimal() or len(text) > 20:
        return None
    number = int(text)
    if number == 0 or number >= 2**64:
        return None
    return str(number)


def lnd_channel_route_ids(channel: dict) -> set[str]:
    """Return every numeric SCID/alias LND may use for this channel."""

    candidates = [
        channel.get("scid"),
        channel.get("chan_id"),
        channel.get("zero_conf_confirmed_scid"),
        channel.get("peer_scid_alias"),
        *(channel.get("alias_scids") or []),
    ]
    return {
        normalized
        for value in candidates
        if (normalized := decimal_scid(value)) is not None
    }


def lnd_outgoing_chan_id(channel: dict) -> str:
    """Return the base SCID accepted by lncli --outgoing_chan_id."""

    # lncli v0.19+ renamed the numeric short channel ID to `scid` and now
    # renders the BOLT02 32-byte channel ID as `chan_id`. Older lncli versions
    # only expose the numeric value as `chan_id`.
    field = "scid" if "scid" in channel else "chan_id"
    scid = decimal_scid(channel.get(field))
    assert scid is not None, (
        f"configured lnd-d/FiberSwap CCH LND channel has no valid decimal "
        f"{field} for "
        f"--outgoing_chan_id: {json.dumps(channel, sort_keys=True)}"
    )
    return scid


def wait_lnd_channel_quiescent(config: FiberSwapSmokeConfig):
    deadline = time.monotonic() + config.wait_timeout
    last_pending = None
    last_error = None
    while time.monotonic() < deadline:
        try:
            channel = get_lnd_channel(config)
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
        "timed out waiting for lnd-d/FiberSwap CCH LND HTLCs to settle; "
        f"pending HTLCs: {json.dumps(last_pending, sort_keys=True)}; "
        f"last read error: {last_error}"
    )


def lnd_channel_balance_snapshot(
    channel: dict,
    local_label: str,
    remote_label: str,
):
    local = int(channel["local_balance"])
    remote = int(channel["remote_balance"])
    local_reserve = int(channel.get("local_chan_reserve_sat") or 0)
    remote_reserve = int(channel.get("remote_chan_reserve_sat") or 0)
    balances = {
        "chan_id": str(channel.get("chan_id", "")),
        "channel_point": channel["channel_point"],
        local_label: local,
        remote_label: remote,
        f"{local_label} spendable": max(0, local - local_reserve),
        f"{remote_label} spendable": max(0, remote - remote_reserve),
    }
    # Preserve field absence so legacy lncli output can fall back to the
    # decimal chan_id. An injected empty scid would incorrectly select the
    # modern field and make lnd_outgoing_chan_id() fail.
    if "scid" in channel:
        balances["scid"] = str(channel["scid"])
    return balances


def wait_lnd_balance_snapshot_delta(
    config,
    before,
    *,
    load_channel,
    local_label,
    remote_label,
    expected_local_delta,
    expected_remote_delta,
    channel_description,
    interval=0.5,
):
    """Wait for a pinned LND channel to expose its settled target balances."""

    channel_point = before["channel_point"]
    expected_local = before[local_label] + expected_local_delta
    expected_remote = before[remote_label] + expected_remote_delta
    deadline = time.monotonic() + config.wait_timeout
    last = None
    last_pending = None
    last_error = None
    while time.monotonic() < deadline:
        try:
            channel = load_channel(config)
            last = lnd_channel_balance_snapshot(
                channel,
                local_label,
                remote_label,
            )
            last_pending = channel.get("pending_htlcs") or []
            last_error = None
        except pytest.fail.Exception as exc:
            last_error = str(exc)
            time.sleep(interval)
            continue
        except Exception as exc:  # listchannels is a read-only query.
            last_error = f"{type(exc).__name__}: {exc}"
            time.sleep(interval)
            continue
        if (
            last["channel_point"] == channel_point
            and not last_pending
            and last[local_label] == expected_local
            and last[remote_label] == expected_remote
        ):
            return last
        time.sleep(interval)
    raise AssertionError(
        f"timed out waiting for {channel_description} balance update: "
        f"channel_point={channel_point}, {local_label}={expected_local}, "
        f"{remote_label}={expected_remote}, pending_htlcs={last_pending}, "
        f"last={last}, last_error={last_error}"
    )


def tlc_status_name(tlc: dict) -> str:
    status = tlc.get("status")
    if isinstance(status, dict) and len(status) == 1:
        return str(next(iter(status.values())))
    return str(status or "")


def active_fiber_tlcs(channel: dict) -> list[dict]:
    """Exclude terminal TLC records that FNN intentionally keeps for history."""

    return [
        tlc
        for tlc in channel.get("pending_tlcs") or []
        if tlc_status_name(tlc) != "RemoveAckConfirmed"
    ]


def lnd_balances(config: FiberSwapSmokeConfig):
    channel = wait_lnd_channel_quiescent(config)
    return lnd_channel_balance_snapshot(
        channel,
        "lnd-d",
        "FiberSwap CCH LND",
    )


def wait_lnd_balance_delta(
    config,
    before,
    expected_lnd_d_delta,
    expected_fiber_swap_delta,
    interval=0.5,
):
    return wait_lnd_balance_snapshot_delta(
        config,
        before,
        load_channel=get_lnd_channel,
        local_label="lnd-d",
        remote_label="FiberSwap CCH LND",
        expected_local_delta=expected_lnd_d_delta,
        expected_remote_delta=expected_fiber_swap_delta,
        channel_description="lnd-d/FiberSwap CCH LND channel",
        interval=interval,
    )


def get_fiber_channel(config: FiberSwapSmokeConfig):
    channels = fiber_swap_fnn(
        config,
        ["channel", "list_channels", "--pubkey", config.bottle_pubkey],
    ).get("channels", [])
    matches = [
        channel
        for channel in channels
        if channel.get("channel_id") == config.fiber_channel_id
        and state_name(channel) == "ChannelReady"
        and channel.get("enabled", True)
        and same_script(
            channel.get("funding_udt_type_script") or {}, config.udt_script
        )
    ]
    if len(matches) != 1:
        available = [
            {
                "channel_id": channel.get("channel_id"),
                "state": state_name(channel),
                "enabled": channel.get("enabled"),
                "local_balance": channel.get("local_balance"),
                "remote_balance": channel.get("remote_balance"),
            }
            for channel in channels
        ]
        pytest.fail(
            "expected exactly one ready fiber2/Bottle cWBTC channel "
            f"{config.fiber_channel_id}; available: "
            f"{json.dumps(available, sort_keys=True)}"
        )
    channel = matches[0]
    return channel


def wait_fiber_channel_quiescent(config: FiberSwapSmokeConfig):
    deadline = time.monotonic() + config.wait_timeout
    last_active = None
    last_error = None
    while time.monotonic() < deadline:
        try:
            channel = get_fiber_channel(config)
            last_error = None
        except Exception as exc:  # list_channels is a read-only query.
            last_error = f"{type(exc).__name__}: {exc}"
            time.sleep(2)
            continue
        last_active = active_fiber_tlcs(channel)
        if not last_active:
            return channel
        time.sleep(2)
    raise AssertionError(
        "timed out waiting for fiber2/Bottle channel TLCs to settle; "
        f"active TLCs: {json.dumps(last_active, sort_keys=True)}; "
        f"last read error: {last_error}"
    )


def fiber_balances(config: FiberSwapSmokeConfig):
    channel = wait_fiber_channel_quiescent(config)
    return {
        "channel_id": channel["channel_id"],
        "fiber2": hex_to_int(channel["local_balance"]),
        "Bottle": hex_to_int(channel["remote_balance"]),
    }


def create_fiber_invoice(config: FiberSwapSmokeConfig, amount: int):
    result = fiber_swap_fnn(
        config,
        [
            "invoice",
            "new_invoice",
            "--amount",
            str(amount),
            "--currency",
            config.currency,
            "--udt-type-script",
            json.dumps(config.udt_script, separators=(",", ":")),
            "--hash-algorithm",
            "sha256",
            "--expiry",
            "86400",
            "--description",
            f"fiber-swap-flow1-{int(time.time())}",
        ],
    )
    assert hex_to_int(result["invoice"]["amount"]) == amount
    return result["invoice_address"], canonical_hash(
        result["invoice"]["data"]["payment_hash"]
    )


def create_private_lnd_invoice(config: FiberSwapSmokeConfig, amount: int):
    target_channel = get_lnd_channel(config)
    target_route_ids = lnd_channel_route_ids(target_channel)
    assert target_route_ids, (
        "configured lnd-d/FiberSwap CCH LND channel exposes no usable SCID "
        "or alias: "
        f"{json.dumps(target_channel, sort_keys=True)}"
    )
    result = fiber_swap_lncli_json(
        config,
        [
            "addinvoice",
            f"--amt={amount}",
            f"--memo=fiber-swap-flow2-{int(time.time())}",
            "--expiry=86400",
            "--private",
        ],
    )
    decoded = fiber_swap_lncli_json(
        config, ["decodepayreq", result["payment_request"]]
    )
    payment_hash = canonical_hash(decoded["payment_hash"])
    assert decoded["destination"] == config.lnd_d_pubkey
    assert int(decoded["num_satoshis"]) == amount
    hints = [
        hop
        for route in decoded.get("route_hints", [])
        for hop in route.get("hop_hints", [])
        if hop.get("node_id") == config.fiber_swap_lnd_pubkey
        and str(hop.get("chan_id")) in target_route_ids
    ]
    assert hints, (
        "lnd-d private invoice has no route hint through the configured "
        "FiberSwap CCH LND channel "
        f"(route ids {sorted(target_route_ids)}); "
        f"decoded invoice: {json.dumps(decoded, sort_keys=True)}"
    )
    return result["payment_request"], payment_hash, hints


def parse_fiber_invoice(config: FiberSwapSmokeConfig, pay_req: str):
    result = fiber_swap_fnn(
        config, ["invoice", "parse_invoice", "--invoice", pay_req]
    )
    return {
        "amount": hex_to_int(result["invoice"]["amount"]),
        "payment_hash": canonical_hash(
            result["invoice"]["data"]["payment_hash"]
        ),
    }


def decode_lnd_invoice(config: FiberSwapSmokeConfig, pay_req: str):
    decoded = fiber_swap_lncli_json(config, ["decodepayreq", pay_req])
    return {
        "destination": decoded["destination"],
        "amount": int(decoded["num_satoshis"]),
        "payment_hash": canonical_hash(decoded["payment_hash"]),
        "route_hints": decoded.get("route_hints", []),
    }


def require_spendable(label: str, available: int, needed: int):
    if available < needed:
        pytest.fail(
            f"insufficient {label}: spendable={available:,}, needed={needed:,}"
        )


def print_balance_table(title: str, unit: str, before: dict, after: dict):
    print(f"\n{title} ({unit})")
    print(f"{'Side':<20}{'Before':>16}{'After':>16}{'Change':>16}")
    print("-" * 68)
    for side in before:
        if side.endswith("spendable") or side in {
            "scid",
            "chan_id",
            "channel_point",
            "channel_id",
        }:
            continue
        print(
            f"{side:<20}{before[side]:>16,}{after[side]:>16,}"
            f"{after[side] - before[side]:>+16,}"
        )


def display_lightning_network(lnd_network: str) -> str:
    return "testnet3" if lnd_network == "testnet" else lnd_network


def print_asset_convention(lnd_network: str):
    print("\nAsset convention: 1 sat BTC = 1 raw cWBTC for this demo")
    print(
        "Bitcoin/Lightning network: "
        f"{display_lightning_network(lnd_network)} "
        f"(LND/lncli network={lnd_network})"
    )


def print_fiber_swap_topology_key():
    print("\nTOPOLOGY KEY:")
    print(
        "  FiberSwap API            : control plane only; creates and queries "
        "CCH orders"
    )
    print(
        "  FiberSwap CCH LND        : Lightning-side node; receives or pays BTC"
    )
    print(
        "  FiberSwap FNN (CCH node) : Fiber-side node; receives or pays cWBTC"
    )
    print(
        "  Bottle (Fiber trampoline): public Fiber routing node between "
        "fiber2 and the FiberSwap FNN"
    )
    print(
        "  CCH swap boundary       : two coordinated payments; funds do not "
        "move directly between LND and FNN"
    )
    print("CONTROL PATH: CI → FiberSwap API → FiberSwap FNN (CCH node)")
    print("              order orchestration only; no funds pass through the API")


def print_flow_header(
    number: int,
    direction: str,
    money_path: tuple[tuple[str, str], ...],
    payment_hash: str,
):
    title = f" FLOW {number} · {direction} "
    fill = "─" if number == 1 else "═"
    print(f"\n{title.center(100, fill)}")
    print("MONEY PATH:")
    for leg, path in money_path:
        print(f"  {leg:<10}: {path}")
    print(f"Payment hash: {payment_hash}")


def print_flow_footer(number: int):
    label = f" FLOW {number} COMPLETE "
    fill = "─" if number == 1 else "═"
    print(f"\n{label.center(100, fill)}")


def print_flow_1_summary(
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
        "BTC → cWBTC",
        (
            ("BTC leg", "lnd-d --BTC--> FiberSwap CCH LND"),
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
    print(f"WHO PAID      : lnd-d paid {lnd_outflow:,} sats")
    print(f"WHO RECEIVED  : fiber2 received {principal:,} raw cWBTC")
    print("FEE OWNERSHIP:")
    print(
        f"  - CCH service fee     : {cch_fee:,} sats; "
        "paid by lnd-d, received by the FiberSwap CCH service"
    )
    print(
        f"  - Lightning route fee : {lightning_route_fee:,} sats; "
        "direct lnd-d ↔ FiberSwap CCH LND channel, no intermediary"
    )
    print(
        "  - Fiber route fee     : amount not exposed by the external API; "
        "paid by the FiberSwap CCH service through its FNN"
    )
    print_balance_table("LND channel", "sats", lnd_before, lnd_after)
    print_balance_table("Fiber channel", "raw cWBTC", fiber_before, fiber_after)
    print_flow_footer(1)


def print_flow_2_summary(
    *,
    payment_hash: str,
    btc_principal: int,
    cch_fee: int,
    fiber_route_fee: int,
    fiber_total: int,
    lnd_before: dict,
    lnd_after: dict,
    fiber_before: dict,
    fiber_after: dict,
):
    print_flow_header(
        2,
        "cWBTC → BTC",
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
            ("BTC leg", "FiberSwap CCH LND --BTC--> lnd-d"),
        ),
        payment_hash,
    )
    print(f"WHO PAID      : fiber2 paid {fiber_total:,} raw cWBTC")
    print(f"WHO RECEIVED  : lnd-d received {btc_principal:,} sats")
    print("FEE OWNERSHIP:")
    print(
        f"  - CCH service fee     : {cch_fee:,} raw cWBTC; "
        "paid by fiber2, received by the FiberSwap CCH service"
    )
    print(
        f"  - Fiber route fee     : {fiber_route_fee:,} raw cWBTC; "
        "paid by fiber2 across the trampoline route"
    )
    print(
        "                          Bottle is the first trampoline hop; "
        "the returned aggregate fee does not expose its final allocation"
    )
    print(
        "  - Lightning route fee : 0 sats; FiberSwap CCH LND pays lnd-d "
        "over their direct private channel"
    )
    print_balance_table("LND channel", "sats", lnd_before, lnd_after)
    print_balance_table("Fiber channel", "raw cWBTC", fiber_before, fiber_after)
    print_flow_footer(2)


def verify_lnd_direct_payment(
    config: FiberSwapSmokeConfig,
    payment: dict,
    expected_value: int,
):
    target_route_ids = lnd_channel_route_ids(get_lnd_channel(config))
    value = int(payment.get("value_sat", "0"))
    fee = int(payment.get("fee_sat", "0"))
    assert value == expected_value
    assert fee == 0, (
        "expected zero Lightning route fee on the direct "
        f"lnd-d/FiberSwap CCH LND channel; got {fee} sats"
    )
    succeeded = [
        htlc for htlc in payment.get("htlcs", []) if htlc.get("status") == "SUCCEEDED"
    ]
    assert succeeded, f"lnd-d payment has no successful HTLC: {payment}"
    for htlc in succeeded:
        hops = (htlc.get("route") or {}).get("hops") or []
        assert len(hops) == 1, f"expected a direct Lightning route: {hops}"
        assert hops[0].get("pub_key") == config.fiber_swap_lnd_pubkey
        assert str(hops[0].get("chan_id")) in target_route_ids, (
            "lnd-d payment used the wrong direct FiberSwap CCH LND channel: "
            f"hop={hops[0]}, configured route ids={sorted(target_route_ids)}"
        )
    return value + fee, fee


def verify_settled_invoice_channel(
    lnd_invoice: dict,
    target_route_ids: set[str],
    expected_sats: int,
    channel_description: str = "configured direct FiberSwap CCH LND channel",
    lnd_label: str = "lnd-d",
):
    settled_htlcs = [
        htlc
        for htlc in lnd_invoice.get("htlcs", [])
        if htlc.get("state") == "SETTLED"
    ]
    assert settled_htlcs, (
        f"{lnd_label} invoice has no settled HTLC: {lnd_invoice}"
    )
    wrong_channel_htlcs = [
        htlc
        for htlc in settled_htlcs
        if str(htlc.get("chan_id")) not in target_route_ids
    ]
    assert not wrong_channel_htlcs, (
        f"LND invoice used a channel other than the {channel_description}: "
        f"route_ids={sorted(target_route_ids)}, htlcs={wrong_channel_htlcs}"
    )
    settled_msat = sum(int(htlc.get("amt_msat", "0")) for htlc in settled_htlcs)
    assert settled_msat == expected_sats * 1000, (
        f"{lnd_label} invoice settled amount on the configured channel is wrong: "
        f"{settled_msat} msat != {expected_sats * 1000} msat"
    )


def run_flow_1_btc_to_cwbtc(config: FiberSwapSmokeConfig):
    """Pay BTC from lnd-d and receive cWBTC on fiber2."""

    principal = config.amount_sats
    fiber_before = fiber_balances(config)
    lnd_before = lnd_balances(config)
    outgoing_chan_id = lnd_outgoing_chan_id(lnd_before)
    require_spendable(
        "lnd-d outbound BTC (principal preflight)",
        lnd_before["lnd-d spendable"],
        principal,
    )
    require_spendable(
        "Bottle outbound cWBTC",
        fiber_before["Bottle"],
        principal,
    )

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
    require_cch_fee_within_limit(config, "BTC → cWBTC", cch_fee)
    lightning_total = hex_to_int(order["amount_sats"])
    assert lightning_total == principal + cch_fee
    assert decoded["amount"] == lightning_total

    require_spendable(
        "lnd-d outbound BTC",
        lnd_before["lnd-d spendable"],
        lightning_total,
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
    lnd_payment = wait_lnd_payment_succeeded(config, payment_hash)
    lnd_outflow, lightning_route_fee = verify_lnd_direct_payment(
        config, lnd_payment, lightning_total
    )
    wait_order_success(config, payment_hash)
    wait_fiber_invoice_paid(config, payment_hash)

    fiber_after = fiber_balances(config)
    lnd_after = wait_lnd_balance_delta(
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
        "FLOW 1 lnd-d",
        lnd_before["lnd-d"],
        lnd_after["lnd-d"],
        -lnd_outflow,
        f"lnd_channel_point={config.lnd_channel_point}",
    )
    assert_balance_delta(
        "FLOW 1 FiberSwap CCH LND",
        lnd_before["FiberSwap CCH LND"],
        lnd_after["FiberSwap CCH LND"],
        lnd_outflow,
        f"lnd_channel_point={config.lnd_channel_point}",
    )

    print_flow_1_summary(
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
        "fiber_before": fiber_before,
        "fiber_after": fiber_after,
        "lnd_before": lnd_before,
        "lnd_after": lnd_after,
    }


def run_flow_2_cwbtc_to_btc(config: FiberSwapSmokeConfig, flow_1: dict):
    """Pay cWBTC from fiber2 and restore lnd-d's FLOW 1 BTC outflow."""

    btc_principal = flow_1["lnd_outflow"]
    fiber_before = fiber_balances(config)
    lnd_before = lnd_balances(config)
    require_spendable(
        "fiber2 outbound cWBTC (principal and route-fee preflight)",
        fiber_before["fiber2"],
        btc_principal + config.max_fiber_fee,
    )
    require_spendable(
        "FiberSwap CCH LND outbound BTC",
        lnd_before["FiberSwap CCH LND spendable"],
        btc_principal,
    )

    btc_invoice, payment_hash, _route_hints = create_private_lnd_invoice(
        config, btc_principal
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
    require_cch_fee_within_limit(config, "cWBTC → BTC", cch_fee)
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
        config, payment_hash, btc_principal
    )
    fiber_route_fee = hex_to_int(fiber_payment.get("fee", 0))
    assert fiber_route_fee <= config.max_fiber_fee

    target_route_ids = lnd_channel_route_ids(get_lnd_channel(config))
    verify_settled_invoice_channel(lnd_invoice, target_route_ids, btc_principal)

    fiber_after = fiber_balances(config)
    lnd_after = wait_lnd_balance_delta(
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
        "FLOW 2 lnd-d",
        lnd_before["lnd-d"],
        lnd_after["lnd-d"],
        btc_principal,
        f"lnd_channel_point={config.lnd_channel_point}",
    )
    assert_balance_delta(
        "FLOW 2 FiberSwap CCH LND",
        lnd_before["FiberSwap CCH LND"],
        lnd_after["FiberSwap CCH LND"],
        -btc_principal,
        f"lnd_channel_point={config.lnd_channel_point}",
    )
    assert lnd_after["lnd-d"] == flow_1["lnd_before"]["lnd-d"]
    assert lnd_after["FiberSwap CCH LND"] == (
        flow_1["lnd_before"]["FiberSwap CCH LND"]
    )

    print_flow_2_summary(
        payment_hash=payment_hash,
        btc_principal=btc_principal,
        cch_fee=cch_fee,
        fiber_route_fee=fiber_route_fee,
        fiber_total=fiber_total,
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
        "fiber_total": fiber_total,
        "fiber_after": fiber_after,
        "lnd_after": lnd_after,
    }


def test_fiber_swap_bidirectional():
    config = FiberSwapSmokeConfig.from_env()

    info = fiber_swap_lncli_json(config, ["getinfo"])
    assert info["identity_pubkey"] == config.lnd_d_pubkey
    assert info.get("synced_to_chain") is True
    assert info.get("synced_to_graph") is True
    fiber_swap_fnn(config, ["info"])
    wait_lnd_channel_quiescent(config)
    wait_fiber_channel_quiescent(config)

    print_asset_convention(config.lnd_network)
    print_fiber_swap_topology_key()
    flow_1 = run_flow_1_btc_to_cwbtc(config)
    flow_2 = run_flow_2_cwbtc_to_btc(config, flow_1)

    expected_fiber_net = flow_1["principal"] - flow_2["fiber_total"]
    actual_fiber_net = (
        flow_2["fiber_after"]["fiber2"] - flow_1["fiber_before"]["fiber2"]
    )
    assert actual_fiber_net == expected_fiber_net

    print("\n" + "=" * 100)
    print("DAILY ROUND SUMMARY: both FiberSwap CCH directions passed")
    print(
        "LND liquidity          : net 0 sats "
        "(FLOW 2 restored exactly what FLOW 1 spent)"
    )
    print(
        f"fiber2 liquidity       : {actual_fiber_net:+,} raw cWBTC "
        "(CCH service fees plus aggregate Fiber routing fee are real costs)"
    )
    print("=" * 100)
