#!/usr/bin/env python3
"""Convert streaming stability records into reports that are easy to inspect."""

import argparse
import csv
import json
import math
from collections import Counter
from pathlib import Path
from typing import Any


CSV_COLUMNS = [
    "sequence",
    "status",
    "latency_ms",
    "start_delay_ms",
    "amount_sats",
    "fee_sats",
    "source_amount",
    "destination_amount",
    "error_type",
    "error",
    "payment_hash",
    "started_at",
    "finished_at",
]


def percentile(values: list[float], percent: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percent / 100
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def read_jsonl(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    transactions = []
    summary = None
    with path.open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSON on {path}:{line_number}: {exc}") from exc
            if record.get("type") == "summary":
                summary = record
            elif record.get("type") == "transaction":
                transactions.append(record)
    return transactions, summary


def partial_summary(
    transactions: list[dict[str, Any]],
    flow: str,
    target_tps: float,
    duration: float,
    mode: str = "fixed-tps",
) -> dict[str, Any]:
    statuses = Counter(record.get("status", "unknown") for record in transactions)
    errors = Counter(
        record.get("error_type") or "UnknownError"
        for record in transactions
        if record.get("status") not in {"success", None}
    )
    latencies = [
        float(record["latency_ms"])
        for record in transactions
        if record.get("latency_ms") is not None
    ]
    recorded = len(transactions)
    target_transactions = (
        math.ceil(target_tps * duration) if mode == "fixed-tps" else None
    )
    unsuccessful = statuses["failed"] + statuses["rejected"]
    return {
        "type": "summary",
        "flow": flow,
        "load_mode": mode,
        "target_tps": target_tps if mode == "fixed-tps" else None,
        "configured_duration_seconds": duration,
        "target_transactions": target_transactions,
        "recorded_transactions": recorded,
        "scheduled": recorded,
        "started": recorded - statuses["rejected"],
        "succeeded": statuses["success"],
        "failed": statuses["failed"],
        "rejected": statuses["rejected"],
        "failure_rate": round(unsuccessful / recorded, 6) if recorded else None,
        "latency_ms": {
            "min": round(min(latencies), 3) if latencies else None,
            "p50": round(percentile(latencies, 50), 3) if latencies else None,
            "p95": round(percentile(latencies, 95), 3) if latencies else None,
            "p99": round(percentile(latencies, 99), 3) if latencies else None,
            "max": round(max(latencies), 3) if latencies else None,
        },
        "errors": dict(sorted(errors.items())),
        "passed": False,
        "partial": True,
        "completion_status": "interrupted",
    }


def normalized_summary(
    existing: dict[str, Any] | None,
    transactions: list[dict[str, Any]],
    flow: str,
    target_tps: float,
    duration: float,
    mode: str = "fixed-tps",
) -> dict[str, Any]:
    if existing is None:
        return partial_summary(transactions, flow, target_tps, duration, mode)

    summary = dict(existing)
    summary.setdefault("load_mode", mode)
    summary.setdefault(
        "target_transactions",
        math.ceil(target_tps * duration) if mode == "fixed-tps" else None,
    )
    summary.setdefault("recorded_transactions", len(transactions))
    completed_load = "scheduled" in summary
    summary["partial"] = not completed_load
    summary["completion_status"] = (
        "completed" if completed_load else "failed_before_completion"
    )
    return summary


def write_csv(path: Path, records: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as destination:
        writer = csv.DictWriter(destination, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)


def format_value(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.3f}".rstrip("0").rstrip(".")
    return str(value)


def summary_markdown(summary: dict[str, Any]) -> str:
    status = summary.get("completion_status", "unknown")
    if status == "completed":
        result = "PASS" if summary.get("passed") else "FAIL"
    else:
        result = status.upper().replace("_", " ")
    latency = summary.get("latency_ms") or {}
    failure_rate = summary.get("failure_rate")
    failure_rate_display = (
        f"{failure_rate * 100:.3f}%" if failure_rate is not None else None
    )
    rows = [
        ("Result", result),
        ("Flow", summary.get("flow")),
        ("Load mode", summary.get("load_mode")),
        ("Target TPS", summary.get("target_tps")),
        ("Target transactions", summary.get("target_transactions")),
        ("Recorded transactions", summary.get("recorded_transactions", summary.get("scheduled"))),
        ("Succeeded", summary.get("succeeded")),
        ("Failed", summary.get("failed")),
        ("Rejected", summary.get("rejected")),
        ("Failure rate", failure_rate_display),
        ("Actual start TPS", summary.get("actual_start_tps")),
        (
            "Successful completion TPS",
            summary.get("successful_completion_tps_wall"),
        ),
        ("Successful flow latency p50 (ms)", latency.get("p50")),
        ("Successful flow latency p95 (ms)", latency.get("p95")),
        ("Successful flow latency p99 (ms)", latency.get("p99")),
        ("Successful flow latency max (ms)", latency.get("max")),
    ]
    lines = ["## CCH stability report", "", "| Metric | Value |", "| --- | ---: |"]
    lines.extend(f"| {label} | {format_value(value)} |" for label, value in rows)
    errors = summary.get("errors") or {}
    if errors:
        lines.extend(["", "### Errors", ""])
        lines.extend(f"- `{name}`: {count}" for name, count in errors.items())
    if summary.get("partial"):
        lines.extend(
            [
                "",
                "> This is a partial report generated from records written before the run stopped.",
            ]
        )
    return "\n".join(lines) + "\n"


def build_artifacts(
    jsonl_path: Path,
    flow: str,
    target_tps: float,
    duration: float,
    mode: str = "fixed-tps",
) -> dict[str, Path]:
    transactions, existing_summary = read_jsonl(jsonl_path)
    summary = normalized_summary(
        existing_summary, transactions, flow, target_tps, duration, mode
    )
    base = jsonl_path.with_suffix("")
    paths = {
        "summary_json": Path(f"{base}-summary.json"),
        "summary_md": Path(f"{base}-summary.md"),
        "transactions_csv": Path(f"{base}-transactions.csv"),
        "failures_csv": Path(f"{base}-failures.csv"),
    }
    paths["summary_json"].write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    markdown = summary_markdown(summary)
    paths["summary_md"].write_text(markdown, encoding="utf-8")
    write_csv(paths["transactions_csv"], transactions)
    write_csv(
        paths["failures_csv"],
        [record for record in transactions if record.get("status") != "success"],
    )
    return paths


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reports-dir", type=Path, required=True)
    parser.add_argument("--flow", required=True)
    parser.add_argument("--target-tps", type=float, required=True)
    parser.add_argument("--duration", type=float, required=True)
    parser.add_argument(
        "--mode", choices=["fixed-tps", "sequential"], default="fixed-tps"
    )
    parser.add_argument("--github-summary", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    jsonl_files = sorted(args.reports_dir.glob("*.jsonl"))
    if not jsonl_files:
        print(f"No stability JSONL records found in {args.reports_dir}")
        return 0

    for jsonl_path in jsonl_files:
        paths = build_artifacts(
            jsonl_path, args.flow, args.target_tps, args.duration, args.mode
        )
        if args.github_summary:
            with args.github_summary.open("a", encoding="utf-8") as destination:
                destination.write(paths["summary_md"].read_text(encoding="utf-8"))
        print(f"Built readable reports for {jsonl_path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
