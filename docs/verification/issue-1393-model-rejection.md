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

## Follow-up: provider envelope boundary

The next round found four false positives in the original HTTP prefix matcher:
`HTTP 400 handling example: {error JSON}`, `HTTP 400; HTTP 404: {error JSON}`,
`HTTP 400.5: {error JSON}`, and a newline between `HTTP` and `400`. Each returned
high-confidence `model_rejected` before the correction. The matcher now accepts only a
complete status field, an optional `Bad Request` reason phrase and separator, and the
immediately following JSON body. Horizontal whitespace inside the status field prevents
separate lines from supplying its words. JSON bodies on the next line remain supported.

Regression tests now exercise these four negative cases and three additional valid
prefixes, including LF and CRLF body separators. The follow-up checks passed:

- [x] Classifier and structured evidence: **53 passed** for model-rejection tests in
  `tests/test_rate_incidents.py`.
- [x] No shed or cooldown: related incident tests pass as part of the same selection.
- [x] Pre-work infrastructure classification, prose exclusion and work-event protection:
  `tests/test_provider_limit_infra.py`: **43 passed**.
- [x] Named acceptance test: **1 passed**.
- [x] Deliberate break and restoration repeated: deleting only the classifier branch
  returned `provider_limit_before_work: None` and **1 failed**, exit 1, at line 245;
  restoring the exact source bytes in `finally` returned **1 passed**, exit 0.
- [x] Combined incident, expiry and provider-refusal tests: **163 passed**.
- [x] Black formatted both changed Python files at line length 100; the required
  repository-wide check passed: **712 files would be left unchanged**.

All pytest commands included `-m "not slow"`. Pytest 9.0.2 and Black 26.10.1 were loaded
from official source snapshots under `/tmp` because the runner lacks these tools and
cannot resolve PyPI. Runtime/state overrides isolated tests under `/tmp`.
Black used one worker and an event-loop wakeup timer for this sandbox; its formatting
and AST checks remained enabled.

The follow-up commit attempt also failed at `git add` with the read-only `.git/index.lock`
error below. Posting the evidence comment and adding `needs-human` were refused because
the GitHub write tools require approval and this run's approval policy is `never`.
The three follow-up files remain modified locally; no new commit or remote task-checkbox
update was made. The PR was rechecked open with `isDraft=false` before handoff.

## Prior round delivery blocker

The prior round could not stage the implementation because its environment mounted
`.git` read-only:

```text
fatal: Unable to create '/home/runner/work/Orchestrator/Orchestrator/.git/index.lock': Read-only file system
```

Posting the evidence to PR #1397 and adding `needs-human` were also refused in that
round: the GitHub write tools required approval while the approval policy was `never`.
The implementation, tests and original verification record subsequently arrived in
commit `677e727` through the keepalive runner. The follow-up began on that commit, with
PR #1397 open and ready for review.
