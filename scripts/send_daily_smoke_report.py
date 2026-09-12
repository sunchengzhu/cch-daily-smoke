#!/usr/bin/env python3
"""Build and send the scheduled CCH daily-smoke report to Discord.

The report consumes explicit environment variables and structured scenario JSON.
It intentionally does not scrape pytest or other human-readable command output.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
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


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    if limit <= 1:
        return value[:limit]
    return value[: limit - 1].rstrip() + "…"


def _outcome(value: Any) -> tuple[str, str, str]:
    normalized = _text(value, "unknown").lower()
    emoji, label = OUTCOME_PRESENTATION.get(normalized, ("❔", "Unknown"))
    return normalized, emoji, label


def _duration(value: Any) -> str:
    try:
        seconds = max(0.0, float(value))
    except (TypeError, ValueError):
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


def _scenario_summary(
    outcome_value: Any,
    scenario: Mapping[str, Any] | None,
    parse_error: str | None,
) -> str:
    normalized, _emoji, label = _outcome(outcome_value)
    lines = [f"**{label}**"]

    if scenario:
        lines[0] += f" · ⏱ {_duration(scenario.get('duration_seconds'))}"
        topology = scenario.get("topology")
        if topology:
            lines.append(f"**Topology:** {_one_line(topology)}")

        flows = scenario.get("flows")
        if isinstance(flows, list):
            for flow in flows:
                if not isinstance(flow, Mapping):
                    continue
                direction = _one_line(flow.get("direction"), "Flow")
                paid = _one_line(flow.get("paid"), "payer unavailable")
                received = _one_line(
                    flow.get("received"), "recipient unavailable"
                )
                lines.append(f"• **{direction}** · {paid} → {received}")

        if scenario.get("fees") is not None:
            lines.append(f"**Fees:** {_compact_value(scenario['fees'])}")
        if scenario.get("net") is not None:
            lines.append(f"**Net:** {_compact_value(scenario['net'])}")
    elif parse_error:
        lines.append("Structured summary unavailable (invalid report JSON).")
    elif normalized == "skipped":
        lines.append("Not run because an earlier step did not complete.")
    elif normalized in {"failure", "cancelled"}:
        lines.append(
            "No structured summary was produced; open the run log for details."
        )
    else:
        lines.append("Structured summary unavailable.")

    return _truncate("\n".join(lines), FIELD_VALUE_LIMIT)


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


def _preflight_summary(preflight: Mapping[str, Any]) -> str:
    presentations = [
        (label, *_outcome(outcome_value))
        for label, outcome_value in preflight.items()
    ]
    if presentations and all(
        normalized == "success"
        for _label, normalized, _emoji, _status in presentations
    ):
        labels = " · ".join(label for label, *_rest in presentations)
        return f"✅ **All passed** · {labels}"

    return " · ".join(
        f"{emoji} {label}"
        for label, _normalized, emoji, _status in presentations
    ) or "No preflight results."


def collect_report(environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Collect typed report data from the documented environment variables."""

    env = os.environ if environ is None else environ
    scenarios: dict[str, dict[str, Any]] = {}
    for key, _label in SCENARIOS:
        prefix = f"CCH_REPORT_{key.upper()}"
        data, error = _parse_scenario(env.get(f"{prefix}_JSON"))
        scenarios[key] = {
            "outcome": env.get(f"{prefix}_OUTCOME", "unknown"),
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
            "Checkout": env.get("CCH_REPORT_CHECKOUT_OUTCOME", "unknown"),
            "Python": env.get("CCH_REPORT_PYTHON_OUTCOME", "unknown"),
            "Dependencies": env.get(
                "CCH_REPORT_DEPENDENCIES_OUTCOME", "unknown"
            ),
            "Unit tests": env.get("CCH_REPORT_UNIT_OUTCOME", "unknown"),
            "FNN auth": env.get("CCH_REPORT_AUTH_OUTCOME", "unknown"),
            "FNN nodes": env.get("CCH_REPORT_FNN_OUTCOME", "unknown"),
            "LND liquidity": env.get(
                "CCH_REPORT_LIQUIDITY_OUTCOME", "unknown"
            ),
            "Cleanup": env.get("CCH_REPORT_CLEANUP_OUTCOME", "unknown"),
        },
        "scenarios": scenarios,
        "run_url": env.get("CCH_REPORT_RUN_URL", ""),
        "run_number": env.get("CCH_REPORT_RUN_NUMBER", "unknown"),
        "run_attempt": env.get("CCH_REPORT_RUN_ATTEMPT", "unknown"),
        "sha": env.get("CCH_REPORT_SHA", "unknown"),
    }


def build_discord_payload(report: Mapping[str, Any]) -> dict[str, Any]:
    """Render one Discord embed. This function has no network side effects."""

    result, result_emoji, result_label = _outcome(report.get("job_result"))
    passed = result == "success"
    run_number = _one_line(report.get("run_number"), "unknown")
    run_url = _text(report.get("run_url"), "")
    sha = _one_line(report.get("sha"), "unknown")
    short_sha = sha[:7] if sha != "unknown" else sha
    total_duration = _duration(report.get("total_seconds"))
    fiber_source = _truncate(
        _one_line(report.get("fiber_source"), "unknown"), 80
    )
    fnn_version = _truncate(
        _one_line(report.get("fnn_version"), "unknown"), 80
    )
    run_attempt = _one_line(report.get("run_attempt"), "unknown")
    ref_line = (
        f"Branch `{_truncate(_one_line(report.get('branch'), 'unknown'), 100)}`"
        f" · `{short_sha}`"
    )
    if run_attempt not in {"1", "unknown"}:
        ref_line += f" · retry `{run_attempt}`"

    overview_lines = [
        f"{result_emoji} **{result_label}** · ⏱ {total_duration}",
        ref_line,
        (
            f"Fiber source `{fiber_source}`"
            f" · FNN `{fnn_version}`"
        ),
        f"Package `{_truncate(_one_line(report.get('fnn_package'), 'unknown'), 180)}`",
    ]
    schedule_summary = _schedule_summary(report)
    if schedule_summary:
        overview_lines.append(schedule_summary)

    fields = [
        {
            "name": "Run overview",
            "value": _truncate("\n".join(overview_lines), FIELD_VALUE_LIMIT),
            "inline": False,
        },
        {
            "name": "Preflight",
            "value": _truncate(
                _preflight_summary(report.get("preflight", {})),
                FIELD_VALUE_LIMIT,
            ),
            "inline": False,
        },
    ]

    scenarios = report.get("scenarios", {})
    for key, label in SCENARIOS:
        scenario_report = scenarios.get(key, {})
        _normalized, emoji, _status = _outcome(
            scenario_report.get("outcome", "unknown")
        )
        fields.append(
            {
                "name": f"{emoji} {label}",
                "value": _scenario_summary(
                    scenario_report.get("outcome", "unknown"),
                    scenario_report.get("data"),
                    scenario_report.get("parse_error"),
                ),
                "inline": False,
            }
        )

    scenario_outcomes = [
        _outcome(scenarios.get(key, {}).get("outcome", "unknown"))[0]
        for key, _label in SCENARIOS
    ]
    passed_scenarios = scenario_outcomes.count("success")
    description = (
        f"{passed_scenarios}/{len(SCENARIOS)} smoke scenarios passed."
        if passed
        else (
            f"{passed_scenarios}/{len(SCENARIOS)} smoke scenarios passed; "
            "workflow did not pass."
        )
    )
    embed = {
        "title": _truncate(
            f"{result_emoji} CCH Daily Smoke · Testnet · #{run_number}", 256
        ),
        "description": _truncate(description, 4096),
        "color": SUCCESS_COLOR if passed else FAILURE_COLOR,
        "fields": fields,
        "footer": {"text": "Scheduled GitHub Actions report"},
    }
    if run_url:
        embed["url"] = run_url

    # All individual limits above keep normal reports comfortably under 6,000
    # characters. Retain a guard so future fields cannot silently exceed the
    # Discord API's aggregate embed limit.
    total_chars = (
        len(embed["title"])
        + len(embed["description"])
        + len(embed["footer"]["text"])
        + sum(len(field["name"]) + len(field["value"]) for field in fields)
    )
    if total_chars > EMBED_TOTAL_LIMIT:
        overflow = total_chars - EMBED_TOTAL_LIMIT
        for field in reversed(fields):
            reducible = max(0, len(field["value"]) - 80)
            reduction = min(reducible, overflow)
            if reduction:
                field["value"] = _truncate(
                    field["value"], len(field["value"]) - reduction
                )
                overflow -= reduction
            if overflow <= 0:
                break

    return {
        "allowed_mentions": {"parse": []},
        "embeds": [embed],
    }


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


def main() -> None:
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook_url:
        raise SystemExit("DISCORD_WEBHOOK_URL is required")
    send_discord_webhook(webhook_url, payload_from_env())
    print("Discord daily-smoke report sent")


if __name__ == "__main__":
    main()
