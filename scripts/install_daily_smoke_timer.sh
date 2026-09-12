#!/usr/bin/env bash
set -Eeuo pipefail

# The account and paths are deliberately scoped to the existing test-new-02 runner.
[[ "$(id -un)" == ckb ]] || { echo 'Run as the test-new-02 ckb account.' >&2; exit 1; }
[[ "$(hostname)" == test-new-02 ]] || { echo 'This installer is for test-new-02 only.' >&2; exit 1; }
cd "$(dirname "$0")/.."
sudo -n true
DISPATCH_TOKEN_PATH=/var/lib/cch-daily-smoke-dispatch/github-token
if [[ -n "${CCH_SMOKE_DISPATCH_TOKEN:-}" ]]; then
  DISPATCH_TOKEN_TEMP="$(mktemp)"
  trap 'rm -f -- "$DISPATCH_TOKEN_TEMP"' EXIT
  chmod 600 "$DISPATCH_TOKEN_TEMP"
  printf '%s' "$CCH_SMOKE_DISPATCH_TOKEN" > "$DISPATCH_TOKEN_TEMP"
  unset CCH_SMOKE_DISPATCH_TOKEN
  sudo -n install -d -m 0700 -o ckb -g ckb /var/lib/cch-daily-smoke-dispatch
  sudo -n install -m 0600 -o ckb -g ckb "$DISPATCH_TOKEN_TEMP" "$DISPATCH_TOKEN_PATH"
fi
sudo -n -u ckb test -s "$DISPATCH_TOKEN_PATH" || {
  echo 'Configure repository secret CCH_SMOKE_DISPATCH_TOKEN with Actions write access first.' >&2
  exit 1
}
sudo -n -H -u ckb /usr/bin/env -u GH_TOKEN -u GITHUB_TOKEN \
  CCH_SMOKE_DISPATCH_TOKEN_FILE="$DISPATCH_TOKEN_PATH" \
  /usr/bin/python3 scripts/dispatch_daily_smoke.py --check
systemd-analyze calendar '*-*-* 10..23:0/5:00 Asia/Shanghai'

sudo -n install -d -m 0755 /usr/local/lib/cch-daily-smoke
sudo -n install -m 0755 scripts/dispatch_daily_smoke.py \
  /usr/local/lib/cch-daily-smoke/dispatch_daily_smoke.py
sudo -n install -m 0644 deploy/cch-daily-smoke-dispatch.service \
  /etc/systemd/system/cch-daily-smoke-dispatch.service
sudo -n install -m 0644 deploy/cch-daily-smoke-dispatch.timer \
  /etc/systemd/system/cch-daily-smoke-dispatch.timer
sudo -n systemd-analyze verify /etc/systemd/system/cch-daily-smoke-dispatch.service \
  /etc/systemd/system/cch-daily-smoke-dispatch.timer
sudo -n systemctl daemon-reload
sudo -n systemctl enable --now cch-daily-smoke-dispatch.timer
# Validate the installed service using the same identity/environment as the timer.
# The daily state makes subsequent starts read-only no-ops after acceptance.
if ! sudo -n systemctl start cch-daily-smoke-dispatch.service; then
  sudo -n systemctl disable --now cch-daily-smoke-dispatch.timer
  sudo -n journalctl -u cch-daily-smoke-dispatch.service -n 20 --no-pager
  exit 1
fi
sudo -n systemctl show cch-daily-smoke-dispatch.service -p Result -p ExecMainStatus
sudo -n systemctl list-timers cch-daily-smoke-dispatch.timer --all --no-pager
sudo -n journalctl -u cch-daily-smoke-dispatch.service -n 12 --no-pager
