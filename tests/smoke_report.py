"""Machine-readable output and CI payment details shared by live smoke tests."""

from __future__ import annotations

import json
import os
from html import escape
from pathlib import Path


SMOKE_REPORT_MARKER = "SMOKE_REPORT_JSON="
REPORT_KEYS = {"duration_seconds", "topology", "flows", "fees", "net"}
FLOW_KEYS = {"direction", "paid", "received"}


def _markdown_text(value: str) -> str:
    text = escape(value, quote=False).replace("\\", "\\\\")
    for character in ("`", "*", "_", "[", "]", "|"):
        text = text.replace(character, f"\\{character}")
    return text.replace("\n", "<br>")


def append_flow_summary(
    *,
    scenario: str,
    number: int,
    direction: str,
    money_path: tuple[tuple[str, str], ...],
    payment_hash: str,
    paid: str,
    received: str,
    fees: dict[str, str],
    balances: tuple[tuple[str, str, dict, dict], ...],
    assertions: str,
) -> str:
    """Append one verified payment immediately, preserving partial run results."""

    lines = [
        f"## {_markdown_text(scenario)} · FLOW {number} · "
        f"{_markdown_text(direction)}",
        "",
        "- **Status:** ✅ Success",
        f"- **Payer:** {_markdown_text(paid)}",
        f"- **Recipient:** {_markdown_text(received)}",
        f"- **Assertions:** ✅ {_markdown_text(assertions)}",
        f"- **Payment hash:** {_markdown_text(payment_hash)}",
        "",
        "### Payment path",
        "",
    ]
    lines.extend(
        f"- **{_markdown_text(label)}:** {_markdown_text(path)}"
        for label, path in money_path
    )
    lines.extend(["", "### Fees", ""])
    lines.extend(
        f"- **{_markdown_text(label)}:** {_markdown_text(value)}"
        for label, value in fees.items()
    )
    for title, unit, before, after in balances:
        lines.extend(
            [
                "",
                f"### {_markdown_text(title)} ({_markdown_text(unit)})",
                "",
                "| Node | Before | After | Change |",
                "| --- | ---: | ---: | ---: |",
            ]
        )
        lines.extend(
            f"| {_markdown_text(node)} | {value:,} | {after[node]:,} | "
            f"{after[node] - value:+,} |"
            for node, value in before.items()
            if node not in {"scid", "chan_id", "channel_point", "channel_id"}
            and not node.endswith("spendable")
        )
    markdown = "\n".join(lines) + "\n\n"
    github_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if github_summary:
        with Path(github_summary).open("a", encoding="utf-8") as summary:
            summary.write(markdown)
    return markdown


def emit_smoke_report(report: dict) -> str:
    """Print a compact report and expose it as a GitHub Actions step output."""

    if set(report) != REPORT_KEYS:
        raise ValueError(f"smoke report must contain exactly {sorted(REPORT_KEYS)}")
    if not isinstance(report["duration_seconds"], (int, float)):
        raise TypeError("duration_seconds must be numeric")
    if not isinstance(report["topology"], str) or not report["topology"]:
        raise TypeError("topology must be a non-empty string")
    if not isinstance(report["flows"], list) or not report["flows"]:
        raise TypeError("flows must be a non-empty list")
    for flow in report["flows"]:
        if not isinstance(flow, dict) or set(flow) != FLOW_KEYS:
            raise ValueError(f"each flow must contain exactly {sorted(FLOW_KEYS)}")
        if not all(isinstance(flow[key], str) and flow[key] for key in FLOW_KEYS):
            raise TypeError("flow values must be non-empty strings")
    for key in ("fees", "net"):
        if not isinstance(report[key], dict):
            raise TypeError(f"{key} must be a mapping")
        if not all(
            isinstance(label, str)
            and label
            and isinstance(value, str)
            and value
            for label, value in report[key].items()
        ):
            raise TypeError(f"{key} entries must be non-empty strings")

    payload = json.dumps(report, ensure_ascii=False, separators=(",", ":"))
    print(f"{SMOKE_REPORT_MARKER}{payload}")

    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with Path(github_output).open("a", encoding="utf-8") as output:
            output.write(f"report={payload}\n")

    return payload
