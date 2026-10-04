# Same-repository Gate status refusal — Workflows #3399

Source: https://github.com/stranske/Workflows/issues/3399. This bounded consumer-owned Gate repair does not close the 17-repository campaign.

Baseline `d326e59f5afeffd078cae488daa6b15a091640ac` lets the real retry helper return null for a refused commit-status write on a same-repository PR. The workflow then publishes nothing and succeeds, retaining whatever older status was already present. Forks have an expected read-only token; same-repository refusals are unexpected and must fail visibly.

The production workflow scripts run under Node with the actual retry helper; only the GitHub client and core are test doubles. New expectations over the unchanged baseline workflow fail for same-repository success/failure verdicts and the helper's 404 refusal route: **3 failed, 21 deselected**. After the null-response guard, the entire existing fork/deleted-fork, same-repository, rate-limit and origin/comment suite passes: **24 passed**. No tokens, permissions, event triggers or shared helper semantics changed. The fork summary fallback and non-success verdict floor remain intact.

Commands:

```sh
/opt/anaconda3/bin/python3 -m pytest tests/test_gate_commit_status_fork_tolerance.py -q --override-ini addopts= -k same_repo_refusal
/opt/anaconda3/bin/python3 -m pytest tests/test_gate_commit_status_fork_tolerance.py -q --override-ini addopts=
/opt/anaconda3/bin/python3 -m black --check --line-length 100 --exclude '(\.venv|\.workflows-lib|node_modules)' .
/opt/anaconda3/bin/python3 -m ruff check tests/test_gate_commit_status_fork_tolerance.py
git diff --check
```

Full Black checked 297 files. This is repo-specific recovery of Orchestrator's consumer-owned create-only Gate, as requested by the source campaign. Current exact-head CI, full review-thread enumeration, seven-minute floor, repository merge_guard and post-merge comparison remain required. No installed mirror publication or source issue closure is claimed.
