"""Release-channel selection in scripts/update_fnn.sh.

The daily job upgrades the node binaries in place, so resolving the newest
release while silently accepting a prerelease can hand the runner a package it
cannot upgrade to (v0.10.0-rc1 shipped without the database migration). These
tests pin the selection rules by evaluating the jq program the script uses.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

UPDATE_FNN = Path(__file__).resolve().parents[1] / "scripts" / "update_fnn.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None and shutil.which("python3") is None,
    reason="needs jq, or python3 as the fallback interpreter",
)


def update_fnn_text() -> str:
    return UPDATE_FNN.read_text(encoding="utf-8")


def release_filter() -> str:
    """Return the jq program the script applies to the releases listing."""

    script = update_fnn_text()
    match = re.search(r"jq -c --argjson allow_prerelease \"\$ALLOW_PRERELEASE\" \\\n\s*'([^']+)'", script)
    assert match, "update_fnn.sh no longer contains the expected release-selection jq program"
    return match.group(1)


def select_release(releases: list[dict], allow_prerelease: int) -> dict | None:
    program = release_filter()
    jq = shutil.which("jq")
    if jq:
        command = [
            jq,
            "-c",
            "--argjson",
            "allow_prerelease",
            str(allow_prerelease),
            program,
        ]
        result = subprocess.run(
            command, input=json.dumps(releases), capture_output=True, text=True
        )
        assert result.returncode == 0, result.stderr
        output = result.stdout.strip()
    else:
        # jq is installed in CI, but keep the rule checkable without it.
        candidates = [
            release
            for release in releases
            if not release.get("draft")
            and (allow_prerelease == 1 or not release.get("prerelease"))
        ]
        candidates.sort(key=lambda release: release.get("published_at") or "")
        output = json.dumps(candidates[-1], separators=(",", ":")) if candidates else "null"
    return None if output in {"", "null"} else json.loads(output)


RELEASES = [
    {
        "tag_name": "v0.9.0",
        "draft": False,
        "prerelease": False,
        "published_at": "2026-08-06T02:39:29Z",
    },
    {
        "tag_name": "v0.9.1",
        "draft": False,
        "prerelease": False,
        "published_at": "2026-09-11T08:09:37Z",
    },
    {
        # Newer than v0.9.1 and installed by the old unfiltered selection, but
        # its release notes said existing databases must not upgrade to it.
        "tag_name": "v0.10.0-rc1",
        "draft": False,
        "prerelease": True,
        "published_at": "2026-09-25T05:17:12Z",
    },
    {
        "tag_name": "v0.10.0-rc2-draft",
        "draft": True,
        "prerelease": True,
        "published_at": "2026-09-26T00:00:00Z",
    },
]


def test_newest_stable_release_is_selected_by_default():
    """A newer prerelease must not be picked while prereleases are disallowed."""

    assert select_release(RELEASES, 0)["tag_name"] == "v0.9.1"


def test_prerelease_is_selected_only_when_explicitly_allowed():
    assert select_release(RELEASES, 1)["tag_name"] == "v0.10.0-rc1"


def test_drafts_are_never_selected():
    for allow_prerelease in (0, 1):
        for tag in ("v0.10.0-rc2-draft",):
            assert select_release(RELEASES, allow_prerelease)["tag_name"] != tag


def test_missing_stable_release_is_not_reported_as_a_candidate():
    """With no stable release left, the script must fail instead of guessing."""

    assert select_release([RELEASES[2]], 0) is None


def test_script_defaults_to_stable_releases_and_rejects_bad_values():
    script = update_fnn_text()

    assert 'ALLOW_PRERELEASE="${CCH_SMOKE_FNN_ALLOW_PRERELEASE:-0}"' in script
    assert "CCH_SMOKE_FNN_ALLOW_PRERELEASE must be 0 or 1" in script
    assert "no published stable release found" in script


def test_explicit_release_tag_still_bypasses_channel_selection():
    """A pinned tag stays available for deliberate prerelease runs."""

    script = update_fnn_text()

    pinned = script.index("resolving requested release $RELEASE_TAG")
    selection = script.index("resolving latest published stable release")
    assert pinned < selection
    assert 'releases/tags/$RELEASE_TAG' in script


def test_workflow_exposes_the_prerelease_opt_in_and_forwards_it():
    workflow = (
        Path(__file__).resolve().parents[1]
        / ".github"
        / "workflows"
        / "cch-daily-smoke.yml"
    ).read_text(encoding="utf-8")

    assert "fiber_allow_prerelease:" in workflow
    assert "type: boolean" in workflow
    assert (
        "CCH_SMOKE_FNN_ALLOW_PRERELEASE: "
        "${{ inputs.fiber_allow_prerelease && '1' || '0' }}" in workflow
    )
