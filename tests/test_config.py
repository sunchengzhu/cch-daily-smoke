import sys

from test_cch_daily_smoke import CWBTC_SCRIPT, CchSmokeConfig


def test_blank_channel_ids_enable_auto_discovery(monkeypatch):
    monkeypatch.setenv("CCH_SMOKE_FNN_CLI", sys.executable)
    monkeypatch.setenv("CCH_SMOKE_FIBER_CHANNEL_ID", "")
    monkeypatch.setenv("CCH_SMOKE_LND_CHANNEL_ID", "")
    monkeypatch.delenv("CCH_SMOKE_UDT_SCRIPT_JSON", raising=False)

    config = CchSmokeConfig.from_env()

    assert config.channel_id is None
    assert config.lnd_channel_id is None
    assert config.lnd_topup_sats == 3_000_000
    assert config.udt_script == CWBTC_SCRIPT
