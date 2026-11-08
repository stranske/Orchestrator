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
