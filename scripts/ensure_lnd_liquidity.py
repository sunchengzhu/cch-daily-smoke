#!/usr/bin/env python3
"""Ensure lnd-b has a reusable outbound-liquidity buffer before smoke runs."""

import argparse
import math
import os
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = ROOT / "tests"
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from test_cch_daily_smoke import (  # noqa: E402
    CchSmokeConfig,
    lnd_b_liquidity,
    lncli_json,
    top_up_lnd_b_if_needed,
)


LOAD_HEADROOM_RATIO = 0.20
MIN_LOAD_HEADROOM_SATS = 100_000
PEER_RECONNECT_WAIT_SECONDS = 30


def ensure_local_lnd_peer(config) -> None:
    """Reconnect the existing lnd-b -> lnd-a channel after a peer disconnect."""

    pubkey = lncli_json(config, "lnd-a", ["getinfo"])["identity_pubkey"]

    def matching_channels():
        return [
            channel
            for channel in lncli_json(config, "lnd-b", ["listchannels"]).get(
                "channels", []
            )
            if channel.get("remote_pubkey") == pubkey
            and (
                config.lnd_channel_id is None
                or channel.get("chan_id") == config.lnd_channel_id
            )
        ]

    channels = matching_channels()
    if not channels or any(channel.get("active") for channel in channels):
        return

    peers = lncli_json(config, "lnd-b", ["listpeers"]).get("peers", [])
    if not any(peer.get("pub_key") == pubkey for peer in peers):
        address = os.environ.get(
            "CCH_SMOKE_LND_A_PEER_ADDR", f"{config.lnd_a_container}:9735"
        )
        print(
            f"[lnd-liquidity] channel inactive; reconnecting lnd-b to "
            f"lnd-a at {address}",
            flush=True,
        )
        lncli_json(config, "lnd-b", ["connect", "--perm", f"{pubkey}@{address}"])

    deadline = time.monotonic() + PEER_RECONNECT_WAIT_SECONDS
    while time.monotonic() < deadline:
        if any(channel.get("active") for channel in matching_channels()):
            print("[lnd-liquidity] local LND channel active", flush=True)
            return
        time.sleep(2)
    raise RuntimeError(
        "lnd-b -> lnd-a channel remained inactive after peer reconnect; "
        "inspect both nodes before attempting a payment"
    )


def minimum_spendable_for_load(
    *,
    base_minimum_sats: int,
    load_mode: str | None,
    tps: float | None,
    duration_seconds: float | None,
    amount_sats: int | None,
    max_inflight: int | None,
) -> tuple[int, dict[str, int]]:
    """Calculate one-way load volume plus a conservative liquidity buffer."""

    details = {
        "target_transactions": 0,
        "principal_sats": 0,
        "headroom_sats": 0,
    }
    if load_mode != "fixed-tps":
        return base_minimum_sats, details

    if not all(
        value is not None
        for value in (tps, duration_seconds, amount_sats, max_inflight)
    ):
        raise ValueError("fixed-tps liquidity calculation requires all load inputs")
    if tps <= 0 or duration_seconds <= 0 or amount_sats <= 0 or max_inflight <= 0:
        raise ValueError("fixed-tps liquidity inputs must be positive")

    target_transactions = math.ceil(tps * duration_seconds)
    principal_sats = target_transactions * amount_sats
    headroom_sats = max(
        math.ceil(principal_sats * LOAD_HEADROOM_RATIO),
        max_inflight * amount_sats,
        MIN_LOAD_HEADROOM_SATS,
    )
    details.update(
        target_transactions=target_transactions,
        principal_sats=principal_sats,
        headroom_sats=headroom_sats,
    )
    return max(base_minimum_sats, principal_sats + headroom_sats), details


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--load-mode", choices=["fixed-tps", "sequential"])
    parser.add_argument("--tps", type=float)
    parser.add_argument("--duration-seconds", type=float)
    parser.add_argument("--amount-sats", type=int)
    parser.add_argument("--max-inflight", type=int)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = CchSmokeConfig.from_env()
    base_minimum_spendable = int(
        os.environ.get("CCH_SMOKE_LND_MIN_SPENDABLE_SATS", "1000000")
    )
    if base_minimum_spendable <= 0:
        raise ValueError("CCH_SMOKE_LND_MIN_SPENDABLE_SATS must be positive")
    if config.lnd_topup_sats <= 0:
        raise ValueError("CCH_SMOKE_LND_TOPUP_SATS must be positive")

    minimum_spendable, load = minimum_spendable_for_load(
        base_minimum_sats=base_minimum_spendable,
        load_mode=args.load_mode,
        tps=args.tps,
        duration_seconds=args.duration_seconds,
        amount_sats=args.amount_sats,
        max_inflight=args.max_inflight,
    )
    if args.load_mode == "fixed-tps":
        print(
            "[lnd-liquidity] load requirement: "
            f"transactions={load['target_transactions']:,}, "
            f"principal={load['principal_sats']:,} sats, "
            f"headroom={load['headroom_sats']:,} sats, "
            f"minimum={minimum_spendable:,} sats",
            flush=True,
        )

    ensure_local_lnd_peer(config)
    before = lnd_b_liquidity(config)
    print(
        "[lnd-liquidity] preflight: "
        f"local={before['local_balance']:,} sats, "
        f"reserve={before['reserve_sats']:,} sats, "
        f"spendable={before['spendable_sats']:,} sats, "
        f"minimum={minimum_spendable:,} sats, "
        f"top-up-buffer={config.lnd_topup_sats:,} sats",
        flush=True,
    )

    result = top_up_lnd_b_if_needed(config, minimum_spendable)
    if result is None:
        print("[lnd-liquidity] ready; no top-up required", flush=True)
    else:
        print(
            "[lnd-liquidity] ready after top-up: "
            f"transferred={result['amount_sats']:,} sats, "
            f"spendable={result['previous_spendable_sats']:,} -> "
            f"{result['updated_spendable_sats']:,} sats",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
