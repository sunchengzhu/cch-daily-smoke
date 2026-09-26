"""Inspect/recover the relay connection independently of swap order creation."""

import json
import os
import sys
from dataclasses import replace
from html import escape
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
import pytest
import test_fiber_swap_relay_daily_smoke as relay


def prepare():
    notes = []
    status = "unavailable"
    try:
        config = replace(relay.RelayFiberSwapSmokeConfig.from_env(),
                         command_timeout=5, wait_timeout=20)
        notes.extend(relay.reconnect_inactive_relay(config))
        channel = relay.wait_relay_lnd_channel_quiescent(config)
        notes.append(f"Original channel active and quiescent: {channel['channel_point']}")
        status = "ready"
    except (Exception, pytest.fail.Exception) as error:
        notes.append(f"Relay preparation failed: {error}")
    for note in notes:
        print(f"RELAY_RECONNECT: {note}", flush=True)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        try:
            with Path(summary).open("a", encoding="utf-8") as output:
                output.write(f"## Relay connection preparation: {status}\n\n")
                output.write("Connection check only; relay swap payments are not verified.\n\n")
                output.write("<pre>" + escape("\n".join(notes)) + "</pre>\n\n")
        except OSError as error:
            print(f"Could not write relay preparation summary: {error}", flush=True)
    print("RELAY_PREPARATION=" + json.dumps({"status": status, "evidence": notes}))
    return 0 if status == "ready" else 1


if __name__ == "__main__":
    raise SystemExit(prepare())
