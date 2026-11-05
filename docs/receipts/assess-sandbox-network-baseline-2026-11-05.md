# Assess sandbox network baseline — 2026-11-05

Recorded before any edit to `src/dispatcher.py` for the Codex assess network fallback
(issue #1133, source #673). Main had no `_sandbox_has_network` and no `sandbox_fallback`
writer. `adapters.build_command(..., mode="assess")` selected `--sandbox read-only`.

## Real input

Date: 2026-11-05T02:57Z (runner clock, Thursday Nov 5, 2026).

Command:

```text
gh api rate_limit
```

Outcome: exit 0. GitHub returned rate-limit JSON. Core bucket: limit 5000, used 267,
remaining 4733, reset 1793848231. This is host connectivity, not a child-sandbox probe.
`command -v codex` was empty on this runner, so the dispatcher command below was not
executed in this pre-edit check (one lookup, no retry).

Assess argv on this checkout, same day, before the fallback existed:

```text
['codex', 'exec', '--skip-git-repo-check', '--cd', '/home/runner/work/Orchestrator/Orchestrator', '--sandbox', 'read-only', 'Call gh api rate_limit and print the JSON.']
```

## Dispatcher command for the post-merge live gate

```text
/opt/hostedtoolcache/Python/3.14.9/x64/bin/python3 src/dispatcher.py offload --agent codex --mode assess --cwd . --timeout 120 --prompt 'Run gh api rate_limit and print the raw JSON to stdout. Do not create, modify or delete any file.'
```

Use the repository-selected absolute interpreter when it differs. `/opt/anaconda3/bin/python3`
is the fallback named by the issue when this path is absent.

## Attempts-and-clock stop rule

- One probe attempt. No second probe while the first is running, and no probe retry.
- Total probe clock: 5.0 seconds from entry, including process start. On expiry classify
  `timeout` and stop.
- DNS failure and connection failure take the network-failure path and stop the probe.
- Authentication or API rejection (HTTP 4xx/5xx, bad credentials) stops the probe as
  network-present. It does not select workspace-write.
- A probe that cannot start is not evidence of a networkless sandbox.
- The live assess command is a separate post-merge gate. Do not launch it to manufacture
  the seven-day switch-review window.
