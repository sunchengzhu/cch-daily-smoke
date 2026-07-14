#!/usr/bin/env python3
"""Ensure lnd-b has a reusable outbound-liquidity buffer before smoke runs."""

import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = ROOT / "tests"
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from test_cch_daily_smoke import (  # noqa: E402
    CchSmokeConfig,
    lnd_b_liquidity,
    top_up_lnd_b_if_needed,
)


def main() -> int:
    config = CchSmokeConfig.from_env()
    minimum_spendable = int(
        os.environ.get("CCH_SMOKE_LND_MIN_SPENDABLE_SATS", "1000000")
    )
    if minimum_spendable <= 0:
        raise ValueError("CCH_SMOKE_LND_MIN_SPENDABLE_SATS must be positive")
    if config.lnd_topup_sats <= 0:
        raise ValueError("CCH_SMOKE_LND_TOPUP_SATS must be positive")

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
