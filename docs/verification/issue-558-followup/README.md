# Issue 558: follow-up creation recovery

This evidence covers the first task on PR #560: `adversarial.file_followup`
serializes same-key workers, refuses failed searches, and reconciles an ambiguous
create without issuing another create. It does not qualify the remaining panel
isolation, author exclusion, source-closure retry, workspace restoration, or merge
commit binding tasks.

The lock uses a persistent per-key inode under the existing panel state directory.
All workers must share that directory. An atomic, fsynced `create_pending` receipt
is written before the request. Nonzero exits, timeouts, malformed responses, and
process death retain that receipt. Reconciliation requires both the exact title
and the panel-key marker, includes closed issues, and refuses truncated or malformed
search results. An unresolved attempt stays pending even after empty searches;
GitHub search cannot prove that a timed-out request was never accepted. A known
issue number is saved before source disposition, preventing duplicate filing if
that later operation fails. Source closure retries remain a separate task.

`proof.json` retains the exact argv and production source hashes. Only
`file_followup` was replaced temporarily with its original implementation from
`5247ea1f9e9f72191cbd232325b813219ff12a9a`. All eight selected regressions failed
with assertions (`original-red.log`, `original-red.xml`). The corrected source was
restored byte-for-byte, then all 64 focused tests passed (`corrected-green.log`,
`corrected-green.xml`). The tests use real filesystem locks and fresh processes,
including an `os._exit` immediately after a fake remote mutation. GitHub commands
and reviewers are mocked; no live state, real issues, or execution mirror changed.

Reproduce the focused suite with installed pytest:

```sh
PYTHONPATH=src python3 -m pytest tests/test_adversarial_review_at_head.py tests/test_adversarial_high_stakes_shape.py tests/test_merge_guard_panel.py -q -m "not slow"
PYTHONPATH=src python3 src/adversarial.py --selftest
PYTHONPATH=src python3 src/merge_guard.py --selftest
black --check --line-length 100 --exclude '(\.workflows-lib|node_modules)' .
```

The runner initially had neither pytest nor Black and could not resolve PyPI.
Their pinned release sources (`pytest-dev/pytest@9.1.1`, `psf/black@26.10.0`)
and required dependencies were loaded through the GitHub connector into `/tmp`,
outside the repository. The formatter used one worker and periodic event-loop
wakes because the sandbox did not deliver thread socket wakeups. Black's checking
and formatting code was unchanged. Collection found 3,087 tests: the previous
3,066 plus 21 new executed regressions. The floor preserves every skip and mypy
ceiling. This is an executed-node union, not a claim of a fresh full-suite run;
current-head hosted CI and the rest of acceptance remain required.
