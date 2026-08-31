# Project Workflow

These instructions apply to the whole TTD repository.

## Completion and release rule

- If the requested task is completed, do not leave the finished change only in the working tree: commit it, push it, and deploy it.
- Production deployment is performed by `.github/workflows/deploy.yml` after a push to `main`. A successful GitHub Actions deploy is the project deploy step.
- Do not commit `.env`, Telegram tokens, cookies, SSH keys, or any other secrets. Keep `.env` local and update the VPS secret configuration separately when explicitly required.
- Preserve unrelated worktree changes. Stage only files belonging to the current task.

## Post-Task Review

- After any changes to source, config, test, or workflow files, run the relevant checks before the final answer.
- Before the final answer, run a separate `caveman-review` pass.
- If a sub-agent/reviewer tool is available, use a separate reviewer sub-agent. Otherwise perform a self-review using `caveman-review`.
- Review only real problems: bugs, broken behavior, regressions, missing tests, risky cleanup/removal, and edge cases.
- Use terse findings from `caveman-review`.
- If the skill is unavailable by name, use `C:\Users\bnvspace\.agents\skills\caveman-review\SKILL.md`.
- If review finds problems, fix them and rerun the relevant checks.
- After substantial review fixes, repeat `caveman-review` once.
- If problems remain after the repeat review, do not loop; report them to the user.
- Skip review only for docs-only changes, clean answers without file changes, or when the user explicitly asks not to run review.

## Review Format

Write `caveman-review` findings as briefly as possible:

```text
<file>:L<line>: 🔴 bug: problem. concrete fix.
<file>:L<line>: 🟡 risk: problem. concrete fix.
<file>:L<line>: 🔵 nit: problem. concrete fix.
<file>:L<line>: ❓ q: question.
```

If there are no findings, the review output must be exactly:

```text
No findings.
```

## Project-Specific Checks

Run the project tests with the project virtual environment when it exists:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

If the project virtual environment is unavailable locally, use the same Python version as CI:

```powershell
py -3.11 -m compileall -q ttd_bot tests
py -3.11 -m pytest -q
```

For changes affecting Telegram handlers, subscription checks, downloader behavior, Camoufox, or the deployment workflow, also verify the real runtime after deployment:

- Confirm the GitHub Actions run for the pushed `main` commit completed successfully.
- Confirm the deployed commit SHA matches the pushed `git rev-parse HEAD`.
- Confirm `/opt/ttd-bot/current/ttd_bot/main.py`, `/opt/ttd-bot/current/ttd_bot/downloader.py`, and `/opt/ttd-bot/requirements.txt` are present on the VPS.
- Confirm `ttd-bot.service` is `active` and `running`.
- For behavior changes, run a bounded live smoke test and inspect service logs for errors and the actual user-visible result.

## Commit, Push, and Deploy

After the task is complete and checks/review pass:

```powershell
git add <only-files-for-this-task>
git commit -m "<conventional commit message>"
git push --no-verify origin main
gh run list --repo bnvspace/ttd --limit 1
gh run watch <run-id> --repo bnvspace/ttd --exit-status
```

The push triggers `.github/workflows/deploy.yml`, which installs dependencies, refreshes Camoufox, updates `/opt/ttd-bot`, restarts `ttd-bot.service`, and checks that the service is active. After the workflow succeeds, verify the live tree and service on the VPS; never print secrets while doing so.
