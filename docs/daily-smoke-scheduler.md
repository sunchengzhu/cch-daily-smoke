# Daily smoke server scheduler

`test-new-02` uses `cch-daily-smoke-dispatch.timer` to dispatch the daily run at
10:00 Asia/Shanghai. It checks again every five minutes until 23:55 and after
boot. The dispatch state prevents additional requests once GitHub has accepted
the day's request. The 10:17 GitHub schedule is an independent, best-effort fallback.

Deployment status (2026-09-12): the units are installed but the timer is paused.
The server's pre-existing GitHub login has read-only repository access and dispatch
returns HTTP 403. Configure the dedicated credential below before enabling it.
The GitHub fallback is enabled; the workstation Codex automation is paused.

## Deployment

The runner account must be `ckb`, with Python 3.9+, GitHub CLI, synchronized time,
systemd, and non-interactive sudo. Create a fine-grained token limited to
`sunchengzhu/cch-daily-smoke` with Actions read/write access and save it as repository
secret `CCH_SMOKE_DISPATCH_TOKEN`. The installer places it in a `ckb`-owned 0600
file outside the checkout; only the GitHub CLI subprocess receives it. Never put
the token in a commit, workflow input or chat, or retain a job's temporary token.

From GitHub Actions, run **cch scheduler administration** with operation `check`
for a read-only preflight. Once the secret is configured, operation `verify-dispatch`
confirms write permission by dispatching today's dated run after 10:00 Beijing
(at most once enters smoke). Then operation `install` installs the repository's script and units,
enables the timer and starts its service once to validate dispatch.
After 10:00, this validation can trigger today's smoke if it has not been claimed.
The installation does not modify FNN services or their data.

Alternatively, on `test-new-02`, as `ckb`, from a checkout of `main`:

```sh
bash scripts/install_daily_smoke_timer.sh
```

Direct installation needs `CCH_SMOKE_DISPATCH_TOKEN` in the environment or the
credential file already installed. Token expiry/revocation requires updating the
repository secret and running `install` again; it does not alter the account's
global GitHub CLI login. The script also supports an existing `gh` login when no
`CCH_SMOKE_DISPATCH_TOKEN_FILE` is configured, but the deployed service explicitly
uses the dedicated file.

The installed script is independent of the Actions checkout:

- `/usr/local/lib/cch-daily-smoke/dispatch_daily_smoke.py`: installed dispatcher.
- `/var/lib/cch-daily-smoke-dispatch/`: persistent request state and lock.
- `/var/lib/cch-daily-smoke-dispatch/github-token`: private credential, never log it.
- `/home/ckb/.local/state/cch-daily-smoke/executions/`: shared execution claims.
- `/etc/systemd/system/cch-daily-smoke-dispatch.{service,timer}`: systemd units.

Keep both state directories across deploys. A manual run without `scheduled_date`
bypasses daily claims. A same-day explicit re-run of the original GitHub run keeps
its run ID and is allowed. For recovery on a later day, use a manual run without
`scheduled_date`; old dated dispatches skip. A failed daily smoke is not
automatically retried.

## Dispatch confirmation and failures

The dispatcher accepts the GitHub API's 200 response with a run ID and legacy 204.
If the response is lost, it retains a pending record and looks up the exact daily
run name on `main` before doing anything else. It never blindly repeats an
uncertain POST. Failures before submitting a request may be retried at the next
timer tick. After a request with an uncertain outcome, subsequent ticks only
reconcile its run ID; unresolved pending state requires operator review. The
GitHub fallback remains available and uses the same execution claim.

Inspect both scheduler logs and Actions: a healthy timer cannot guarantee an
available API or an idle runner. The daily smoke and stability workflows share
this runner, so a long stability job can delay the smoke even after prompt dispatch.

```sh
systemctl list-timers cch-daily-smoke-dispatch.timer --all
systemctl status cch-daily-smoke-dispatch.service
journalctl -u cch-daily-smoke-dispatch.service --since today
CCH_SMOKE_DISPATCH_TOKEN_FILE=/var/lib/cch-daily-smoke-dispatch/github-token \
  python3 /usr/local/lib/cch-daily-smoke/dispatch_daily_smoke.py --check
gh run list --repo sunchengzhu/cch-daily-smoke --workflow cch-daily-smoke.yml
```

The timer's next event may be a five-minute catch-up check, not the next day's
10:00 run. An `accepted` record means GitHub accepted the request, not that tests
succeeded. Do not remove pending or execution records just to force a retry:
first inspect the matching GitHub run; use its explicit re-run or a manual run.

## Rollback

```sh
sudo systemctl disable --now cch-daily-smoke-dispatch.timer
```

This stops the new dispatcher. Leave the state files intact; the GitHub fallback
remains enabled and skips a day already claimed. A repository rollback that
removes the execution gate must be coordinated with stopping the timer first.
The administration workflow's `pause` operation provides the same timer stop
without requiring SSH.
