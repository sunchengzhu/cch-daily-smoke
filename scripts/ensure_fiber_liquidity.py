#!/usr/bin/env python3
"""Ensure fiber1/CCH has enough cWBTC outbound liquidity for a load run."""

import argparse
import json
import math
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = ROOT / "tests"
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from test_cch_daily_smoke import (  # noqa: E402
    CchSmokeConfig,
    fnn,
    get_fiber_channel,
    hex_to_int,
    wait_fiber_payment_status,
)


FIBER_LOAD_HEADROOM_RATIO = 0.10


def minimum_fiber1_for_load(
    *,
    load_mode: str,
    tps: float,
    duration_seconds: float,
    amount_sats: int,
) -> tuple[int, dict[str, int]]:
    """Calculate cWBTC principal plus a small directional-liquidity buffer."""

    if tps <= 0 or duration_seconds <= 0 or amount_sats <= 0:
        raise ValueError("Fiber liquidity inputs must be positive")
    if load_mode == "fixed-tps":
        target_transactions = math.ceil(tps * duration_seconds)
    else:
        # Sequential throughput is unknown in advance. The workflow's TPS input
        # remains a conservative upper-bound estimate for liquidity preparation.
        target_transactions = math.ceil(tps * duration_seconds)

    principal_sats = target_transactions * amount_sats
    headroom_sats = max(
        math.ceil(principal_sats * FIBER_LOAD_HEADROOM_RATIO),
        amount_sats,
    )
    return principal_sats + headroom_sats, {
        "target_transactions": target_transactions,
        "principal_sats": principal_sats,
        "headroom_sats": headroom_sats,
    }


def top_up_fiber1_if_needed(config: CchSmokeConfig, needed_sats: int):
    channel = get_fiber_channel(config)
    fiber2_balance = hex_to_int(channel["local_balance"])
    fiber1_balance = hex_to_int(channel["remote_balance"])
    if fiber1_balance >= needed_sats:
        print(
            "[fiber-liquidity] ready; no top-up required: "
            f"fiber1/CCH={fiber1_balance:,}, minimum={needed_sats:,}",
            flush=True,
        )
        return None

    transfer_sats = needed_sats - fiber1_balance
    if fiber2_balance < transfer_sats:
        raise RuntimeError(
            "insufficient total Fiber channel liquidity to prepare load: "
            f"fiber1/CCH={fiber1_balance}, fiber2={fiber2_balance}, "
            f"transfer_required={transfer_sats}, minimum={needed_sats}"
        )

    print(
        "[fiber-liquidity] insufficient fiber1/CCH outbound liquidity: "
        f"fiber1/CCH={fiber1_balance:,}, fiber2={fiber2_balance:,}, "
        f"minimum={needed_sats:,}; transferring {transfer_sats:,} from fiber2",
        flush=True,
    )
    invoice = fnn(
        config,
        config.f1_rpc,
        [
            "invoice",
            "new_invoice",
            "--amount",
            str(transfer_sats),
            "--currency",
            config.currency,
            "--udt-type-script",
            json.dumps(config.udt_script, separators=(",", ":")),
            "--hash-algorithm",
            "sha256",
        ],
    )
    payment_hash = invoice["invoice"]["data"]["payment_hash"]
    payment = fnn(
        config,
        config.f2_rpc,
        ["payment", "send_payment", "--invoice", invoice["invoice_address"]],
    )
    if payment.get("payment_hash") != payment_hash:
        raise AssertionError("Fiber liquidity top-up returned a different payment hash")
    wait_fiber_payment_status(config, payment_hash, "Success")

    updated = get_fiber_channel(config, channel_id=channel["channel_id"])
    updated_fiber1 = hex_to_int(updated["remote_balance"])
    if updated_fiber1 < needed_sats:
        raise AssertionError(
            "fiber1/CCH outbound liquidity is still insufficient after top-up: "
            f"balance={updated_fiber1}, minimum={needed_sats}"
        )
    print(
        "[fiber-liquidity] ready after top-up: "
        f"transferred={transfer_sats:,}, "
        f"fiber1/CCH={fiber1_balance:,} -> {updated_fiber1:,}",
        flush=True,
    )
    return {
        "amount_sats": transfer_sats,
        "previous_fiber1_balance": fiber1_balance,
        "updated_fiber1_balance": updated_fiber1,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--load-mode", choices=["fixed-tps", "sequential"], required=True
    )
    parser.add_argument("--tps", type=float, required=True)
    parser.add_argument("--duration-seconds", type=float, required=True)
    parser.add_argument("--amount-sats", type=int, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    minimum, load = minimum_fiber1_for_load(
        load_mode=args.load_mode,
        tps=args.tps,
        duration_seconds=args.duration_seconds,
        amount_sats=args.amount_sats,
    )
    print(
        "[fiber-liquidity] load requirement: "
        f"transactions={load['target_transactions']:,}, "
        f"principal={load['principal_sats']:,}, "
        f"headroom={load['headroom_sats']:,}, minimum={minimum:,}",
        flush=True,
    )
    top_up_fiber1_if_needed(CchSmokeConfig.from_env(), minimum)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
