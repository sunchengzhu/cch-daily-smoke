#!/usr/bin/env python3
"""Build the full CCH CI summary and a compact Discord notification.

The report consumes explicit environment variables and structured scenario JSON.
It intentionally does not scrape pytest or other human-readable command output.
"""

from __future__ import annotations

import argparse
import html
import json
import math
import os
import re
import urllib.error
import urllib.request
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


SUCCESS_COLOR = 0x2ECC71
FAILURE_COLOR = 0xE74C3C
EMBED_TOTAL_LIMIT = 6000
FIELD_VALUE_LIMIT = 1024

SCENARIOS = (
    ("local", "Local CCH"),
    ("direct", "FiberSwap · direct LND"),
    ("relay", "FiberSwap · via relay LND"),
)

OUTCOME_PRESENTATION = {
    "success": ("✅", "Passed"),
    "failure": ("❌", "Failed"),
    "cancelled": ("🛑", "Cancelled"),
    "skipped": ("⏭️", "Skipped"),
}


def _text(value: Any, default: str = "—") -> str:
    if value is None:
        return default
    rendered = str(value).strip()
    return rendered or default


def _one_line(value: Any, default: str = "—") -> str:
    return " ".join(_text(value, default).replace("`", "'").split())


def _length(value: str) -> int:
    return len(value.encode("utf-16-le", errors="replace")) // 2


def _truncate(value: str, limit: int) -> str:
    if _length(value) <= limit:
        return value
    if limit <= 1:
        return "…" if limit == 1 else ""
    result = []
    used = 0
    for char in value:
        size = _length(char)
        if used + size > limit - 1:
            break
        result.append(char)
        used += size
    return "".join(result).rstrip() + "…"


def _outcome(value: Any) -> tuple[str, str, str]:
    normalized = _text(value, "unknown").lower()
    emoji, label = OUTCOME_PRESENTATION.get(normalized, ("❔", "Unknown"))
    return normalized, emoji, label


def _duration(value: Any) -> str:
    try:
        seconds = float(value)
    except (TypeError, ValueError, OverflowError):
        return "duration unavailable"
    if not math.isfinite(seconds) or seconds < 0:
        return "duration unavailable"

    rounded = round(seconds)
    hours, remainder = divmod(rounded, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    if seconds < 10 and not seconds.is_integer():
        return f"{seconds:.1f}s"
    return f"{rounded}s"


def _compact_value(value: Any) -> str:
    if isinstance(value, Mapping):
        if not value:
            return "—"
        return " · ".join(
            f"{_one_line(key)}: {_one_line(item)}"
            for key, item in value.items()
        )
    if isinstance(value, list):
        if not value:
            return "—"
        return " · ".join(_one_line(item) for item in value)
    return _one_line(value)


def _schedule_summary(report: Mapping[str, Any]) -> str | None:
    day = str(report.get("scheduled_date", ""))
    epoch = str(report.get("started_at", ""))
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", day):
        return None
    if not re.fullmatch(r"[0-9]{1,12}", epoch):
        return None
    try:
        scheduled = datetime.fromisoformat(f"{day}T10:00:00+08:00")
        started = datetime.fromtimestamp(int(epoch), timezone(timedelta(hours=8)))
    except (ValueError, OverflowError, OSError):
        return None
    trigger = {
        "schedule": "GitHub fallback",
        "workflow_dispatch": "workflow_dispatch/API",
    }.get(_text(report.get("trigger"), ""), "unknown trigger")
    delay = _duration(max(0, (started - scheduled).total_seconds()))
    return (
        f"Scheduled {day} 10:00 CST · Actual start {started:%H:%M:%S} CST"
        f" · Delay {delay} · {trigger}"
    )


def _missing_summary(outcome_value: Any, parse_error: str | None) -> str:
    normalized, _emoji, _label = _outcome(outcome_value)
    if parse_error:
        return "Structured summary unavailable (invalid report JSON)."
    if normalized == "skipped":
        return "Not run because an earlier step did not complete."
    if normalized in {"failure", "cancelled"}:
        return "No structured summary was produced; open the run log for details."
    return "Structured summary unavailable."


def _parse_scenario(raw: Any) -> tuple[Mapping[str, Any] | None, str | None]:
    if raw is None or not str(raw).strip():
        return None, None
    try:
        parsed = json.loads(str(raw))
    except (TypeError, json.JSONDecodeError) as exc:
        return None, str(exc)
    if not isinstance(parsed, Mapping):
        return None, "scenario JSON must be an object"
    return parsed, None


def collect_report(environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Collect typed report data from the documented environment variables."""

    env = os.environ if environ is None else environ

    def stage_outcome(key: str) -> str:
        fallback = (
            "skipped" if env.get("CCH_REPORT_SMOKE_JOB_RESULT") == "skipped"
            else "unknown"
        )
        return env.get(key) or fallback

    scenarios: dict[str, dict[str, Any]] = {}
    for key, _label in SCENARIOS:
        prefix = f"CCH_REPORT_{key.upper()}"
        data, error = _parse_scenario(env.get(f"{prefix}_JSON"))
        scenarios[key] = {
            "outcome": stage_outcome(f"{prefix}_OUTCOME"),
            "data": data,
            "parse_error": error,
        }

    return {
        "job_result": env.get("CCH_REPORT_JOB_RESULT", "unknown"),
        "branch": env.get("CCH_REPORT_BRANCH", "unknown"),
        "fiber_source": env.get("CCH_REPORT_FIBER_SOURCE", "unknown"),
        "fnn_version": env.get("CCH_REPORT_FNN_VERSION", "unknown"),
        "fnn_package": env.get("CCH_REPORT_FNN_PACKAGE", "unknown"),
        "total_seconds": env.get("CCH_REPORT_TOTAL_SECONDS"),
        "scheduled_date": env.get("CCH_REPORT_SCHEDULED_DATE"),
        "started_at": env.get("CCH_REPORT_STARTED_AT"),
        "trigger": env.get("CCH_REPORT_TRIGGER"),
        "preflight": {
            **({"Schedule gate": env["CCH_REPORT_GATE_OUTCOME"]}
               if "CCH_REPORT_GATE_OUTCOME" in env else {}),
            "Checkout": stage_outcome("CCH_REPORT_CHECKOUT_OUTCOME"),
            "Python": stage_outcome("CCH_REPORT_PYTHON_OUTCOME"),
            "Dependencies": stage_outcome("CCH_REPORT_DEPENDENCIES_OUTCOME"),
            "Unit tests": stage_outcome("CCH_REPORT_UNIT_OUTCOME"),
            "FNN auth": stage_outcome("CCH_REPORT_AUTH_OUTCOME"),
            "FNN nodes": stage_outcome("CCH_REPORT_FNN_OUTCOME"),
            "LND liquidity": stage_outcome("CCH_REPORT_LIQUIDITY_OUTCOME"),
            "Cleanup": stage_outcome("CCH_REPORT_CLEANUP_OUTCOME"),
        },
        "scenarios": scenarios,
        "run_url": env.get("CCH_REPORT_RUN_URL", ""),
        "run_number": env.get("CCH_REPORT_RUN_NUMBER", "unknown"),
        "run_attempt": env.get("CCH_REPORT_RUN_ATTEMPT", "unknown"),
        "sha": env.get("CCH_REPORT_SHA", "unknown"),
    }


def _started_at(report: Mapping[str, Any]) -> datetime | None:
    epoch = str(report.get("started_at", ""))
    if not re.fullmatch(r"[0-9]{1,12}", epoch):
        return None
    try:
        return datetime.fromtimestamp(int(epoch), timezone(timedelta(hours=8)))
    except (ValueError, OverflowError, OSError):
        return None


def _run_overview(report: Mapping[str, Any]) -> str:
    scheduled = bool(report.get("scheduled_date")) or report.get("trigger") == "schedule"
    trigger = "Scheduled run" if scheduled else "Manual run"
    started = _started_at(report)
    timing = f"Started {started:%Y-%m-%d %H:%M:%S} CST" if started else "Start time unavailable"
    lines = [f"{trigger} · {timing}"]
    lines.append(
        f"FNN `{_truncate(_one_line(report.get('fnn_version'), 'unknown'), 100)}`"
        f" · {_truncate(_one_line(report.get('fiber_source'), 'unknown'), 80)}"
    )
    attempt = _one_line(report.get("run_attempt"), "unknown")
    if attempt not in {"1", "unknown"}:
        lines.append(f"Retry {attempt}")
    return "\n".join(lines)


def _scenario_lines(report: Mapping[str, Any]) -> list[str]:
    lines = []
    scenarios = report.get("scenarios", {})
    for key, label in SCENARIOS:
        scenario = scenarios.get(key, {})
        _normalized, emoji, status = _outcome(scenario.get("outcome"))
        line = f"{emoji} {label} · {status}"
        if scenario.get("data"):
            line += f" · {_duration(scenario['data'].get('duration_seconds'))}"
        elif scenario.get("parse_error") or _normalized == "success":
            line += " · summary unavailable"
        lines.append(line)
    return lines


def _passed_count(report: Mapping[str, Any]) -> int:
    scenarios = report.get("scenarios", {})
    return sum(
        _outcome(scenarios.get(key, {}).get("outcome"))[0] == "success"
        for key, _label in SCENARIOS
    )


def _md(value: Any) -> str:
    # Keep arbitrary package names and scenario values inside their Markdown cell.
    value = html.escape(_one_line(value), quote=False)
    for char in "\\|*_[]":
        value = value.replace(char, "\\" + char)
    return value


def build_github_summary(report: Mapping[str, Any]) -> str:
    """Render the full CI report independently of Discord and its size limits."""
    _result, emoji, label = _outcome(report.get("job_result"))
    lines = [
        "# CCH Daily Smoke · Testnet",
        "",
        f"{emoji} **{label}** · {_passed_count(report)}/{len(SCENARIOS)} smoke scenarios passed."
        f" · ⏱ {_duration(report.get('total_seconds'))}",
        "",
        "## Run overview",
        "",
        "| Item | Value |",
        "| --- | --- |",
    ]
    started = _started_at(report)
    metadata = {
        "Run": f"#{_text(report.get('run_number'))} · attempt {_text(report.get('run_attempt'))}",
        "Branch": report.get("branch"),
        "Commit": report.get("sha"),
        "Fiber source": report.get("fiber_source"),
        "FNN version": report.get("fnn_version"),
        "Package": report.get("fnn_package"),
        "Trigger": report.get("trigger"),
        "Started": f"{started:%Y-%m-%d %H:%M:%S} CST" if started else "unavailable",
    }
    lines.extend(f"| {key} | {_md(value)} |" for key, value in metadata.items())
    schedule = _schedule_summary(report)
    if schedule:
        lines.extend(["", _md(schedule)])
    run_url = _text(report.get("run_url"), "")
    if run_url:
        lines.extend(["", f"[Full run and logs]({run_url})"])
    lines.extend([
        "", "## Checks", "", "| Stage | Result |", "| --- | --- |",
    ])
    for stage, value in report.get("preflight", {}).items():
        _normalized, icon, status = _outcome(value)
        lines.append(f"| {_md(stage)} | {icon} {status} |")
    lines.extend([
        "", "## Scenario results", "",
        "Per-payment hashes, verified assertions and before/after balances appear in the smoke job's summary. "
        "If a scenario stops early, completed flows remain there; open the failed step's log for the error.",
    ])
    for key, scenario_label in SCENARIOS:
        scenario = report.get("scenarios", {}).get(key, {})
        _normalized, icon, status = _outcome(scenario.get("outcome"))
        lines.extend(["", f"### {icon} {scenario_label}", "", f"**{status}**"])
        data = scenario.get("data")
        if not data:
            lines.extend(["", _missing_summary(
                scenario.get("outcome"), scenario.get("parse_error")
            )])
            continue
        lines.extend([
            "",
            f"**Duration:** {_duration(data.get('duration_seconds'))}",
            "",
            f"**Topology:** {_md(data.get('topology'))}",
            "",
            "| Direction | Paid | Received |",
            "| --- | --- | --- |",
        ])
        flows = data.get("flows", [])
        if isinstance(flows, list):
            for flow in flows:
                if isinstance(flow, Mapping):
                    lines.append(
                        f"| {_md(flow.get('direction'))} | {_md(flow.get('paid'))} | {_md(flow.get('received'))} |"
                    )
        for field, heading in (("fees", "Fees"), ("net", "Net balance changes")):
            lines.extend(["", f"**{heading}**", ""])
            values = data.get(field)
            if isinstance(values, Mapping):
                lines.extend(f"- **{_md(name)}:** {_md(value)}" for name, value in values.items())
            else:
                lines.append(_md(_compact_value(values)))
    return "\n".join(lines) + "\n"


def build_discord_payload(report: Mapping[str, Any]) -> dict[str, Any]:
    """Render a short notification linking to the detailed CI report."""
    result, emoji, _label = _outcome(report.get("job_result"))
    passed = result == "success"
    preflight = report.get("preflight", {})
    problems = [
        f"{_outcome(value)[1]} {stage}"
        for stage, value in preflight.items()
        if _outcome(value)[0] != "success"
    ]
    checks = " · ".join(problems) if problems else (
        "✅ All checks passed" if preflight else "No check results."
    )
    fields = [
        {"name": "Run overview", "value": _truncate(_run_overview(report), FIELD_VALUE_LIMIT), "inline": False},
        {"name": "Checks", "value": _truncate(checks, FIELD_VALUE_LIMIT), "inline": False},
        {"name": "Scenarios", "value": _truncate("\n".join(_scenario_lines(report)), FIELD_VALUE_LIMIT), "inline": False},
    ]
    description = f"{_passed_count(report)}/{len(SCENARIOS)} smoke scenarios passed"
    description += "." if passed else "; workflow did not pass."
    description += f" · ⏱ {_duration(report.get('total_seconds'))}"
    embed = {
        "title": _truncate(f"{emoji} CCH Daily Smoke · Testnet · #{_one_line(report.get('run_number'), 'unknown')}", 256),
        "description": description,
        "color": SUCCESS_COLOR if passed else FAILURE_COLOR,
        "fields": fields,
        "footer": {"text": "Click the title for the full report and logs"},
    }
    run_url = _text(report.get("run_url"), "")
    if run_url:
        embed["url"] = run_url
    # Three bounded fields + title/description/footer stay below 6,000 UTF-16 units.
    return {"allowed_mentions": {"parse": []}, "embeds": [embed]}


def payload_from_env(environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    return build_discord_payload(collect_report(environ))


def send_discord_webhook(
    webhook_url: str,
    payload: Mapping[str, Any],
    timeout: int = 20,
) -> None:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    request = urllib.request.Request(
        webhook_url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "cch-daily-smoke/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response.read()
    except urllib.error.HTTPError as exc:
        response_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Discord webhook returned HTTP {exc.code}: {response_body}"
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Discord webhook request failed: {exc.reason}") from exc


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--summary-only", action="store_true", help="Write the full GitHub Actions summary without sending Discord")
    modes.add_argument("--dry-run", action="store_true", help="Print the Discord payload without sending it")
    args = parser.parse_args(argv)
    report = collect_report()
    if args.summary_only:
        summary_path = os.environ.get("GITHUB_STEP_SUMMARY", "").strip()
        if not summary_path:
            raise SystemExit("GITHUB_STEP_SUMMARY is required")
        with Path(summary_path).open("a", encoding="utf-8") as summary:
            summary.write(build_github_summary(report))
        print("GitHub Actions daily-smoke summary written")
        return
    if args.dry_run:
        print(json.dumps(build_discord_payload(report), ensure_ascii=False, indent=2))
        return
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook_url:
        raise SystemExit("DISCORD_WEBHOOK_URL is required")
    send_discord_webhook(webhook_url, build_discord_payload(report))
    print("Discord daily-smoke report sent")


if __name__ == "__main__":
    main()
