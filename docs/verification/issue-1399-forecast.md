# Issue #1399 forecast verification

## Current round: response-independent consumption reach (2026-11-16)

Reviewed the recent commits through `26d3bc1` and PR #1400's current body. The
duplicated unchecked renderer task is stale: `summary_line` already renders
`drainable unknown` while retaining all rule-arm counts. Attempting to reconcile
that checkbox through `github_update_pull_request` was rejected with
`MCP tool call requires approval, but approval policy is never`. No checkbox or
acceptance criterion was changed. The connector confirmed the PR is open and
ready for review at `26d3bc10ee97e2b8fb304ce977492ea5a0724969`.
The requested blocker comment and additive `needs-human` label were also rejected
with the same approval-policy error; neither was applied.

The active review finding at `src/outcomes.py:2243` is valid. A consumption of
`#9@100`, unread PR `700@200`, cached final cover `701@250 -> #9`, and rule cycle
`#1@300` with candidate `702@400` always leave the rule cycle reachable under the
default budget. The previous forecast incorrectly returned `None`.

Added a read-only proof that reuses `_PrSearch` and the consumption traversal.
Private young-negative responses establish the maximum possible consumption
reads without settling candidates. A numeric result is returned only when every
pending rule cycle remains disjoint from those unread consumption keys and fits
in the worst-case remaining budget. Shared keys or insufficient budget retain
the existing indeterminate result. No hypothetical cover is persisted or graded.

Added 32 pytest variants comparing the offline forecast with injected forecasts
and actual linker reach. They cover the review example, repeated young PRs,
one- and two-read budgets, successful/negative/failed responses, and shared PRs
that can settle a rule pick during consumption. Offline checks forbid fetching,
opening the Brain write path, and appending evidence; all state bytes must remain
unchanged.

Validation with `/opt/hostedtoolcache/Python/3.14.10/x64/bin/python`:

- A standard-library reproducer against `HEAD`'s original `outcomes.py` failed:
  `AssertionError: (None, False, 20, 300, None, 1)`.
- The same reproducer passed against the changed source; all 32 standalone
  offline/injected/linker comparisons passed. These do not replace pytest.
- Both module selftests, Python compilation, and `git diff --check` passed.
- The required full suite and named mutation-test command both stopped before
  collection with `No module named pytest`. The named removal/restoration pytest
  criterion remains unverified in this environment.
- Black formatting and the exact repository-wide check both failed because
  `black` is not installed. Installing pytest and Black failed because
  `pypi.org` could not resolve. The mandatory formatting gate prevents a commit.
- The sandbox also grants read-only access to `.git`; no branch, PR, commit,
  workflow, or production-state change was made. Source/test changes remain
  available in the working tree for a run with the required tools and Git access.

## Reproducible mutation runner (earlier round, 2026-11-16)

Reviewed commits `1d3518d`, `8a41f45`, `b3af101`, and `e4777f9`. The renderer
already prints `drainable unknown`, and the latest regression preserves judged,
no-delivery and unresolved counts together. The duplicated renderer checkbox in
the PR body is stale. Updating that checkbox through the GitHub connector was
rejected: `MCP tool call requires approval, but approval policy is never`.
The PR remains open and ready for review. No acceptance checkbox was changed.

Added `scripts/verify_triage_shadow_mutation.js` to make the outstanding mutation
check repeatable with the explicitly selected interpreter:

```sh
PYTHON=/absolute/path/to/python3 node scripts/verify_triage_shadow_mutation.js
```

It requires the named test to pass, replaces only the actual rule-pass invocation
with `pass`, requires the named test to fail for missing `pr_reads`, restores the
original source bytes, and requires the same command to pass again. A fresh
bytecode-cache directory prevents stale imports. Successful output contains all
three transcripts, source/test hashes, and the measured Git HEAD. Missing pytest,
collection errors, timeouts, unexpected assertions and changed inputs refuse
successful evidence. Source restoration also runs after exceptions.

Verified in this round:

- `node tests/test_verify_triage_shadow_mutation.js`: 26 unit tests passed.
- `node tests/test_issue_1138_mutation.js`: 29 existing helper tests passed.
- `node tests/test_redirect_routing_mutations.js`: 11 existing helper tests passed.
- Both Python module selftests passed, as did JavaScript syntax and diff checks.

The runner unit tests use simulated pytest output and **do not** establish the
Python acceptance criteria. The selected interpreter remains
`/opt/hostedtoolcache/Python/3.14.10/x64/bin/python3`; both the full triage-shadow
pytest command and the named test command with `-m 'not slow'` failed before
collection with `No module named pytest`. The real mutation runner refused its
baseline, emitted no success receipt, and left the Python source/test unchanged.
Black is also unavailable, and package-host DNS resolution fails. This round
changes only JavaScript and this verification note, so it introduces no Python
changes requiring the Black pre-commit gate. The remaining acceptance checks
still require pytest in the selected interpreter.

The required commit was attempted after verification, but `git add` failed:
`Unable to create '.git/index.lock': Read-only file system`. This environment
grants only read access to `.git`; all three round changes remain uncommitted on
`codex/issue-1399`. Follow-up requires writable Git metadata as well as pytest.

The linker and forecast now share consumption eligibility/traversal and live rule-cycle
selection. Both retain the ordered rule drain and `_PrSearch` cache/budget behavior.
An unknown response consuming the final read now leaves an exact reach: zero during
consumption, or the cycles already reached during the rule pass.

Regression tests in `tests/test_triage_shadow.py` cover failed-read reuse, repeated
unsettled reads, linked-consumption filtering before the limit, final-read reach, and
production forecasting without fetching PRs or persisting evidence. Existing
`summary_line` rendering already preserves judged/no-delivery/unresolved fields and
prints `drainable unknown`; the stronger offline check verifies that behavior.

Verified locally with `/opt/anaconda3/bin/python3` (3.12.2):

- `python3 src/outcomes.py --selftest`: passed.
- `python3 src/triage_shadow.py --selftest`: passed.
- Standard-library harness `/tmp/check_rule_pick_forecast.py`: four checks passed,
  including six budget/cache scenarios and six final-read response variants.
- Additional isolated checks: both linked-consumption limit cases passed; completed
  consumption linking and an idempotent retry passed.
- `python3 -m py_compile src/outcomes.py tests/test_triage_shadow.py`: passed.
- `git diff --check`: passed.

## Formatting gate

```text
$ black --line-length 100 src/outcomes.py tests/test_triage_shadow.py
reformatted tests/test_triage_shadow.py

All done! ✨ 🍰 ✨
1 file reformatted, 1 file left unchanged.

$ black --check --line-length 100 --exclude '(\.workflows-lib|node_modules)' .
All done! ✨ 🍰 ✨
712 files would be left unchanged.
```

## Pytest mutation: `_resolve_rule_pick_covers` in `link_triage_consumption`

Invocation temporarily removed from `src/outcomes.py::link_triage_consumption`, then restored.
Command: `/opt/anaconda3/bin/python3 -m pytest tests/test_triage_shadow.py::test_linker_resolves_rule_picks_within_the_read_bound -vv`

### Removed (exit 1)

```text
tests/test_triage_shadow.py::test_linker_resolves_rule_picks_within_the_read_bound FAILED [100%]

=================================== FAILURES ===================================
____________ test_linker_resolves_rule_picks_within_the_read_bound _____________

    def test_linker_resolves_rule_picks_within_the_read_bound(brain):
        ...
        result = outcomes.link_triage_consumption(now=10_000, pr_reader=read)
>       assert result["pr_reads"] == 1 <= outcomes.PR_READS_PER_CALL
               ^^^^^^^^^^^^^^^^^^
E       KeyError: 'pr_reads'

tests/test_triage_shadow.py:622: KeyError
=========================== short test summary info ============================
FAILED tests/test_triage_shadow.py::test_linker_resolves_rule_picks_within_the_read_bound - KeyError: 'pr_reads'
============================== 1 failed in 0.69s ===============================
```

### Restored (exit 0)

```text
tests/test_triage_shadow.py::test_linker_resolves_rule_picks_within_the_read_bound PASSED [100%]

============================== 1 passed in 0.27s ===============================
```

## Full `tests/test_triage_shadow.py`

```text
$ /opt/anaconda3/bin/python3 -m pytest tests/test_triage_shadow.py -q
........................................................................ [ 58%]
...................................................                      [100%]
123 passed in 10.17s
```

Committed on branch `codex/issue-1399` (PR #1400); production mirror sync not performed here.

## Follow-up verification in the hosted sandbox (2026-11-16)

Found an additional determinate case: two cycles with disjoint PR candidates and a
two-read budget always reach both cycles, independently of either response. Before
the follow-up change, the offline forecast returned `None`; a standalone Python
assertion expecting `2` failed. `_independent_rule_pick_reach` now proves reach
when candidates are disjoint and the worst-case preceding reads leave budget for
each cycle. It counts repeated non-final candidates separately so a young PR
cannot hide possible budget exhaustion.

Added 12 pytest cases comparing independent cycles with actual linker traversal,
including reverse corpus order, one- and two-read budgets, failed/negative/positive
responses, repeated young PRs, and no forecast fetches or persistence.

Current interpreter: `/opt/hostedtoolcache/Python/3.14.10/x64/bin/python3`.
The earlier Python 3.12 results above describe a previous environment.

Checks completed in this environment:

- Both `src/outcomes.py --selftest` and `src/triage_shadow.py --selftest` passed.
- `python3 -m py_compile src/outcomes.py tests/test_triage_shadow.py` passed.
- `git diff --check` passed.
- Standalone standard-library harness `/tmp/verify_1399_reach.py` passed 24
  offline/injected/linker comparisons, including consumption-budget competition,
  shared PRs, repeated young PRs, and checks against forecast fetching or writing.
- Removing the actual `_resolve_rule_pick_covers(conn, corpus_rows, search)`
  invocation made that harness fail with `AssertionError: ([], 1)` (no cycle
  reached where one was forecast). Restoring the invocation restored all 24
  passing comparisons. Bytecode was cleared between mutations.

Required gates remain blocked in this environment:

```text
$ python3 -m pytest tests/test_triage_shadow.py -m 'not slow'
.../python3: No module named pytest

$ python3 -m pytest tests/test_triage_shadow.py::test_linker_resolves_rule_picks_within_the_read_bound -m 'not slow'
.../python3: No module named pytest

$ black --line-length 100 src/outcomes.py tests/test_triage_shadow.py
black: command not found

$ black --check --line-length 100 --exclude '(\.workflows-lib|node_modules)' .
black: command not found
```

The named pytest command was blocked both with the actual invocation removed
and restored; this is not a completed pytest mutation check. Installing pytest
and Black also failed because the sandbox could not resolve `pypi.org`; neither
package was available in the local package cache. The standalone harness does
not replace the required pytest suite. No additional checkboxes were marked
complete. Follow-up source/test changes remain **uncommitted** because the
mandatory Black gate cannot run. Required next step: provision pytest and Black,
run formatting and the required pytest checks, then commit on the existing branch.

## Mixed-grade unknown-reach regression (2026-11-16, subsequent round)

Added `test_summary_line_preserves_rule_grades_when_reach_is_unknown` in
`tests/test_triage_shadow.py`. It constructs real recorded state with one judged
durable delivery, one confirmed no-delivery pick in another repository, and two
unresolved picks sharing an unread PR. The offline report must retain all those
counts while rendering `drainable unknown`. Fetching, the Brain write path, and
evidence appends are forbidden during the report; file bytes must remain unchanged.
The existing renderer already implements this behavior, so no production change
was needed.

With `/opt/hostedtoolcache/Python/3.14.10/x64/bin/python3`:

- A separate standard-library harness reproducing that recorded state passed,
  including the no-fetch/no-write checks. This does **not** establish that the
  pytest regression passes.
- Both module selftests passed; test compilation and `git diff --check` passed.
- The required full pytest suite and the new individual regression both failed
  before collection: `No module named pytest`.
- The actual rule-pass invocation was temporarily removed and the required named
  pytest command attempted; it failed before collection with the same missing
  module. The invocation was restored byte-for-byte and the command attempted
  again, with the same result. This is **not** a successful mutation check.
- Black formatting and the exact repository-wide Black check both failed with
  `black: command not found`.
- Installing pytest and Black into `/tmp/issue-1399-tools` failed because the
  environment could not resolve `pypi.org`. No local copies of either tool were
  found. GitHub CLI inspection failed because `api.github.com` was unreachable,
  but the GitHub connector verified PR #1400 is open with `draft=false` at head
  `b3af10177bedc3c7cf87978efe845ca0160ef5f6`.
- The requested blocker comment and `needs-human` label were attempted through
  the GitHub connector. Both calls were refused with
  `MCP tool call requires approval, but approval policy is never`; neither
  mutation was applied.

The new regression remains **uncommitted** because the mandatory Black gate cannot
run. No additional task or acceptance checkbox is marked complete. Provision
pytest and Black, run formatting and the named pytest checks, then commit the
regression on the existing branch.
