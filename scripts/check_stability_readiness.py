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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.run_stability import (  # noqa: E402
    CchSmokeConfig,
    FLOW_FIBER_TO_LND,
    FLOW_LND_TO_FIBER,
    compact_error,
    fnn,
    log_preflight,
    lncli_json,
    run_flow_fiber_to_lnd,
    run_flow_lnd_to_fiber,
)


LOG = logging.getLogger("cch-readiness")
Flow = Callable[..., dict]


class ReadinessProbeTimeout(TimeoutError):
    """The complete readiness flow exceeded its wall-clock deadline."""


@dataclass
class ProbeStageTracker:
    """Log probe stages and preserve the stage that originally failed."""

    current_stage: str | None = None
    failed_stage: str | None = None
    payment_hash: str | None = None
    stage_started_at: float = field(default_factory=time.monotonic)

    def __call__(
        self, stage: str, status: str, details: dict[str, Any] | None = None
    ) -> None:
        details = details or {}
        now = time.monotonic()
        if details.get("payment_hash"):
            self.payment_hash = str(details["payment_hash"])
        if status == "START":
            self.current_stage = stage
            self.stage_started_at = now
            LOG.info("PROBE STAGE stage=%s status=START", stage)
            return

        elapsed = now - self.stage_started_at
        rendered = " ".join(
            f"{key}={value}"
            for key, value in sorted(details.items())
            if value is not None and key != "payment_hash"
        )
        suffix = f" {rendered}" if rendered else ""
        LOG.info(
            "PROBE STAGE stage=%s status=%s latency=%.2fs%s",
            stage,
            status,
            elapsed,
            suffix,
        )
        if status == "FAIL" and self.failed_stage is None:
            self.failed_stage = stage
        if status == "PASS" and self.current_stage == stage:
            self.current_stage = None


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


def diagnostic_value(
    action: Callable[[], Any],
    transform: Callable[[Any], Any] = lambda value: value,
) -> Any:
    """Collect one best-effort diagnostic without hiding the original failure."""

    try:
        return transform(action())
    except BaseException as exc:
        return f"unavailable({type(exc).__name__}: {compact_error(exc)})"


def collect_probe_diagnostics(
    config: CchSmokeConfig, flow: str, payment_hash: str | None
) -> dict[str, Any]:
    """Log endpoint states after cleanup, without exposing invoices or tokens."""

    diagnostics: dict[str, Any] = {"payment_hash": payment_hash or "unavailable"}
    command_timeout = min(float(getattr(config, "command_timeout", 10.0)), 10.0)
    if payment_hash:
        diagnostics["cch_order"] = diagnostic_value(
            lambda: fnn(
                config,
                config.f1_rpc,
                ["cch", "get_cch_order", "--payment-hash", payment_hash],
                timeout=command_timeout,
            ),
            lambda value: value.get("status", "unknown"),
        )
        fiber_command = (
            ["invoice", "get_invoice", "--payment-hash", payment_hash]
            if flow == FLOW_LND_TO_FIBER
            else ["payment", "get_payment", "--payment-hash", payment_hash]
        )
        diagnostics["fiber"] = diagnostic_value(
            lambda: fnn(
                config,
                config.f2_rpc,
                fiber_command,
                timeout=command_timeout,
            ),
            lambda value: value.get("status", "unknown"),
        )
        invoice_node = "lnd-a" if flow == FLOW_LND_TO_FIBER else "lnd-b"
        diagnostics[f"{invoice_node}_invoice"] = diagnostic_value(
            lambda: lncli_json(
                config,
                invoice_node,
                ["lookupinvoice", payment_hash.removeprefix("0x")],
                timeout=command_timeout,
            ),
            lambda value: value.get("state", "unknown"),
        )

    for node_name in ("lnd-a", "lnd-b"):
        diagnostics[f"{node_name}_pending_htlcs"] = diagnostic_value(
            lambda node_name=node_name: lncli_json(
                config,
                node_name,
                ["listchannels"],
                timeout=command_timeout,
            ),
            lambda value: sum(
                len(channel.get("pending_htlcs") or [])
                for channel in value.get("channels", [])
            ),
        )

    LOG.info(
        "PROBE DIAG %s",
        " ".join(f"{key}={value}" for key, value in diagnostics.items()),
    )
    return diagnostics


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
    stages = ProbeStageTracker()
    try:
        with wall_clock_timeout(timeout_seconds):
            stages("static_preflight", "START")
            try:
                log_preflight(config, flow, amount_sats, 1)
            except BaseException as exc:
                stages(
                    "static_preflight",
                    "FAIL",
                    {"error_type": type(exc).__name__},
                )
                raise
            stages("static_preflight", "PASS")
            result = flow_fn(
                config,
                amount_sats,
                f"cch-readiness-{probe_id}",
                stage_callback=stages,
            )
    except BaseException as exc:
        failed_stage = stages.failed_stage or stages.current_stage or "unknown"
        LOG.error(
            "PROBE FAIL flow=%s failed_stage=%s latency=%.2fs %s: %s",
            flow,
            failed_stage,
            time.monotonic() - started_at,
            type(exc).__name__,
            compact_error(exc),
        )
        try:
            collect_probe_diagnostics(config, flow, stages.payment_hash)
        except BaseException as diagnostic_error:
            LOG.warning(
                "PROBE DIAG unavailable %s: %s",
                type(diagnostic_error).__name__,
                compact_error(diagnostic_error),
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
    parser.add_argument("--timeout", type=float, default=90.0, help="seconds")
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
