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
    assert "(github.event_name == 'schedule' || inputs.scheduled_date != '' || inputs.send_discord_report)" in workflow
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


def test_smoke_scenarios_finish_before_the_job_is_failed():
    workflow = workflow_text()
    job = workflow.split("  cch-daily-smoke:\n", 1)[1].split("\n  report:\n", 1)[0]

    assert (
        job.index("id: local_cch")
        < job.index("id: fiberswap_direct")
        < job.index("id: fiberswap_relay")
        < job.index("id: cleanup_auth")
        < job.index("id: report_metadata")
        < job.index("name: Evaluate smoke outcomes")
    )
    for name in (
        "Run local CCH smoke",
        "Run FiberSwap CCH smoke (direct Lightning channel)",
        "Run FiberSwap CCH smoke (via relay LND)",
    ):
        step = job.split(f"      - name: {name}\n", 1)[1].split("      - name:", 1)[0]
        assert "continue-on-error: true" in step
        if name != "Run local CCH smoke":
            assert "if: ${{ !cancelled() && steps.lnd_liquidity.outcome == 'success' }}" in step

    final_step = job.split("      - name: Evaluate smoke outcomes\n", 1)[1]
    assert "if: ${{ !cancelled() && steps.lnd_liquidity.outcome == 'success' }}" in final_step
    for scenario in ("local_cch", "fiberswap_direct", "fiberswap_relay"):
        assert f"steps.{scenario}.outcome" in final_step
    assert '!= "success"' in final_step
    assert "exit 1" in final_step
    assert "continue-on-error:" not in final_step


def test_ci_summary_is_independent_of_discord_and_written_before_notification():
    report_job = workflow_text().split("  report:\n", 1)[1]
    job_config, steps = report_job.split("    steps:\n", 1)
    assert "always()" in job_config
    assert "should_run == 'true'" in job_config
    assert "send_discord_report" not in job_config
    assert "DISCORD_WEBHOOK_URL" not in job_config
    summary, notification = steps.split("      - name: Send compact Discord report", 1)
    assert "if: ${{ always() }}" in summary
    assert "scripts/send_daily_smoke_report.py --summary-only" in summary
    assert "send_discord_report" not in summary
    assert "(github.event_name == 'schedule' || inputs.scheduled_date != '' || inputs.send_discord_report)" in notification
    assert "inputs.send_discord_report" in notification
    assert "continue-on-error: true" in notification
    assert "DISCORD_WEBHOOK_URL" in notification


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


def test_relay_failure_class_reaches_the_report_job():
    """An environment fault must be able to surface in the Discord report."""

    workflow = workflow_text()
    job = workflow.split("  cch-daily-smoke:\n", 1)[1].split("\n  report:\n", 1)[0]
    report_job = workflow.split("\n  report:\n", 1)[1]

    assert "id: fiberswap_relay" in job
    assert "relay_failure_class: ${{ steps.fiberswap_relay.outputs.failure_class }}" in job
    assert "CCH_REPORT_FAILURE_CLASS: ${{ needs.cch-daily-smoke.outputs.relay_failure_class }}" in report_job


def test_failure_diagnostics_reach_the_report_job_for_each_external_scenario():
    workflow = workflow_text()
    for scenario in ("direct", "relay"):
        assert (
            f"{scenario}_diagnostic: ${{{{ steps.fiberswap_{scenario}.outputs.failure_diagnostic }}}}"
            in workflow
        )
        assert (
            f"CCH_REPORT_{scenario.upper()}_DIAGNOSTIC: ${{{{ needs.cch-daily-smoke.outputs.{scenario}_diagnostic }}}}"
            in workflow
        )
    assert 'CCH_FIBER_SWAP_RELAY_TO_FIBER_SWAP_SCID: "5637388530143199233"' in workflow
