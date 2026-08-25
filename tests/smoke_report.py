"""Machine-readable output shared by the live smoke tests."""

from __future__ import annotations

import json
import os
from pathlib import Path


SMOKE_REPORT_MARKER = "SMOKE_REPORT_JSON="
REPORT_KEYS = {"duration_seconds", "topology", "flows", "fees", "net"}
FLOW_KEYS = {"direction", "paid", "received"}


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
