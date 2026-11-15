"""A version preflight failure must be understandable without opening job logs."""

import json
import re
from unittest import mock

import pytest

from scripts import send_daily_smoke_report as report
from test_daily_smoke_report import complete_env, fields_by_name, github_summary


def version_env(*, mismatch=True):
    env = complete_env()
    env["CCH_REPORT_FNN_VERSION"] = "fnn Fiber v0.10.0 (d6868d1 2026-11-13)"
    env["CCH_REPORT_FNN_LATEST_TAG"] = "v0.10.1"
    env["CCH_REPORT_FNN_CHECK"] = json.dumps({
        "status": "version-mismatch" if mismatch else "verified",
        "expected_tag": "v0.10.0-rc3" if mismatch else "v0.10.0",
        "expected_commit": "7c8b0e2" if mismatch else "d6868d1",
        "nodes": {
            "node1": "fnn Fiber v0.10.0 (d6868d1 2026-11-13)",
            "node2": "fnn Fiber v0.10.0 (d6868d1 2026-11-13)",
        },
        "cli_version": "fnn-cli 0.10.0-rc3" if mismatch else "fnn-cli 0.10.0",
        "running": {},
    })
    if mismatch:
        env["CCH_REPORT_JOB_RESULT"] = "failure"
        env["CCH_REPORT_FNN_OUTCOME"] = "failure"
        for key in ("LOCAL", "DIRECT", "RELAY"):
            env[f"CCH_REPORT_{key}_OUTCOME"] = "skipped"
            env[f"CCH_REPORT_{key}_JSON"] = ""
    return env


def test_version_mismatch_is_visible_in_both_report_headlines_and_all_versions():
    env = version_env()
    payload = report.payload_from_env(env)
    embed = payload["embeds"][0]
    summary = github_summary(env)
    versions = fields_by_name(payload)["Version check"]

    assert "Fiber version mismatch" in embed["title"]
    assert summary.startswith("# CCH Daily Smoke · Fiber version mismatch\n")
    assert summary.index("Version check") < summary.index("## Run overview")
    for value in ("v0.10.0-rc3", "d6868d1", "node1", "node2", "CLI", "No payment scenarios ran."):
        assert value in versions
        assert value in summary
    assert not re.search(r"[\u4e00-\u9fff]", json.dumps(payload, ensure_ascii=False) + summary)
    assert embed["color"] == report.FAILURE_COLOR
    assert "Failure diagnosis" not in fields_by_name(payload)


def test_newer_release_is_advisory_when_the_pinned_versions_match():
    env = version_env(mismatch=False)
    embed = report.payload_from_env(env)["embeds"][0]

    assert embed["title"].startswith("✅")
    assert embed["color"] == report.SUCCESS_COLOR
    assert "version mismatch" not in embed["title"]
    fields = fields_by_name(report.payload_from_env(env))
    assert "Version check" not in fields
    assert "Fiber [0.10.0 release]" in fields["Run overview"]
    assert "latest [0.10.1 release]" in fields["Run overview"]
    assert "informational" not in github_summary(env).lower()


def test_matching_release_is_shown_once_without_node_inventory():
    env = version_env(mismatch=False)
    env["CCH_REPORT_FNN_VERSION"] = "fnn Fiber v0.10.1 (d9b6bbd 2026-11-14)"
    check = json.loads(env["CCH_REPORT_FNN_CHECK"])
    check.update({
        "expected_tag": "v0.10.1", "expected_commit": "d9b6bbd",
        "nodes": {node:env["CCH_REPORT_FNN_VERSION"] for node in ("node1", "node2")},
        "running": {node:"0.10.1 (d9b6bbd 2026-11-14)" for node in ("node1", "node2")},
        "cli_version": "fnn-cli 0.10.1",
    })
    env["CCH_REPORT_FNN_CHECK"] = json.dumps(check)
    payload = report.payload_from_env(env)
    rendered = json.dumps(payload, ensure_ascii=False)
    summary = github_summary(env)
    link = "[0.10.1 release](https://github.com/nervosnetwork/fiber/releases/tag/v0.10.1)"

    assert set(fields_by_name(payload)) == {"Run overview", "Scenarios"}
    assert fields_by_name(payload)["Run overview"].splitlines()[1] == f"Fiber {link}"
    assert rendered.count(link) == summary.count(link) == 1
    for value in ("node1", "node2", "CLI", "d9b6bbd", "Latest", "Version check", "Package"):
        assert value not in rendered
        assert value not in summary
    assert not re.search(r"[\u4e00-\u9fff]", rendered + summary)


def test_same_tag_rpc_commit_mismatch_keeps_the_actual_difference():
    env = version_env()
    check = json.loads(env["CCH_REPORT_FNN_CHECK"])
    check.update({
        "expected_tag": "v0.10.1", "expected_commit": "d9b6bbd",
        "problems": [{
            "component": "node2 RPC", "kind": "version-mismatch",
            "expected": "v0.10.1 (d9b6bbd)", "actual": "0.10.1 (abcdef0 2026-11-13)",
        }],
    })
    env["CCH_REPORT_FNN_CHECK"] = json.dumps(check)
    versions = fields_by_name(report.payload_from_env(env))["Version check"]

    assert "Expected: v0.10.1 (d9b6bbd)" in versions
    assert "node2 RPC: 0.10.1 (abcdef0 2026-11-13)" in versions
    assert "node1" not in versions
    assert "CLI" not in versions
    assert "abcdef0" in github_summary(env)


def test_unavailable_latest_release_does_not_hide_the_actual_mismatch():
    env = version_env()
    env["CCH_REPORT_FNN_LATEST_TAG"] = ""
    versions = fields_by_name(report.payload_from_env(env))["Version check"]

    assert "Fiber version mismatch" in report.payload_from_env(env)["embeds"][0]["title"]
    assert "latest" not in fields_by_name(report.payload_from_env(env))["Run overview"]
    assert "v0.10.0-rc3" in versions


@pytest.mark.parametrize("raw", ["[]", "null", "{broken"])
def test_invalid_version_diagnostic_does_not_invent_a_version_cause(raw):
    env = version_env()
    env["CCH_REPORT_FNN_CHECK"] = raw
    embed = report.payload_from_env(env)["embeds"][0]

    assert "version mismatch" not in embed["title"]
    assert "Version check" not in fields_by_name(report.payload_from_env(env))


@pytest.mark.parametrize("fail", [False, True])
def test_release_lookup_is_bounded_and_never_installs_anything(monkeypatch, tmp_path, fail):
    destination = tmp_path / "env"
    monkeypatch.setenv("GITHUB_ENV", str(destination))
    response = mock.MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps({
        "tag_name": "v0.10.1", "prerelease": False, "draft": False,
    }).encode()
    urlopen = mock.Mock(side_effect=OSError("offline")) if fail else mock.Mock(return_value=response)
    monkeypatch.setattr(report.urllib.request, "urlopen", urlopen)

    report.main(["--check-latest-release"])

    assert urlopen.call_args.kwargs["timeout"] == 8
    assert urlopen.call_args.args[0].full_url == report.FIBER_RELEASE_API
    assert destination.read_text() == f"CCH_REPORT_FNN_LATEST_TAG={'v0.10.1' if not fail else ''}\n"


def test_version_field_preserves_discord_limits():
    env = version_env()
    check = json.loads(env["CCH_REPORT_FNN_CHECK"])
    check["nodes"]["node1"] = "😀" * 4000
    env["CCH_REPORT_FNN_CHECK"] = json.dumps(check)
    embed = report.payload_from_env(env)["embeds"][0]
    units = lambda value: len(value.encode("utf-16-le")) // 2

    assert all(units(field["value"]) <= 1024 for field in embed["fields"])
    assert sum(units(field["name"]) + units(field["value"]) for field in embed["fields"]) + units(embed["title"]) + units(embed["description"]) + units(embed["footer"]["text"]) <= 6000
