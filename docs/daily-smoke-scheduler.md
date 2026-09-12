# Daily smoke server scheduler

`test-new-02` uses `cch-daily-smoke-dispatch.timer` to dispatch the daily run at
10:00 Asia/Shanghai. It checks again every five minutes until 23:55 and after
boot. The dispatch state prevents additional requests once GitHub has accepted
the day's request. The 10:17 GitHub schedule is an independent, best-effort fallback.

## Deployment

The runner account must be `ckb`, with Python 3.9+, GitHub CLI, synchronized time,
systemd, and non-interactive sudo. Its existing `gh` login must be able to dispatch
this repository's workflow. For a new credential, use a fine-grained token limited
to this repository with Actions write access. Authenticate on the server; do not
copy a personal workstation token or retain a job's temporary `GITHUB_TOKEN`.

From GitHub Actions, run **cch scheduler administration** with operation `check`
for a read-only preflight. Operation `install` installs the repository's script
and units, enables the timer and starts its service once to validate dispatch.
After 10:00, this validation can trigger today's smoke if it has not been claimed.
The installation does not modify FNN services or their data.

Alternatively, on `test-new-02`, as `ckb`, from a checkout of `main`:

```sh
bash scripts/install_daily_smoke_timer.sh
```

The installed script is independent of the Actions checkout:

- `/usr/local/lib/cch-daily-smoke/dispatch_daily_smoke.py`: installed dispatcher.
- `/var/lib/cch-daily-smoke-dispatch/`: persistent request state and lock.
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
