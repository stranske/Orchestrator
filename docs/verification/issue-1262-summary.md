# Issue 1262: linked summary failure

The linked run does not demonstrate either exception quoted in the issue or the
disappearing-file fixture failure named in its title. It rejects an unsealed
generated delivery. Removing this rejection would weaken the merge guard.

## Observed evidence

`gh run view 42500846882 --repo stranske/Orchestrator --log-failed` could not
connect to api.github.com in this environment. The GitHub connector supplied
the run's jobs and decoded job logs instead.

- [Summary job 128793812800](https://github.com/stranske/Orchestrator/actions/runs/42500846882/job/128793812800):
  step 30, **Enforce Gate success**, exits 1 at 2026-11-08T16:42:43Z with
  `Gate reported failure: Generated delivery is not sealed: delivery state is staging.`
  The preceding summary rendering and comment steps succeeded.
- [Detect job 128793283062](https://github.com/stranske/Orchestrator/actions/runs/42500846882/job/128793283062):
  **Classify changed paths** throws at
  `stranske/Workflows/.github/actions/path-classifier/classify.js:528` because
  `delivery_not_sealed:staging`. The helper revision is
  `40e80e510f61e81f5612064b035a77e9b8d28756`.
- The **generated delivery seal** job also failed; Python and Node test jobs
  were skipped in this run. Neither the PR identity RuntimeError nor a
  rate-limit rethrow appears as an executed error in the summary job.

The consumer-owned `.github/scripts/gate_summary.py` preserves the failed seal
in its emitted `state` and `description`. Rendering itself exits 0; the later
workflow enforcement step exits 1 from that state.

## Regression verification

`.github/scripts/__tests__/gate_delivery_seal.test.js` invokes the real renderer
with synthetic event and output files. It checks the exact staging description,
a valid exact-head seal, a seal for another head, and a failed seal job overriding
an otherwise valid event. The valid-seal control uses the docs fast pass to isolate
the seal decision; it does not establish that the real PR's other checks pass.

`node --test --test-isolation=none .github/scripts/__tests__/*.test.js` passed
all 44 tests, including the four new regressions. Default Node process isolation
cannot create its capture pipes in this sandbox; `--test-isolation=none` keeps
every test enabled.

A disposable copy of the renderer with the summary seal guard removed failed
three new hold assertions while the valid-seal control passed. The original
renderer was never edited, and its four tests pass. This mutation demonstrates
guard sensitivity, not a fix for the unobserved fixture failure.

Full verification was attempted with
`PYTEST_ADDOPTS='-m "not slow"' python3 src/verify.py --floor-may-lag`.
It did not complete and was interrupted after approximately five minutes.
`python3 -m pytest --version` separately confirmed that pytest is unavailable;
installing pytest and Black into a temporary virtual environment also failed.
No full-suite success is claimed. No Python files were changed.

## Remaining work

Obtain the disappearing-file fixture traceback or its actual failing run before
changing fixture code. The linked run needs bounded review settlement and a valid
seal for its exact head through the existing delivery lifecycle. No workflow edit,
pipeline dispatch, seal fabrication, or guard bypass was performed. A maintainer
must verify the regenerated delivery and the next sync-core classification.

## Environment restrictions

The task ledger under `.agents/` and Git metadata under `.git/` are read-only.
`git add` fails creating `.git/index.lock`, so this run cannot commit the new
test or update ledger statuses. GitHub comment and `needs-human` label requests
were also rejected because they require approval and this run's approval policy
is `never`. The diagnosis and handoff are recorded here instead.
PR #1263 was re-read before handoff and remains open with `draft=false` at
`a641ac80855787a9c0302a5972c2d83e23c4d9b9`.

## Keepalive follow-up verification

The linked summary job and run jobs were read again through the GitHub connector.
Step 30 still identifies the staging delivery hold as the failure. The requested
`gh run view 42500846882 --repo stranske/Orchestrator --log-failed` was also
retried and could not connect to api.github.com.

Three additional renderer regressions now exercise active checks, extending the
earlier docs-only controls:

- [x] Reproduce the staging hold with required Python jobs skipped and the seal
  job failed, matching the linked run; the hold takes precedence over missing
  Python artifacts or a pending rerun.
- [x] Verify that an exact-head seal still requires Python shard receipts when
  Python checks are required.
- [x] Verify that an exact-head seal preserves success, failure, and cancelled
  Node-job verdicts for changes without Python.

The focused command
`node --test --test-isolation=none .github/scripts/__tests__/gate_delivery_seal.test.js`
passes all seven tests. `git diff --check` also passes. No Python or workflow
files were edited. The full scripts test command
`node --test --test-isolation=none .github/scripts/__tests__/*.test.js`
passes all 47 tests with none skipped. The source-fix and live acceptance tasks remain incomplete:
these tests verify the existing rejection and its interaction with active checks,
and do not repair the external delivery lifecycle or reproduce the unobserved
disappearing-file failure.

The commit attempt failed at `git add` because `.git/index.lock` cannot be created
on the read-only filesystem. `python3 -m pytest --version` and `black --version`
again confirm that the Python verification tools are unavailable. Full Python
verification remains unconfirmed.

The connector rejected both the PR blocker comment and the `needs-human` label
because they require approval and this run's approval policy is `never`.
PR #1263 remains open with `draft=false` at
`a5b94ec1636129368a23a948f27e7e04bf395ebb`.

## Active Python recovery control

The linked job log was re-read through the connector. Its executed error remains
`Generated delivery is not sealed: delivery state is staging.` at
2026-11-08T16:42:43Z. The quoted identity RuntimeError appears in printed source,
not an executed traceback. The requested `gh run view` command still cannot
connect to api.github.com. No source defect justifying removal of the hold was
identified; the source-fix and live acceptance tasks remain incomplete.

The new `exact-head sealing clears the staging hold with all required Python
receipts` regression supplies twelve successful shard receipts across Python
3.13 and 3.14 with active Python, Node, and test-quality jobs. Identical job
results remain blocked for a staging record and succeed for a valid exact-head
seal. It asserts that all twelve receipt rows were loaded. Disposable renderer
copies that ignore the seal or retain the hold after sealing each make this new
test fail with an assertion error. These mutations prove test sensitivity;
they do not reproduce either exception quoted in the issue or repair the live
delivery lifecycle.

All 48 scripts tests pass with no skips using
`node --test --test-isolation=none .github/scripts/__tests__/*.test.js`.
The full verification command with `-m "not slow"` was attempted under a
45-second timeout and exited 124 without a verdict. Pytest and Black remain
unavailable; no Python files were changed. `git add` still fails because
`.git/index.lock` cannot be created on the read-only filesystem, so the changes
cannot be committed here. Automatic approval review again rejected the blocker
comment and `needs-human` label because the approval policy is `never`.
PR #1263 was re-read and is open with `draft=false` at
`03459b0abc75edd9ab6a313ecf74b26ab172a494`.

## Regenerated delivery verification

The regenerated sync PR now has head
`c0c5cb6aa8bf79aa855a54258c0ed423011cebeb`. Its delivery record is sealed to
that head, although the separate `workflows-consumer-sync:v1` metadata still
reports `lifecycle_state: staging`.

- [x] Verify the regenerated delivery's live summary result: Gate run
  [42518196137, attempt 2](https://github.com/stranske/Orchestrator/actions/runs/42518196137)
  completed successfully. Its
  [summary job 128848669696](https://github.com/stranske/Orchestrator/actions/runs/42518196137/job/128848669696)
  succeeded at 2026-11-08T20:16:03Z, and the exact-head `Gate / gate` commit
  status is `success`. These results were read through the GitHub connector;
  `gh run view` remains unavailable because it cannot connect to api.github.com.
- [x] Exercise the observed mixed-metadata envelope with active Node checks:
  a sealed delivery record succeeds despite staging sync metadata, and sealed
  sync metadata cannot clear a staging delivery hold.
- [x] Check the new regression's sensitivity: a disposable renderer modified
  to prefer the sync metadata's lifecycle state fails the new test. The unchanged
  repository renderer passes all nine focused tests, and the scripts suite passes
  all 49 tests with none skipped using
  `node --test --test-isolation=none .github/scripts/__tests__/*.test.js`.

The original linked run's failure remains a correct staging hold, rather than
either exception quoted in the issue. No renderer fix is justified by that
evidence, and no production source or workflow was changed. The new mutation
checks metadata precedence; it does not reproduce the unobserved disappearing-file
failure or establish the deliberate-break acceptance criterion for that failure.
Full local verification and the next sync-core classification remain unverified.

The full local command
`PYTEST_ADDOPTS='-m "not slow"' timeout 45s python3 -u src/verify.py --floor-may-lag`
exited 124 without a verdict. Pytest and Black are still unavailable, and no
Python files were edited. The commit attempt failed creating `.git/index.lock`
on the read-only filesystem. Requests to post the blocker comment and apply
`needs-human` to PR #1263 were rejected because they require approval and this
run's approval policy is `never`; neither external change was made.

## Required Python verdicts after sealing

The linked summary log was inspected again through the GitHub connector; its
executed failure is still the staging hold. `gh run view 42500846882 --repo
stranske/Orchestrator --log-failed` cannot connect to api.github.com. PR #1247
has since merged with the previously verified sealed head
`c0c5cb6aa8bf79aa855a54258c0ed423011cebeb`. No disappearing-file traceback or
source defect was established, so the source-fix task remains incomplete.

The new regression `sealing exposes a failed or cancelled Python leg instead of
clearing its verdict` exercises both lifecycle states with all twelve receipts
present and a failed or cancelled final Python shard. Staging retains its hold;
sealing exposes the Python failure or pending-rerun verdict without marking the
test failure cosmetic. The successful-receipts control shares the same fixture
writer. All ten focused tests and all fifty scripts tests pass, with none skipped,
using `node --test --test-isolation=none`. A disposable renderer that replaces
the aggregate Python verdict with success fails the new test; repository source
was never mutated. This validates verdict preservation rather than reproducing
the unobserved fixture failure.

`git diff --check` passes. No Python or workflow files changed. Pytest and Black
are still unavailable. `git add` fails creating `.git/index.lock` on the
read-only filesystem, preventing the requested commit. Requests to post the
blocker comment and add `needs-human` to #1263 were rejected because they require
approval and this run's approval policy is `never`. PR #1263 was re-read and is
open with `draft=false` at `00e4fd9988e6e27407d8dd27cb56cbc48dcd1240`.

The full local command
`PYTEST_ADDOPTS='-m "not slow"' timeout 45s python3 -u src/verify.py --floor-may-lag`
exited 124 without a verdict. Full repository verification remains unconfirmed.
