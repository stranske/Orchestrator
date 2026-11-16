# Issue #1393: model rejection verification

Verified on 2026-11-16 against the working tree for `codex/issue-1393` (PR #1397).

The classifier recognizes an anchored HTTP 400 response containing the provider's JSON
`error.message` for an unsupported or nonexistent model. It reports authoritative
`model_rejected` evidence with `should_shed=false`. Incident recording and direct shed
requests skip this category, which has no cooldown. The existing terminal-event detector
classifies a rejection before work as `transient_infra`, excludes it from learning, and
leaves outcomes unchanged when work events appear. Prose remains non-authoritative.

## Deliberate break and restoration

Command used at each step:

```sh
python3 -m pytest tests/test_provider_limit_infra.py::test_model_rejection_turn_failed_before_work_is_transient_infra -q -m "not slow"
```

1. Original implementation: **1 passed**, exit 0.
2. Removed only this branch from `classify_provider_failure`:

   ```python
   if _contains_model_rejection(text):
       return "model_rejected", "http_400_model_rejected", "high"
   ```

   Confirmed `provider_limit_before_work(_refused(MODEL_REJECTION))` returned `None`.
   The named test reported **1 failed**, exit 1, at the detector assertion:

   ```text
   E assert None == {'category': 'model_rejected', ...}
   tests/test_provider_limit_infra.py:245: AssertionError
   ```

3. Restored the exact source bytes in a `finally` block: **1 passed**, exit 0.

## Regression and formatting checks

```sh
python3 -m pytest tests/test_provider_limit_infra.py -q -m "not slow"
# 43 passed

python3 -m pytest tests/test_provider_limit_infra.py tests/test_rate_incidents.py tests/test_rate_incidents_expiry.py -q -m "not slow"
# 141 passed

black --line-length 100 src/rate_incidents.py tests/test_provider_limit_infra.py tests/test_rate_incidents.py
# 3 files left unchanged

black --check --line-length 100 --exclude '(\.workflows-lib|node_modules)' .
# 712 files would be left unchanged
```

The full formatting check used repository-pinned Black 26.10.1. The runner lacked pytest
and Black and could not resolve PyPI, so official tagged sources were placed under `/tmp`
(pytest 9.0.2 and Black 26.10.1) with existing local dependencies. Tests used supported
runtime/state path overrides under `/tmp`. Black used one worker and an event-loop timer
to wake its sandboxed worker completions; all formatting and AST checks remained enabled.

## Delivery blocker

The implementation is verified locally but remains uncommitted. Staging failed because
this environment mounts `.git` read-only:

```text
fatal: Unable to create '/home/runner/work/Orchestrator/Orchestrator/.git/index.lock': Read-only file system
```

Posting the evidence to PR #1397 and adding `needs-human` were also refused: the GitHub
write tools require approval, while this run's approval policy is `never`. No remote PR
metadata changed; the PR was verified open and ready for review. A writable runner must
commit the source, tests, and this verification record on the existing branch and deliver
them before checking the PR task boxes.
