#!/usr/bin/env python3
"""Run one real CCH flow before starting a stability test."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.run_stability import (  # noqa: E402
    CchSmokeConfig,
    FLOW_FIBER_TO_LND,
    FLOW_LND_TO_FIBER,
    compact_error,
    log_preflight,
    run_flow_fiber_to_lnd,
    run_flow_lnd_to_fiber,
)


LOG = logging.getLogger("cch-readiness")
Flow = Callable[[CchSmokeConfig, int, str], dict]


class ReadinessProbeTimeout(TimeoutError):
    """The complete readiness flow exceeded its wall-clock deadline."""


@contextmanager
def wall_clock_timeout(seconds: float) -> Iterator[None]:
    """Interrupt a probe that exceeds its complete-flow deadline."""

    previous_handler = signal.getsignal(signal.SIGALRM)

    def raise_timeout(_signum, _frame) -> None:
        raise ReadinessProbeTimeout(
            f"end-to-end readiness probe timed out after {seconds:g}s"
        )

    signal.signal(signal.SIGALRM, raise_timeout)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)


def run_readiness_probe(
    config: CchSmokeConfig,
    flow: str,
    amount_sats: int,
    timeout_seconds: float,
    flow_fn: Flow,
) -> dict:
    """Require one complete real flow to succeed before measured traffic starts."""

    probe_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    LOG.info(
        "PROBE START flow=%s amount=%d timeout=%.0fs",
        flow,
        amount_sats,
        timeout_seconds,
    )
    started_at = time.monotonic()
    try:
        with wall_clock_timeout(timeout_seconds):
            log_preflight(config, flow, amount_sats, 1)
            result = flow_fn(
                config,
                amount_sats,
                f"cch-readiness-{probe_id}",
            )
    except BaseException as exc:
        LOG.error(
            "PROBE FAIL flow=%s latency=%.2fs %s: %s",
            flow,
            time.monotonic() - started_at,
            type(exc).__name__,
            compact_error(exc),
        )
        raise

    LOG.info(
        "PROBE PASS flow=%s latency=%.2fs hash=%s",
        flow,
        time.monotonic() - started_at,
        result.get("payment_hash", "n/a"),
    )
    return result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Require one complete CCH flow to succeed before a stability test."
    )
    parser.add_argument(
        "--flow",
        required=True,
        choices=[FLOW_FIBER_TO_LND, FLOW_LND_TO_FIBER],
    )
    parser.add_argument("--amount-sats", type=int, default=None)
    parser.add_argument("--timeout", type=float, default=60.0, help="seconds")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args(argv)
    if args.amount_sats is not None and args.amount_sats <= 0:
        parser.error("--amount-sats must be greater than zero")
    if args.timeout <= 0:
        parser.error("--timeout must be greater than zero")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    os.environ.setdefault("CCH_SMOKE_ENABLED", "1")
    if args.amount_sats is not None:
        os.environ["CCH_SMOKE_AMOUNT_SATS"] = str(args.amount_sats)

    try:
        config = CchSmokeConfig.from_env()
    except BaseException as exc:
        LOG.error(
            "PROBE CONFIG FAIL %s: %s",
            type(exc).__name__,
            compact_error(exc),
        )
        return 1

    amount_sats = args.amount_sats or config.amount_sats
    flow_fn = (
        run_flow_fiber_to_lnd
        if args.flow == FLOW_FIBER_TO_LND
        else run_flow_lnd_to_fiber
    )
    try:
        run_readiness_probe(
            config,
            args.flow,
            amount_sats,
            args.timeout,
            flow_fn,
        )
    except BaseException:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
