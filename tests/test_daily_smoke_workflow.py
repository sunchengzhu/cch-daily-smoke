from pathlib import Path


WORKFLOW = (
    Path(__file__).resolve().parents[1]
    / ".github"
    / "workflows"
    / "cch-daily-smoke.yml"
)
UPDATE_FNN = Path(__file__).resolve().parents[1] / "scripts" / "update_fnn.sh"


def workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def update_fnn_text() -> str:
    return UPDATE_FNN.read_text(encoding="utf-8")


def test_discord_report_runs_for_daily_claim_or_gate_failure():
    workflow = workflow_text()

    assert "send_discord_report:" in workflow
    assert "default: false" in workflow
    assert "(github.event_name == 'schedule' || inputs.send_discord_report)" in workflow
    assert "(needs.daily-gate.outputs.should_run == 'true' || needs.daily-gate.result == 'failure')" in workflow
    assert "needs.cch-daily-smoke.result == 'failure'" not in workflow


def test_discord_report_receives_all_smoke_results_and_summaries():
    workflow = workflow_text()

    for step_id in ("local_cch", "fiberswap_direct", "fiberswap_relay"):
        assert f"id: {step_id}" in workflow
    for report_variable in (
        "CCH_REPORT_CHECKOUT_OUTCOME",
        "CCH_REPORT_DEPENDENCIES_OUTCOME",
        "CCH_REPORT_AUTH_OUTCOME",
        "CCH_REPORT_LOCAL_OUTCOME",
        "CCH_REPORT_DIRECT_OUTCOME",
        "CCH_REPORT_RELAY_OUTCOME",
        "CCH_REPORT_CLEANUP_OUTCOME",
        "CCH_REPORT_LOCAL_JSON",
        "CCH_REPORT_DIRECT_JSON",
        "CCH_REPORT_RELAY_JSON",
    ):
        assert report_variable in workflow

    assert "python3 scripts/send_daily_smoke_report.py" in workflow
    assert "sarisia/actions-status-discord" not in workflow


def test_fnn_metadata_outputs_are_emitted_before_fallible_update_stages():
    script = update_fnn_text()

    package_output = script.index(
        'write_github_output fnn_package "$TARGET_LABEL"'
    )
    download = script.index('log "downloading $ASSET_NAME"')
    version_output = script.index(
        'write_github_output fnn_version "$TARGET_FNN_VERSION"'
    )
    service_update = script.index('if [[ "$FNN_NEEDS_UPDATE" == "1" ]]')
    rpc_health = script.index('log "waiting for Fiber RPC health"')

    assert package_output < download
    assert version_output < service_update < rpc_health
