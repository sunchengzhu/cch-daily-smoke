import sys

import pytest

from scripts import prepare_relay_connectivity as preparation
from test_daily_smoke_workflow import workflow_text


def test_preparation_precedes_all_payments_and_does_not_block_direct():
    workflow = workflow_text()
    preparation_step = workflow.split('      - name: Prepare relay peer connectivity', 1)[1].split('      - name:', 1)[0]
    assert 'continue-on-error: true' in preparation_step
    assert 'python scripts/prepare_relay_connectivity.py' in preparation_step
    assert workflow.index('id: relay_connectivity') < workflow.index('id: local_cch') < workflow.index('id: fiberswap_direct')
    relay_step = workflow.split('      - name: Run FiberSwap CCH smoke (via relay LND)', 1)[1].split('      - name:', 1)[0]
    assert 'CCH_FIBER_SWAP_RELAY_RECOVERY_PREPARED: "1"' in relay_step
    job_env = workflow.split('  cch-daily-smoke:', 1)[1].split('    steps:', 1)[0]
    assert 'CCH_FIBER_SWAP_RELAY_LND_CONTAINER: lnd-c' in job_env
    assert 'CCH_FIBER_SWAP_RELAY_TO_FIBER_SWAP_SCID: "5637388530143199233"' in job_env


@pytest.mark.parametrize('active', [True, False])
def test_preparation_records_original_channel_result_independent_of_api(monkeypatch, tmp_path, active):
    monkeypatch.setenv('CCH_FIBER_SWAP_FNN_CLI', sys.executable)
    summary = tmp_path / 'summary'
    monkeypatch.setenv('GITHUB_STEP_SUMMARY', str(summary))
    monkeypatch.setattr(preparation.relay, 'reconnect_inactive_relay', lambda _: ['new address from lnd-d'])
    def channel(config):
        assert config.wait_timeout == 20
        assert config.command_timeout == 5
        if not active:
            raise preparation.relay.RelayChannelInactive('still inactive')
        return {'channel_point': config.lnd_channel_point, 'active': True}
    monkeypatch.setattr(preparation.relay, 'wait_relay_lnd_channel_quiescent', channel)
    monkeypatch.setattr(preparation.relay, 'create_swap_order', lambda *_: pytest.fail('must not create orders'))
    assert preparation.prepare() == (0 if active else 1)
    text = summary.read_text()
    assert 'new address from lnd-d' in text
    assert ('preparation: ready' if active else 'preparation: unavailable') in text
    assert 'payments are not verified' in text
