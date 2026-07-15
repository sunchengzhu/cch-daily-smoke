#!/usr/bin/env python3
"""Run one CCH business flow at a fixed arrival rate or sequentially."""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import subprocess
import sys
import threading
import time
import traceback
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = ROOT / "tests"
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from test_cch_daily_smoke import (  # noqa: E402
    CchSmokeConfig,
    add_lnd_invoice,
    create_fiber_invoice,
    fnn,
    get_fiber_channel,
    hex_to_int,
    lnd_b_liquidity,
    lnd_channel_balances_from_a,
    lncli_json,
    lncli_raw,
    pay_lnd_invoice,
    same_script,
    wait_cch_order_status,
    wait_fiber_invoice_status,
    wait_fiber_payment_status,
    wait_lnd_invoice_settled,
)


LOG = logging.getLogger("cch-stability")
FLOW_FIBER_TO_LND = "fiber-to-lnd"
FLOW_LND_TO_FIBER = "lnd-to-fiber"
MODE_FIXED_TPS = "fixed-tps"
MODE_SEQUENTIAL = "sequential"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def compact_error(exc: BaseException, max_length: int = 240) -> str:
    """Return the useful part of a command failure for the console log."""
    if isinstance(exc, subprocess.TimeoutExpired):
        command = exc.cmd
        label = "command"
        if isinstance(command, (list, tuple)):
            tokens = [str(token) for token in command]
            if len(tokens) >= 4 and tokens[:2] == ["docker", "exec"]:
                container = tokens[2]
                tool = Path(tokens[3]).name
                action = next(
                    (token for token in tokens[4:] if not token.startswith("-")),
                    None,
                )
                label = " ".join(part for part in (container, action or tool) if part)
            elif tokens:
                label = Path(tokens[0]).name
        timeout = f"{float(exc.timeout):g}"
        return f"{label} timed out after {timeout}s"

    message = str(exc).strip()
    if "stderr:\n" in message:
        stderr = message.rsplit("stderr:\n", 1)[1].strip()
        if stderr:
            message = stderr
    message = " ".join(message.split())
    if len(message) > max_length:
        return message[: max_length - 3] + "..."
    return message


def seconds(value: float | None) -> str:
    return "n/a" if value is None else f"{value / 1000:.2f}s"


def percentile(values: list[float], percent: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * percent / 100
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


class JsonlWriter:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = path.open("w", encoding="utf-8")
        self._lock = threading.Lock()

    def write(self, record: dict[str, Any]) -> None:
        with self._lock:
            self._file.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            self._file.flush()

    def close(self) -> None:
        with self._lock:
            self._file.close()


@dataclass
class RunState:
    scheduled: int = 0
    started: int = 0
    running: int = 0
    succeeded: int = 0
    failed: int = 0
    rejected: int = 0
    latencies_ms: list[float] = field(default_factory=list)
    start_delays_ms: list[float] = field(default_factory=list)
    errors: dict[str, int] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "scheduled": self.scheduled,
                "started": self.started,
                "running": self.running,
                "succeeded": self.succeeded,
                "failed": self.failed,
                "rejected": self.rejected,
                "latency_p95_ms": percentile(self.latencies_ms, 95),
            }


def run_flow_fiber_to_lnd(
    config: CchSmokeConfig, amount_sats: int, transaction_name: str
) -> dict[str, Any]:
    invoice = add_lnd_invoice(config, "lnd-b", amount_sats, transaction_name)
    order = fnn(
        config,
        config.f1_rpc,
        [
            "cch",
            "send_btc",
            "--btc-pay-req",
            invoice["payment_request"],
            "--currency",
            config.currency,
        ],
    )
    if not same_script(order["wrapped_btc_type_script"], config.udt_script):
        raise AssertionError("send_btc returned an unexpected wrapped BTC type script")

    payment_hash = order["payment_hash"]
    fee_sats = hex_to_int(order["fee_sats"])
    fiber_amount = hex_to_int(order["amount_sats"])
    if fiber_amount != amount_sats + fee_sats:
        raise AssertionError(
            f"unexpected Fiber amount: {fiber_amount}; expected {amount_sats + fee_sats}"
        )

    payment = fnn(
        config,
        config.f2_rpc,
        ["payment", "send_payment", "--invoice", order["incoming_invoice"]["Fiber"]],
    )
    if payment["payment_hash"] != payment_hash:
        raise AssertionError("send_payment returned a different payment hash")

    wait_fiber_payment_status(config, payment_hash, "Success")
    wait_cch_order_status(config, payment_hash, "Success")
    wait_lnd_invoice_settled(config, "lnd-b", payment_hash, amount_sats)
    return {
        "payment_hash": payment_hash,
        "fee_sats": fee_sats,
        "source_amount": fiber_amount,
        "destination_amount": amount_sats,
    }


def run_flow_lnd_to_fiber(
    config: CchSmokeConfig, amount_sats: int, transaction_name: str
) -> dict[str, Any]:
    del transaction_name
    fiber_invoice, payment_hash = create_fiber_invoice(config, amount_sats)
    order = fnn(
        config,
        config.f1_rpc,
        ["cch", "receive_btc", "--fiber-pay-req", fiber_invoice],
    )
    if order["payment_hash"] != payment_hash:
        raise AssertionError("receive_btc returned a different payment hash")
    if not same_script(order["wrapped_btc_type_script"], config.udt_script):
        raise AssertionError("receive_btc returned an unexpected wrapped BTC type script")

    fee_sats = hex_to_int(order["fee_sats"])
    fiber_amount = hex_to_int(order["amount_sats"])
    if fiber_amount != amount_sats:
        raise AssertionError(
            f"unexpected Fiber amount: {fiber_amount}; expected {amount_sats}"
        )
    lightning_amount = fiber_amount + fee_sats

    try:
        pay_lnd_invoice(
            config,
            "lnd-b",
            order["incoming_invoice"]["Lightning"],
            timeout=config.command_timeout,
        )
        wait_cch_order_status(config, payment_hash, "Success")
        wait_fiber_invoice_status(config, payment_hash, "Paid")
        wait_lnd_invoice_settled(config, "lnd-a", payment_hash, lightning_amount)
    except BaseException:
        cleanup_lnd_to_fiber_invoice(config, payment_hash)
        raise
    return {
        "payment_hash": payment_hash,
        "fee_sats": fee_sats,
        "source_amount": lightning_amount,
        "destination_amount": fiber_amount,
    }


def cleanup_lnd_to_fiber_invoice(config: CchSmokeConfig, payment_hash: str) -> None:
    """Cancel this failed flow's hold invoice so it cannot block later runs."""
    payment_hash_hex = payment_hash.removeprefix("0x")
    try:
        invoice = lncli_json(config, "lnd-a", ["lookupinvoice", payment_hash_hex])
        state = invoice.get("state")
        if state not in {"OPEN", "ACCEPTED"}:
            return
        lncli_raw(config, "lnd-a", ["cancelinvoice", payment_hash_hex])
        LOG.debug(
            "CLEANUP canceled failed lnd-to-fiber hold invoice hash=%s previous_state=%s",
            payment_hash,
            state,
        )
    except BaseException as cleanup_error:
        LOG.warning(
            "CLEANUP failed hash=%s %s: %s",
            payment_hash,
            type(cleanup_error).__name__,
            compact_error(cleanup_error),
        )


def log_preflight(
    config: CchSmokeConfig, flow: str, amount_sats: int, target_count: int
) -> None:
    channel = get_fiber_channel(config)
    fiber2_balance = hex_to_int(channel["local_balance"])
    fiber1_balance = hex_to_int(channel["remote_balance"])
    lnd = lnd_channel_balances_from_a(config)
    required_principal = amount_sats * target_count

    if flow == FLOW_FIBER_TO_LND and fiber2_balance < amount_sats:
        raise RuntimeError("fiber2 does not have enough balance for even one transaction")
    if flow == FLOW_LND_TO_FIBER:
        lnd_b = lnd_b_liquidity(config)
        spendable = lnd_b["spendable_sats"]
        pending_htlcs = lnd_b.get("pending_htlcs_count", 0)
        if pending_htlcs:
            raise RuntimeError(
                "lnd-to-fiber preflight found "
                f"{pending_htlcs} pending HTLC(s) on the selected LND channel; "
                "a previous run may have left unresolved hold invoices. "
                "Cancel or settle them before starting a new stability run"
            )
        if spendable < amount_sats:
            raise RuntimeError("lnd-b does not have enough spendable balance for one transaction")

    if flow == FLOW_FIBER_TO_LND:
        capacity = min(fiber2_balance, lnd["lnd_a"])
        capacity_sources = "fiber2 and lnd-a"
    else:
        capacity = min(fiber1_balance, spendable)
        capacity_sources = "fiber1/CCH and spendable lnd-b"
    if capacity < required_principal:
        raise RuntimeError(
            f"insufficient estimated source liquidity on {capacity_sources}: "
            f"available={capacity}, principal_required={required_principal}; "
            "fees and reserves require additional headroom"
        )

    LOG.info(
        "PREFLIGHT tx=%d amount=%d required=%d available=%d source=%s",
        target_count,
        amount_sats,
        required_principal,
        capacity,
        capacity_sources,
    )


def execute_transaction(
    sequence: int,
    scheduled_at: float,
    run_id: str,
    flow: str,
    config: CchSmokeConfig,
    amount_sats: int,
    state: RunState,
    writer: JsonlWriter,
    limiter: threading.Semaphore,
    flow_fn: Callable[[CchSmokeConfig, int, str], dict[str, Any]],
) -> None:
    started_at = time.monotonic()
    start_delay_ms = max(0.0, (started_at - scheduled_at) * 1000)
    with state.lock:
        state.started += 1
        state.running += 1
        state.start_delays_ms.append(start_delay_ms)

    record: dict[str, Any] = {
        "type": "transaction",
        "run_id": run_id,
        "sequence": sequence,
        "flow": flow,
        "started_at": utc_now(),
        "start_delay_ms": round(start_delay_ms, 3),
        "amount_sats": amount_sats,
    }
    try:
        result = flow_fn(config, amount_sats, f"cch-stability-{run_id}-{sequence}")
        latency_ms = (time.monotonic() - started_at) * 1000
        record.update(result)
        record.update(status="success", latency_ms=round(latency_ms, 3))
        with state.lock:
            state.succeeded += 1
            state.latencies_ms.append(latency_ms)
        LOG.debug(
            "TX success seq=%d hash=%s latency_ms=%.1f",
            sequence,
            result.get("payment_hash"),
            latency_ms,
        )
    except BaseException as exc:  # pytest.fail derives from BaseException
        latency_ms = (time.monotonic() - started_at) * 1000
        error_type = type(exc).__name__
        record.update(
            status="failed",
            latency_ms=round(latency_ms, 3),
            error_type=error_type,
            error=compact_error(exc),
            traceback="".join(traceback.format_exception(exc)),
        )
        with state.lock:
            state.failed += 1
            state.errors[error_type] = state.errors.get(error_type, 0) + 1
        LOG.error(
            "FAILED seq=%d latency=%.2fs %s: %s",
            sequence,
            latency_ms / 1000,
            error_type,
            compact_error(exc),
        )
    finally:
        record["finished_at"] = utc_now()
        writer.write(record)
        with state.lock:
            state.running -= 1
        limiter.release()


def build_summary(
    args: argparse.Namespace,
    run_id: str,
    state: RunState,
    load_started_at: float,
    load_finished_at: float,
    finished_at: float,
) -> dict[str, Any]:
    mode = getattr(args, "mode", MODE_FIXED_TPS)
    with state.lock:
        total_unsuccessful = state.failed + state.rejected
        failure_rate = total_unsuccessful / state.scheduled if state.scheduled else 1.0
        load_seconds = max(load_finished_at - load_started_at, 0.000001)
        wall_seconds = max(finished_at - load_started_at, 0.000001)
        return {
            "type": "summary",
            "run_id": run_id,
            "flow": args.flow,
            "load_mode": mode,
            "target_tps": args.tps if mode == MODE_FIXED_TPS else None,
            "target_transactions": (
                math.ceil(args.duration * args.tps)
                if mode == MODE_FIXED_TPS
                else None
            ),
            "configured_duration_seconds": args.duration,
            "load_duration_seconds": round(load_seconds, 3),
            "wall_duration_seconds": round(wall_seconds, 3),
            "max_inflight": args.max_inflight if mode == MODE_FIXED_TPS else 1,
            "scheduled": state.scheduled,
            "started": state.started,
            "succeeded": state.succeeded,
            "failed": state.failed,
            "rejected": state.rejected,
            "failure_rate": round(failure_rate, 6),
            "actual_start_tps": round(state.started / load_seconds, 4),
            "successful_completion_tps_wall": round(state.succeeded / wall_seconds, 4),
            "latency_ms": {
                "min": round(min(state.latencies_ms), 3) if state.latencies_ms else None,
                "p50": round(percentile(state.latencies_ms, 50), 3)
                if state.latencies_ms
                else None,
                "p95": round(percentile(state.latencies_ms, 95), 3)
                if state.latencies_ms
                else None,
                "p99": round(percentile(state.latencies_ms, 99), 3)
                if state.latencies_ms
                else None,
                "max": round(max(state.latencies_ms), 3) if state.latencies_ms else None,
            },
            "start_delay_ms": {
                "p95": round(percentile(state.start_delays_ms, 95), 3)
                if state.start_delays_ms
                else None,
                "max": round(max(state.start_delays_ms), 3)
                if state.start_delays_ms
                else None,
            },
            "errors": dict(sorted(state.errors.items())),
            "passed": failure_rate <= args.max_failure_rate,
            "max_failure_rate": args.max_failure_rate,
            "finished_at": utc_now(),
        }


def finish_load(
    args: argparse.Namespace,
    run_id: str,
    state: RunState,
    writer: JsonlWriter,
    load_started_at: float,
    load_finished_at: float,
    finished_at: float,
) -> dict[str, Any]:
    summary = build_summary(
        args, run_id, state, load_started_at, load_finished_at, finished_at
    )
    writer.write(summary)
    LOG.info(
        "RESULT %s mode=%s succeeded=%d/%d failed=%d rejected=%d "
        "failure_rate=%.3f%% success_tps=%.2f success_p50=%s "
        "success_p95=%s wall=%.1fs errors=%s",
        "PASS" if summary["passed"] else "FAIL",
        summary["load_mode"],
        summary["succeeded"],
        summary["scheduled"],
        summary["failed"],
        summary["rejected"],
        summary["failure_rate"] * 100,
        summary["successful_completion_tps_wall"],
        seconds(summary["latency_ms"]["p50"]),
        seconds(summary["latency_ms"]["p95"]),
        summary["wall_duration_seconds"],
        summary["errors"] or "none",
    )
    return summary


def run_sequential_load(
    args: argparse.Namespace,
    config: CchSmokeConfig,
    writer: JsonlWriter,
    flow_fn: Callable[[CchSmokeConfig, int, str], dict[str, Any]],
) -> dict[str, Any]:
    state = RunState()
    limiter = threading.BoundedSemaphore(1)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_preflight(config, args.flow, args.amount_sats, 1)

    LOG.info(
        "START run_id=%s flow=%s mode=%s duration_seconds=%.3f "
        "concurrency=1 scheduling=next-flow-after-completion",
        run_id,
        args.flow,
        MODE_SEQUENTIAL,
        args.duration,
    )

    load_started_at = time.monotonic()
    deadline = load_started_at + args.duration
    next_progress = load_started_at + args.progress_interval

    def log_progress(now: float) -> None:
        nonlocal next_progress
        snapshot = state.snapshot()
        completed = snapshot["succeeded"] + snapshot["failed"]
        elapsed = now - load_started_at
        LOG.info(
            "PROGRESS %ds mode=%s sent=%d done=%d active=%d failed=%d "
            "success_tps=%.2f success_p95=%s",
            round(elapsed),
            MODE_SEQUENTIAL,
            snapshot["scheduled"],
            completed,
            snapshot["running"],
            snapshot["failed"],
            snapshot["succeeded"] / elapsed,
            seconds(snapshot["latency_p95_ms"]),
        )
        next_progress = now + args.progress_interval

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="cch-flow") as executor:
        sequence = 0
        while time.monotonic() < deadline:
            sequence += 1
            scheduled_at = time.monotonic()
            with state.lock:
                state.scheduled += 1
            limiter.acquire()
            future = executor.submit(
                execute_transaction,
                sequence,
                scheduled_at,
                run_id,
                args.flow,
                config,
                args.amount_sats,
                state,
                writer,
                limiter,
                flow_fn,
            )
            while not future.done():
                now = time.monotonic()
                wait({future}, timeout=max(0.01, next_progress - now))
                now = time.monotonic()
                if now >= next_progress:
                    log_progress(now)
            now = time.monotonic()
            if now >= next_progress:
                log_progress(now)

    return finish_load(
        args,
        run_id,
        state,
        writer,
        load_started_at,
        deadline,
        time.monotonic(),
    )


def run_load(
    args: argparse.Namespace,
    config: CchSmokeConfig,
    writer: JsonlWriter,
    flow_fn: Callable[[CchSmokeConfig, int, str], dict[str, Any]],
) -> dict[str, Any]:
    if getattr(args, "mode", MODE_FIXED_TPS) == MODE_SEQUENTIAL:
        return run_sequential_load(args, config, writer, flow_fn)

    state = RunState()
    limiter = threading.BoundedSemaphore(args.max_inflight)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    interval = 1.0 / args.tps
    target_count = math.ceil(args.duration * args.tps)
    log_preflight(config, args.flow, args.amount_sats, target_count)

    LOG.info(
        "START run_id=%s flow=%s mode=%s target_tps=%.3f duration_seconds=%.3f "
        "target_transactions=%d max_inflight=%d",
        run_id,
        args.flow,
        MODE_FIXED_TPS,
        args.tps,
        args.duration,
        target_count,
        args.max_inflight,
    )

    futures: set[Future[None]] = set()
    load_started_at = time.monotonic()
    deadline = load_started_at + args.duration
    next_progress = load_started_at + args.progress_interval
    saturated_since: float | None = None

    with ThreadPoolExecutor(
        max_workers=args.max_inflight, thread_name_prefix="cch-flow"
    ) as executor:
        sequence = 0
        while True:
            scheduled_at = load_started_at + sequence * interval
            if scheduled_at >= deadline:
                break
            delay = scheduled_at - time.monotonic()
            if delay > 0:
                time.sleep(delay)

            sequence += 1
            with state.lock:
                state.scheduled += 1

            if not limiter.acquire(blocking=False):
                rejected_at = time.monotonic()
                with state.lock:
                    state.rejected += 1
                    state.errors["MaxInflightExceeded"] = (
                        state.errors.get("MaxInflightExceeded", 0) + 1
                    )
                writer.write(
                    {
                        "type": "transaction",
                        "run_id": run_id,
                        "sequence": sequence,
                        "flow": args.flow,
                        "status": "rejected",
                        "error_type": "MaxInflightExceeded",
                        "error": f"max_inflight={args.max_inflight}",
                        "finished_at": utc_now(),
                    }
                )
                if saturated_since is None:
                    saturated_since = rejected_at
                    LOG.warning(
                        "SATURATED active=%d limit=%d first_rejected_seq=%d; "
                        "further rejections are counted in PROGRESS",
                        args.max_inflight,
                        args.max_inflight,
                        sequence,
                    )
            else:
                futures.add(
                    executor.submit(
                        execute_transaction,
                        sequence,
                        scheduled_at,
                        run_id,
                        args.flow,
                        config,
                        args.amount_sats,
                        state,
                        writer,
                        limiter,
                        flow_fn,
                    )
                )

            now = time.monotonic()
            if now >= next_progress:
                futures = {future for future in futures if not future.done()}
                snapshot = state.snapshot()
                elapsed = now - load_started_at
                completed = snapshot["succeeded"] + snapshot["failed"]
                if (
                    saturated_since is not None
                    and snapshot["running"] <= int(args.max_inflight * 0.8)
                ):
                    LOG.info(
                        "RECOVERED active=%d limit=%d saturated_for=%.1fs",
                        snapshot["running"],
                        args.max_inflight,
                        now - saturated_since,
                    )
                    saturated_since = None
                saturation = (
                    f"{now - saturated_since:.0f}s"
                    if saturated_since is not None
                    else "no"
                )
                LOG.info(
                    "PROGRESS %ds sent=%d/%d done=%d active=%d failed=%d "
                    "rejected=%d saturated=%s success_tps=%.2f success_p95=%s",
                    round(elapsed),
                    snapshot["scheduled"],
                    target_count,
                    completed,
                    snapshot["running"],
                    snapshot["failed"],
                    snapshot["rejected"],
                    saturation,
                    snapshot["succeeded"] / elapsed,
                    seconds(snapshot["latency_p95_ms"]),
                )
                next_progress = now + args.progress_interval

        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)
        load_finished_at = time.monotonic()
        LOG.info("DRAIN waiting_for=%d", len(futures))
        wait(futures)

    return finish_load(
        args,
        run_id,
        state,
        writer,
        load_started_at,
        load_finished_at,
        time.monotonic(),
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run complete CCH flows at a fixed rate or one at a time."
    )
    parser.add_argument(
        "--flow",
        required=True,
        choices=[FLOW_FIBER_TO_LND, FLOW_LND_TO_FIBER],
    )
    parser.add_argument(
        "--mode",
        choices=[MODE_FIXED_TPS, MODE_SEQUENTIAL],
        default=MODE_FIXED_TPS,
        help="fixed arrival rate, or one flow at a time",
    )
    parser.add_argument("--tps", type=float, default=5.0)
    parser.add_argument("--duration", type=float, default=300.0, help="seconds")
    parser.add_argument("--amount-sats", type=int, default=None)
    parser.add_argument("--max-inflight", type=int, default=100)
    parser.add_argument("--progress-interval", type=float, default=10.0)
    parser.add_argument("--max-failure-rate", type=float, default=0.0)
    parser.add_argument("--output-dir", type=Path, default=Path("reports/stability"))
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args(argv)
    if args.duration <= 0:
        parser.error("--duration must be greater than zero")
    if args.mode == MODE_FIXED_TPS and args.tps <= 0:
        parser.error("--tps must be greater than zero in fixed-tps mode")
    if args.progress_interval <= 0:
        parser.error("--progress-interval must be greater than zero")
    if args.mode == MODE_FIXED_TPS and args.max_inflight <= 0:
        parser.error("--max-inflight must be greater than zero in fixed-tps mode")
    if not 0 <= args.max_failure_rate <= 1:
        parser.error("--max-failure-rate must be between 0 and 1")
    if args.amount_sats is not None and args.amount_sats <= 0:
        parser.error("--amount-sats must be greater than zero")
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

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    details_path = args.output_dir / f"{args.flow}-{timestamp}.jsonl"
    summary_path = args.output_dir / f"{args.flow}-{timestamp}-summary.json"
    writer = JsonlWriter(details_path)

    LOG.info("OUTPUT details=%s summary=%s", details_path, summary_path)
    try:
        config = CchSmokeConfig.from_env()
        args.amount_sats = args.amount_sats or config.amount_sats
        flow_fn = (
            run_flow_fiber_to_lnd
            if args.flow == FLOW_FIBER_TO_LND
            else run_flow_lnd_to_fiber
        )
        summary = run_load(args, config, writer, flow_fn)
    except BaseException as exc:
        summary = {
            "type": "summary",
            "flow": args.flow,
            "load_mode": args.mode,
            "target_tps": args.tps if args.mode == MODE_FIXED_TPS else None,
            "configured_duration_seconds": args.duration,
            "passed": False,
            "error_type": type(exc).__name__,
            "error": str(exc),
            "finished_at": utc_now(),
        }
        writer.write(summary)
        LOG.exception("RUN failed before a normal summary could be generated")
    finally:
        writer.close()
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
