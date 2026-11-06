# Issue 1138 continuation acceptance

Current issue #1156 repair evidence is in the final section below. Earlier sections
are historical reports at their named commits and runner environments; their pending
statements do not describe this repair. Final-head CI is recorded in the repair PR
acceptance record after the evidence commit, avoiding a self-referential SHA claim.

Checked 2026-11-04 America/Chicago (2026-11-05 UTC).

Outcome: all 34 existing tests must fail on a targeted production break and pass after
restoration; the new module must be assigned once; updated-head CI must pass. Stop rule:
three unsuccessful attempts at one step or 60 minutes, report the first broken step.

Local real-input commands (repository-selected venv absent):

```sh
/opt/anaconda3/bin/python3 -m pytest -q tests/test_verify_sharding.py::test_shard_lists_partition_every_collected_test_exactly_once
/opt/anaconda3/bin/python3 -m pytest -q tests/test_coverage_round_1138.py
```

The first printed `1 passed in 24.23s`. The initial module run exposed three isolation
failures: cached agent runtime setup and live handoff claim reaping escaped the environment
fixture. The fixture now pins HANDOFF_DIR, disables capability heartbeats, and stubs the two
runtime/auth prelude builders in the command-construction test. The subprocess/socket guards
and both temporary state/runtime settings remain. After repair: `34 passed in 0.49s`.

The verdict test queries the actual verification completion event and asserts exact node IDs,
excluding the file-only fallback. No production changes are retained.

## Per-test deliberate-break evidence

[mutation-proof.json](mutation-proof.json) records all 34 individual node commands, exact
production function and before/broken replacement, complete failing output (exit 1), and
restored passing output (exit 0). Each mutation was applied to production source, compiled,
run against the single test node, and restored in a finally block before the success run.
All 34 mutations failed their intended tests; no selected mutation survived. The original
branch's one recorded experiment remains in PR history; this adds proofs for every node.

## Combined coverage and provenance

[base-combined.json](base-combined.json) retains the six summaries from the exact base commit
555c1f1cf0ac8a6fb3e64d4081ecf952df126021, CI run 41952625547, artifact 13382156456.
All six coverage shards succeeded. The aggregate published 141 instrumented processes;
its failure was a seven-test collection-floor lag, not failing tests. The overall run was
cancelled, so this is a measured baseline, not a claim of green base CI.

The updated PR's CI runs the same combined producer without running the full suite locally:

```sh
python3 src/verify.py --pytest-shard N/6 --coverage --shard-output shard-results
python3 src/verify.py --aggregate shard-evidence --coverage --floor-may-lag
```

CI uses Python 3.14 and VERIFY_PYTEST_WORKERS=4. Combined coverage includes pytest and
module selftests. The configuration has branch_coverage=false: missing units here are
statements, not measured branches.

Local intersection against the base artifact's missing statement lists, after the second
test pass (67 tests in `tests/test_coverage_round_1138.py`): the new tests execute 248 of
the 317 baseline-missing statements. That is the predicted CI reduction for these lines,
because the base artifact already includes the full suite plus selftests and these lines
were still missing there. It is not a substitute for the updated-head Gate artifact.

| Target | Base missing | Executed by this module | Still missing here |
|---|---:|---:|---:|
| local_verify | 10 | 10 | 0 |
| capability_activation_audit | 46 | 42 | 4 |
| exp_abcd | 9 | 0 | 9 |
| periodic_report | 69 | 63 | 6 |
| tick | 23 | 20 | 3 |
| repo_knowledge | 160 | 113 | 47 |
| total | 317 | 248 | 69 |

Predicted shortfall from 180: 0 (248 >= 180). Official head deltas stay pending until the
updated-head coverage artifact is published. The 69 statements this module still does not
execute are selftest-only branches (`exp_abcd._selftest_checks` and its nested fakes,
`capability_activation_audit._selftest`, `periodic_report._selftest`, `tick._selftest`),
`periodic_report.build_report` corrupt-artifact handlers, and `repo_knowledge` git-remote,
suggestion-import, and feedback-memory paths.

Target order is inherited from issue #1138's Orchestrator ranking (escaped-defect priority,
then churn, then uncovered mass), not independently reranked or chosen by file size. The
base artifact exactly reproduces its six advertised gaps: 10, 46, 9, 69, 23, 160 (317 total).
The source ranking scores and escaped-defect/churn inputs were not supplied in the issue;
this PR does not claim to have recomputed them.

The 69 statements still missing from this module are not claimed to be dead. `local_verify`'s
ten baseline statements are all executed here. Floors, skip ceilings, dependency pins and
production behavior are unchanged.

Post-push review strengthened the caller fallback to prove a positive exact caller and a
negative nonmatching function, and the malformed metadata test to require empty arm and
missing-outcome lists. Both changed tests received fresh targeted mutation proofs.

The second pass adds 33 tests. [mutation-proof-pass2.json](mutation-proof-pass2.json)
records each one's broken exit 1 and restored exit 0. One first mutation of the snapshot
CLI test survived because argparse usage still printed `--from-gap`; the assertion now
requires the exact `parser.error` sentence, and that mutation was repeated and killed.

## Third pass

Ten more tests cover the production paths the second pass still missed:
`periodic_report.build_report` corrupt pattern-miner artifacts, and `repo_knowledge`
fuzzy playbook duplicates, git-remote parsing, doc and review suggestion filters,
PR-comment payload failures, snapshot skips, tokenless memory scores, and feedback-memory
query failures. Each failed under a real production replacement and passed after
restoration ([mutation-proof-pass3.json](mutation-proof-pass3.json)). No production
change remains.

Local intersection against the same base artifact, 77 tests in this module:

| Target | Base missing | Executed by this module | Still missing here |
|---|---:|---:|---:|
| local_verify | 10 | 10 | 0 |
| capability_activation_audit | 46 | 42 | 4 |
| exp_abcd | 9 | 0 | 9 |
| periodic_report | 69 | 67 | 2 |
| tick | 23 | 20 | 3 |
| repo_knowledge | 160 | 157 | 3 |
| total | 317 | 296 | 21 |

Predicted shortfall from 180: 0 (296 >= 180). This is the same intersection method that
matched the `88df4821` artifact's 88/88 gain.

## Measured head `ea6255b`

[CI run 41975530292](https://github.com/stranske/Orchestrator/actions/runs/41975530292)
published [coverage artifact 13391989393](https://github.com/stranske/Orchestrator/actions/runs/41975530292/artifacts/13391989393)
for `ea6255b9c477f9abe31c81d3ef6ec48b3e023bb0`. Compared with exact-base artifact
13382156456, the six ranked targets dropped from 317 missing statements to 21.
Measured gain: **296**. Shortfall from 180: **0**. The prediction matched the artifact
on every target. Branch coverage is disabled, so this is statements only.

Repo-wide missing lines fell by 289 (85.725677% to 85.974024%). The pull-request job
measures the merge with main, where `provision.py` gained 49 statements and 8 missing
lines, `switch_review.py` lost one covered line, and `capacity.py` gained two:
296 - 8 - 1 + 2 = 289.

The recorded line texts and the arithmetic are checked by
`test_published_head_measurement_matches_ranked_source_lines`.
[head-ea6255b-combined.json](head-ea6255b-combined.json) is that record.

The 21 statements this module does not execute are inside `_selftest` bodies, and the
combined baseline already runs those selftests without reaching them:

- `exp_abcd.py` 3158, 3170, 3179, 3301, 3473, 3548, 3618, 3772, 3774: environment-restore
  `else` branches taken only when the variable was already set, nested fake-completion
  and fake-`Popen.kill` helpers, and assertion-failure raises.
- `capability_activation_audit.py` 2432-2433, 2436, 2448: the live-ledger
  `docs-drift-fix-agent` check and the `ORCH_FLEET_ROOT`-already-set restore. CI
  bootstraps an empty ledger and does not set that variable.
- `periodic_report.py` 2998, 3119: the selftest's own `AssertionError` and
  verdict-consistency assert, taken only when the selftest's expectation fails.
- `tick.py` 897, 1289, 1293: restores of `ORCH_DISPATCH_LANE`, `ORCH_EXPLORATION_RATE`,
  and `HANDOFF_DIR` when those variables were already set. The unset branches are the
  ones the selftest runs.
- `repo_knowledge.py` 1994-1995: loop body over `provision.BASE_BRANCH_OVERRIDES`, which
  is an empty dict, so the body cannot run. Line 2490 is the selftest's
  `AssertionError` when a stubbed `gh` failure is not surfaced; the production
  `RuntimeError` is covered by `test_suggest_from_pr_comments_reports_bad_targets_and_payloads`.

Running those `_selftest` functions from pytest would re-enter non-hermetic setup
(real ledger lookups, nested subprocess fakes, and assertion failures of the selftest
itself). They are not a separate production behavior to score.

## Remeasured head `ecb0707`

[CI run 41979128986](https://github.com/stranske/Orchestrator/actions/runs/41979128986)
published [coverage artifact 13393447683](https://github.com/stranske/Orchestrator/actions/runs/41979128986/artifacts/13393447683)
for `ecb0707b0a8acf7cd5dbd8a1de122829ea2e39ac`. That commit does not change the six
ranked modules. Compared with artifact 13391989393, every ranked missing line is the
same: 21 statements, gain 296, shortfall from 180 is 0.

Repo-wide totals moved because the pull-request job merges main: `pacing_usage.py`
gained 200 statements, `pacing.py` gained 10, and `closer_native_lease.py` covered one
line the prior artifact missed. Missing lines 16717 → 16716. Statements 119186 →
119396. Percent covered 85.974024% → 85.999531%.
[head-ecb0707-combined.json](head-ecb0707-combined.json) is that record.
`test_current_head_remeasurement_keeps_ranked_gaps_inside_selftests` checks the line
lists, the source text, that each remaining line is inside a `_selftest` function, and
that the repo-wide delta is exactly those three files.

## Issue 1156 correction: verification blocked (2026-11-05 UTC)

The working-tree correction on base `d5ab5d280d3f1101e969951038d12338be026405`
carries forward PR #1151's test diff: it removes the mandatory catalog-probe traffic
assertions, traffic bookkeeping, and unused `adapters` import/stub. The malformed
status/inventory inputs, isolated runtime, subprocess stub, and four pattern-miner
result assertions remain. No production source changes are retained.

This runner cannot execute the required verification:

| Command | Observed result |
|---|---|
| `/opt/anaconda3/bin/python3 -m pytest -q tests/test_coverage_round_1138.py -m 'not slow'` | Exit 127: interpreter does not exist |
| `/opt/anaconda3/bin/python3 -m pytest -q tests/test_verify_sharding.py::test_shard_lists_partition_every_collected_test_exactly_once -m 'not slow'` | Exit 127: interpreter does not exist |
| `python3 -m pytest -q tests/test_coverage_round_1138.py -m 'not slow'` | Exit 1: no module named `pytest` |
| `python3 -m pytest -q tests/test_verify_sharding.py::test_shard_lists_partition_every_collected_test_exactly_once -m 'not slow'` | Exit 1: no module named `pytest` |
| `black --line-length 100 tests/test_coverage_round_1138.py` | Exit 127: command not found |
| Repository-wide Black check below | Exit 127: command not found |

```sh
black --check --line-length 100 --exclude '(\.workflows-lib|node_modules)' .
```

Available `python3` is `/opt/hostedtoolcache/Python/3.14.9/x64/bin/python3`.
Installing pytest, pytest-cov, and Black into `/tmp/issue-1156-venv` failed because
`pypi.org` could not resolve; no cached pytest or Black installation was found.
Syntax compilation and `git diff --check` pass, but neither substitutes for pytest
or Black. The mandatory pre-commit Black gate therefore prevents committing this
correction in this run.

Attempts to add `needs-human` and a blocker comment to PR #1160 were refused by
the GitHub tool: "MCP tool call requires approval, but approval policy is never."
The blocker is recorded locally here; neither remote update was applied.

No task checkbox is completed. Fresh production-mutation and artifact-lock
deliberate-break experiments, exact-head pytest results, final-head CI shard and
aggregate results, and review/merge gates remain pending. Historical evidence above
and in `mutation-proof-pass3.json` is unchanged and does not verify this correction.

## Repeatable corrected-test mutation runner (2026-11-05 UTC)

`scripts/verify_issue_1138_mutation.js` now runs a passing baseline, changes only the
`pattern_status = {}` exception fallback to `pattern_status = {"broken": True}`,
requires the corrected test to fail at that assertion, restores the original source
bytes in a `finally` block, and requires a restored pass. Successful runs append
`followup_runs` to `mutation-proof-pass3.json`, preserving the historical cases and
recording the measured Git head, source/test SHA-256 hashes, commands, and output.
Source and test changes must be committed before recording exact-head evidence.

On a runner with pytest installed, repeat the experiment with:

```sh
PYTHON=/opt/anaconda3/bin/python3 node scripts/verify_issue_1138_mutation.js
```

Local runner validation: `node tests/test_issue_1138_mutation.js` reports **12 passed**.
These are runner unit tests with simulated pytest results, not production mutation
evidence. The existing helper tests also pass:
`node tests/test_redirect_routing_mutations.js` reports **11 passed**.
JavaScript syntax checks and `git diff --check` pass. No Python files were changed.

The real runner invocation exits 1 before mutation: `spawnSync python3 EPERM`, with
`No module named pytest` also present in stderr. A byte comparison verifies that
`src/periodic_report.py`, `tests/test_coverage_round_1138.py`, and
`mutation-proof-pass3.json` remain unchanged. Both requested Anaconda commands still
exit 127 because that interpreter is absent. The targeted coverage command
`python3 -m pytest tests/test_coverage_round_1138.py --cov=src/periodic_report --cov-report=term-missing -m 'not slow'`
exits 1 because pytest is absent; no coverage result is claimed. Black is still
unavailable. No mutation task or acceptance checkbox is completed by this run.

### CI observed before the runner changes

For exact head `842cfc22d4c2ad8d0688af5f221208045e93e91f`,
[CI run 42032098796](https://github.com/stranske/Orchestrator/actions/runs/42032098796)
succeeded. All six `pytest + coverage shard N/6` jobs and all six flat exec-mirror
shards succeeded, as did `verify.py aggregate + combined coverage` (job 127265506533)
and `verify.py aggregate in the flat exec-mirror shape` (job 127265628703).
[Gate run 42032100242](https://github.com/stranske/Orchestrator/actions/runs/42032100242)
also succeeded, including all six shards on Python 3.13 and 3.14 and its summary job.
These are results for the prior head, not final-head evidence for the new runner.
PR #1160 was confirmed open with `draft=false`. Fresh corrected-test mutation and
artifact-lock experiments, final-head CI, and the guarded review/merge remain pending.

Delivery is blocked as well: staging the three scoped runner/test/documentation files
fails because `.git/index.lock` cannot be created on the read-only filesystem. A
GitHub connector blob-write attempt was refused: "MCP tool call requires approval,
but approval policy is never." Attempts to apply `needs-human` and post the blocker
comment to PR #1160 were refused for the same reason. No commit, remote code update,
label, or comment was created. Continue in a writable Git checkout with a
pytest-capable interpreter, commit the runner, and execute the command above before
claiming fresh mutation evidence.

## Issue 1156 closer repair: real acceptance (2026-11-05 UTC)

Outcome: all four remaining acceptance criteria evidenced. Command pair below plus
`PYTHON=/opt/anaconda3/bin/python3 node scripts/verify_issue_1138_mutation.js`,
the two artifact-lock experiments, and final-head CI in the repair PR print the
outcome. Stop rule: two attempts per failing experiment or 30 minutes total;
report the first broken command and its output if reached.

Selected interpreter: `/opt/anaconda3/bin/python3` (Python 3.12.2, pytest 9.1.1).
No virtual environment is explicitly selected in this checkout. The earlier
acceptance interpreter was a disposable `.venv/bin/python` (Python 3.14.3), whose
checkout has already been removed; it is not reused or silently substituted.

The complete prior acceptance transcript cannot be recovered: the deployed
`closer_core.run_evidence` returns only `str(gate)[:1500]`, and
`provision.temporary_exact_head_worktree` removes the checkout in `finally`.
The surviving target record ends during AC1-TEST1's passing output, hiding the
actual failing check. Its overall FAIL remains unattributed. Fresh complete
results below replace that uncertainty for these required commands; they do not
retroactively explain the old FAIL.

The first current coverage-module run at `321f572a4bc3cf733cd6c9ee97d66d684886a14f`
printed the isolation failure below. The only necessary correction passes a
capabilities path under the test's temporary runtime to `build_report`, so the
corrupt-artifact test no longer attempts the live capabilities ledger lock.
PR #1160's correction and runner are retained. No production correction was needed.

<details>
<summary>Initial failing command and complete output (exit 1)</summary>

```sh
/opt/anaconda3/bin/python3 -m pytest -q tests/test_coverage_round_1138.py
```

```text
...................................................................F.... [ 91%]
.......                                                                  [100%]
=================================== FAILURES ===================================
_________ test_build_report_treats_corrupt_pattern_artifacts_as_empty __________

monkeypatch = <_pytest.monkeypatch.MonkeyPatch object at 0x112480f20>
isolated_runtime = PosixPath('/Users/teacher/.codex/orchestrator/agent-runtime/codex/tmp/pytest-of-teacher/pytest-1036/test_build_report_treats_corru0')

    def test_build_report_treats_corrupt_pattern_artifacts_as_empty(monkeypatch, isolated_runtime):
        state = isolated_runtime / "state"
        state.mkdir()
        (state / "pattern-miner-status.json").write_text("{not-json")
        (state / "pattern-miner-inventory.json").write_text("{also-not")

        def runner(argv, *args, **kwargs):
            return subprocess.CompletedProcess(argv, 127, "", "not started")

        monkeypatch.setattr(subprocess, "run", runner)
>       report = periodic_report.build_report(window_days=1, probe_langsmith_artifacts=False)
                 ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

tests/test_coverage_round_1138.py:1674:
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _
src/periodic_report.py:1795: in build_report
    "capabilities": capabilities.summary(capabilities_path or capabilities.REG, create=False),
                    ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
src/capabilities.py:2900: in summary
    capabilities = load(path, create=True) if create else load_declared(path)
                                                          ^^^^^^^^^^^^^^^^^^^
src/capabilities.py:2423: in load_declared
    capabilities = load(path, create=False)
                   ^^^^^^^^^^^^^^^^^^^^^^^^
src/capabilities.py:2343: in load
    with _locked(path, shared=True):
/opt/anaconda3/lib/python3.12/contextlib.py:137: in __enter__
    return next(self.gen)
           ^^^^^^^^^^^^^^
src/capabilities.py:1305: in _locked
    with lock_path.open("a+") as handle:
         ^^^^^^^^^^^^^^^^^^^^
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _

self = PosixPath('/Users/teacher/.codex/orchestrator/capabilities.json.lock')
mode = 'a+', buffering = -1, encoding = 'locale', errors = None, newline = None

    def open(self, mode='r', buffering=-1, encoding=None,
             errors=None, newline=None):
        """
        Open the file pointed by this path and return a file object, as
        the built-in open() function does.
        """
        if "b" not in mode:
            encoding = io.text_encoding(encoding)
>       return io.open(self, mode, buffering, encoding, errors, newline)
               ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
E       PermissionError: [Errno 1] Operation not permitted: '/Users/teacher/.codex/orchestrator/capabilities.json.lock'

/opt/anaconda3/lib/python3.12/pathlib.py:1013: PermissionError
=========================== short test summary info ============================
FAILED tests/test_coverage_round_1138.py::test_build_report_treats_corrupt_pattern_artifacts_as_empty
1 failed, 78 passed in 3.98s

```
</details>

### Required targets at corrected source/test commit

Measured Git SHA: `d068c59fff0529d915211560c286f9bf621237cf`. These runs occurred after committing the
correction and before the documentation-only evidence commit. They are evidence
for this measured SHA, not the later PR head.

```text
$ /opt/anaconda3/bin/python3 -m pytest -q tests/test_coverage_round_1138.py
........................................................................ [ 91%]
.......                                                                  [100%]
79 passed in 2.81s
exit 0

$ /opt/anaconda3/bin/python3 -m pytest -q tests/test_verify_sharding.py::test_shard_lists_partition_every_collected_test_exactly_once
.                                                                        [100%]
1 passed in 10.98s
exit 0
```

The pre-commit retry also passed: `79 passed in 2.73s`; the first sharding run
on the original main commit passed: `1 passed in 28.09s`.

### Corrected corrupt-artifact production mutation

```text
$ PYTHON=/opt/anaconda3/bin/python3 node scripts/verify_issue_1138_mutation.js
tests/test_coverage_round_1138.py::test_build_report_treats_corrupt_pattern_artifacts_as_empty: baseline passed; mutation failed; restored passed; evidence appended
exit 0
```

The appended real `followup_runs` entry in
[mutation-proof-pass3.json](mutation-proof-pass3.json) records measured SHA
`d068c59fff0529d915211560c286f9bf621237cf`, the exact single-test command and full
baseline, mutation and restored outputs. Historical cases are unchanged.
The mutation changes only `pattern_status = {}` to
`pattern_status = {"broken": True}`. It fails at `assert miner["status"] == {}`
with `AssertionError: assert {'broken': True} == {}`. Baseline and restored runs
both exit 0; the intended mutation exits 1.

Production source SHA-256 before / after restoration: `8f84bd0fce5ec046d68bbae9ca2ae2c9c4aec04e6ac728e9c312e4e04d315470` / `8f84bd0fce5ec046d68bbae9ca2ae2c9c4aec04e6ac728e9c312e4e04d315470`.
Test SHA-256 before / after: `f7cd686974888ac8ddb8e312e7913f608fdb9425784812d00949c50c47a96161` / `f7cd686974888ac8ddb8e312e7913f608fdb9425784812d00949c50c47a96161`.

### Independent artifact-lock corruptions

Disposable writable checkout: `/private/tmp/orchestrator-1156-artifact-proof`,
a shared clone checked out detached at the measured SHA above. Each experiment
runs its named test before mutation, changes exactly the identified field,
requires exit 1 at its corresponding assertion, restores original artifact bytes
in `finally`, compares bytes, then requires the same test to exit 0.
The two changes are separate: neither corrupted artifact is present during the
other experiment. JSON indentation may change temporarily; restored bytes match
exactly. No coverage-record mutations are retained.

The artifact-lock fields exercised are `head-ea6255b-combined.json:gain`, asserted
by `assert gain == record["gain"] == 296`, and
`head-ecb0707-combined.json:gain_vs_base`, asserted by
`assert record["gain_vs_base"] == prior["gain"] == 296`. These experiments demonstrate
those two gain locks; they do not claim a mutation of every other assertion in
the tests (provenance, missing-line lists, source text, and repo-wide arithmetic).

#### `test_published_head_measurement_matches_ranked_source_lines`

Record: `docs/verification/issue-1138/head-ea6255b-combined.json`; field `gain`: 296 → 297.

```sh
/opt/anaconda3/bin/python3 -m pytest -q tests/test_coverage_round_1138.py::test_published_head_measurement_matches_ranked_source_lines
```

baseline (exit 0):

```text
.                                                                        [100%]
1 passed in 0.97s
```

red (exit 1):

```text
F                                                                        [100%]
=================================== FAILURES ===================================
_________ test_published_head_measurement_matches_ranked_source_lines __________

    def test_published_head_measurement_matches_ranked_source_lines():
        """The ea6255b combined artifact's six-target gain is 296, and the 21 lines it still misses are these source lines."""
        record_path = (
            paths.REPO_ROOT / "docs" / "verification" / "issue-1138" / "head-ea6255b-combined.json"
        )
        record = json.loads(record_path.read_text())
        assert record["measured_head_sha"] == "ea6255b9c477f9abe31c81d3ef6ec48b3e023bb0"
        assert record["base_sha"] == "555c1f1cf0ac8a6fb3e64d4081ecf952df126021"
        assert record["run_id"] == 41975530292
        assert record["artifact_id"] == 13391989393
        assert record["branch_coverage"] is False

        order = list(_MEASURED_MODULES)
        assert list(record["targets"]) == order
        gain = 0
        still = 0
        base_missing = 0
        for name, module in _MEASURED_MODULES.items():
            row = record["targets"][name]
            assert row["base_missing"] - row["head_missing"] == row["gain"]
            assert len(row["still_missing"]) == row["head_missing"]
            gain += row["gain"]
            still += row["head_missing"]
            base_missing += row["base_missing"]
            source = Path(module.__file__).read_text().splitlines()
            for item in row["still_missing"]:
                assert source[item["line"] - 1] == item["text"]
        assert base_missing == record["base_missing"] == 317
        assert still == record["head_missing"] == 21
>       assert gain == record["gain"] == 296
E       assert 296 == 297

tests/test_coverage_round_1138.py:1975: AssertionError
=========================== short test summary info ============================
FAILED tests/test_coverage_round_1138.py::test_published_head_measurement_matches_ranked_source_lines
1 failed in 0.35s
```

green (exit 0):

```text
.                                                                        [100%]
1 passed in 0.28s
```

SHA-256 before: `1afe14f1040e2ec219737fb261686def8e2ad3da62c3c672ba0f3da80a0cb469`.

SHA-256 corrupted: `9314c9fa5b82680e636605b819a5f08c85ced4f31ab6d0b9c244fb9d276e29a8`.

SHA-256 restored: `1afe14f1040e2ec219737fb261686def8e2ad3da62c3c672ba0f3da80a0cb469`.

#### `test_current_head_remeasurement_keeps_ranked_gaps_inside_selftests`

Record: `docs/verification/issue-1138/head-ecb0707-combined.json`; field `gain_vs_base`: 296 → 297.

```sh
/opt/anaconda3/bin/python3 -m pytest -q tests/test_coverage_round_1138.py::test_current_head_remeasurement_keeps_ranked_gaps_inside_selftests
```

baseline (exit 0):

```text
.                                                                        [100%]
1 passed in 0.62s
```

red (exit 1):

```text
F                                                                        [100%]
=================================== FAILURES ===================================
______ test_current_head_remeasurement_keeps_ranked_gaps_inside_selftests ______

    def test_current_head_remeasurement_keeps_ranked_gaps_inside_selftests():
        """ecb0707's gate artifact keeps the six-target gain at 296.

        The 21 statements still missing on that artifact are the same source lines as
        ea6255b, each inside a selftest function. The repo-wide shift is the three
        files main moved under the pull request, not the ranked targets.
        """
        directory = paths.REPO_ROOT / "docs" / "verification" / "issue-1138"
        record = json.loads((directory / "head-ecb0707-combined.json").read_text())
        prior = json.loads((directory / "head-ea6255b-combined.json").read_text())
        assert record["measured_head_sha"] == "ecb0707b0a8acf7cd5dbd8a1de122829ea2e39ac"
        assert record["prior_measured_sha"] == prior["measured_head_sha"]
        assert record["run_id"] == 41979128986
        assert record["artifact_id"] == 13393447683
        assert record["prior_artifact_id"] == prior["artifact_id"] == 13391989393
        assert record["branch_coverage"] is False
>       assert record["gain_vs_base"] == prior["gain"] == 296
E       assert 297 == 296

tests/test_coverage_round_1138.py:2014: AssertionError
=========================== short test summary info ============================
FAILED tests/test_coverage_round_1138.py::test_current_head_remeasurement_keeps_ranked_gaps_inside_selftests
1 failed in 0.32s
```

green (exit 0):

```text
.                                                                        [100%]
1 passed in 0.69s
```

SHA-256 before: `93d0e827be0c8184aa2662033becb77b90e959bf634f77d11a61f10f7ae73180`.

SHA-256 corrupted: `1fb4d66be14ee3d3e198755adcb7f92b77dd139c774a233829494843af4670fb`.

SHA-256 restored: `93d0e827be0c8184aa2662033becb77b90e959bf634f77d11a61f10f7ae73180`.

### Restoration and delivery boundary

`git diff --exit-code -- src` passed in the repair checkout after the production
mutation. `git diff --name-only` was empty in the disposable checkout after both
artifact experiments. The final repair diff changes only this README, the appended
mutation evidence, and the demonstrated capabilities-path correction in the test.
Dependency pins, production source, and both coverage records are unchanged.

Only the provisioned `closer-r1/issue-1156` matched `git branch --list
'closer*/issue-1156'`, and it initially equalled current main. There were no missing
local-branch commits to merge, keep, or discard. The historical #1160 PR-body
completion claims for evidence were unsupported; this repair's checklist records
only observed evidence. Historical reports above remain intact.

Final-head pytest shards and aggregate results belong in the repair PR acceptance
record, keyed to its exact commit SHA and workflow/job URLs. They are pending at
the time this evidence is committed; the PR record must be updated from real CI
before the fourth criterion can be claimed complete.
