# Issue 1242 closer validation

Check date: 2026-11-08.

Outcome: the retained external-caller test rejects admission without verified proof,
then passes with the production file restored. The command printing the outcome is:

```sh
/opt/anaconda3/bin/python3 -m pytest -q tests/test_capability_admission_standalone.py::test_external_caller_admission_uses_audit_proof_then_fresh_probe
```

Acceptance also requires passing required CI and revision-bound combined coverage
measurements for the parent of #1246 and #1246. Do not run the full suite or
`src/verify.py` locally; use the existing hosted measurements.

Stop rule: three repair attempts or 60 minutes from the start of this validation.
Stop at the first unresolved step, report its evidence, and ask whether to stop.
No additional coverage work, production refactor, dependency pin change, or merge
is in scope.

## Branch and retained work

The provisioned `closer-r1/issue-1242` started clean at current main
`641f88a59d4a16e4aff7b5a209af65333483dfbf`. `git branch --list
'closer*/issue-1242'` returned only that branch, and `git log
origin/main..HEAD` was empty. There were no local-only repair commits to merge,
keep, or discard. #1246's test remains unchanged; no part of #1246 is redone or
reverted. No production code, dependency pin, workflow, or floor is changed.

## Observed mutation and restoration

Interpreter selection: no repository `.venv` or `venv` exists; use the owner-selected
`/opt/anaconda3/bin/python3`. Initial exact-node run: exit 0, `1 passed in 0.65s`.

The temporary mutation in `req_caller_exists` replaced only
`return bool(proof.get("exists")), proof.get("detail", "external caller not verified")`
with `return True, proof.get("detail", "external caller not verified")`.
Thus negative/unverified external proof admitted. The fresh-negative assertion
caught that behavior. The original bytes were restored in `finally`, checked
for byte equality, then the exact node was rerun. `git diff --
src/capability_admission.py` was empty. Both executions used the exact command
above, with a 110-second subprocess timeout; neither timed out.

Mutation output:

```text
F                                                                        [100%]
=================================== FAILURES ===================================
_______ test_external_caller_admission_uses_audit_proof_then_fresh_probe _______

monkeypatch = <_pytest.monkeypatch.MonkeyPatch object at 0x113632660>

    def test_external_caller_admission_uses_audit_proof_then_fresh_probe(monkeypatch):
        """External callers must be admitted only from a verified proof, never a declaration."""
        import capability_activation_audit as audit
    
        cap = {"capability_id": "capability:external", "external_caller": {"kind": "controller"}}
    
        # A current audit row is the authoritative result and avoids probing an external controller.
        monkeypatch.setattr(audit, "external_caller", lambda _: pytest.fail("unexpected fresh probe"))
        cached = {"exists": True, "detail": "seen"}
        assert admission.req_caller_exists(
            cap, {"audit_rows": {cap["capability_id"]: {"external_caller": cached}}}
        ) == (True, "seen")
    
        # With no cached proof, the fresh audit result decides the verdict.
        monkeypatch.setattr(
            audit,
            "external_caller",
            lambda _: {"exists": False, "detail": "controller heartbeat stale"},
        )
>       assert admission.req_caller_exists(cap, {"audit_rows": {}}) == (
            False,
            "controller heartbeat stale",
        )
E       AssertionError: assert (True, 'contr...rtbeat stale') == (False, 'cont...rtbeat stale')
E         
E         At index 0 diff: True != False
E         Use -v to get more diff

tests/test_capability_admission_standalone.py:106: AssertionError
=========================== short test summary info ============================
FAILED tests/test_capability_admission_standalone.py::test_external_caller_admission_uses_audit_proof_then_fresh_probe
1 failed in 0.38s

EXIT_CODE=1
```

Restored output:

```text
.                                                                        [100%]
1 passed in 0.33s

EXIT_CODE=0
```

## Merge CI diagnosis and hosted reproduction

[Original CI run, merge 95255cff](https://github.com/stranske/Orchestrator/actions/runs/42501725219)
failed only `verify.py aggregate + combined coverage`. Its actual failing command
was:

```sh
python3 src/verify.py --aggregate shard-evidence --coverage 2>&1 | tee /tmp/combined.txt
```

Terminal output (exit 1):

```text
pytest: 15040 passed, 0 failed, 31/31 max (15073 collected; floor 15072 — 1 BEHIND)
PROBLEMS:
- floor is BEHIND reality: 15073 collected > floor 15072
```

The PR CI run [42499997991](https://github.com/stranske/Orchestrator/actions/runs/42499997991)
passed with the new test. PRs add `--floor-may-lag`; main enforces exact floor
equality. The added test accounts for the one-test lag. Main's existing automated
commit `1bcfc20dab41bfffd49d24d6f57b29f71bc1cdae` already reconciled the floor;
this validation must not edit it or weaken the check.

Reproduction is hosted to obey the prohibition on local full verification:
`gh run rerun 42501725219 --repo stranske/Orchestrator --failed` (dispatch exit 0).
Attempt 2 repeated the actual historical failing command against its original
shard evidence and terminated at 2026-11-08T17:54:26Z with exit 1: the same
`15073 collected; floor 15072 — 1 BEHIND`, `15040 passed, 0 failed` and sole
`floor is BEHIND reality` problem. This reproduces the reported failure without
changing code. The repair PR's terminal CI results are recorded in its body. No
local `verify.py` invocation occurred.

## Combined coverage and selection

The repository's coverage command is the six hosted
`python3 src/verify.py --pytest-shard N/6 --coverage --shard-output shard-results`
commands followed by `python3 src/verify.py --aggregate shard-evidence --coverage`.
It combines pytest and module selftests. Gate delegates this measurement to
`ci.yml` (`pr-00-gate.yml` disables coverage in reusable pytest shards).

Exact revisions:

- Parent of #1246: `76a54e8586c588ce558360f84ad533bf6143ed9c`.
- #1246 merge: `95255cff09a6ae7a67363da56df693b1904bd529`.

There was no existing CI measurement at the exact parent (it is an automated
floor-only commit). The earlier measured commit `88432f40` differs from that
parent only in `.verify-floor.json`. Its combined artifact from
[42496421701](https://github.com/stranske/Orchestrator/actions/runs/42496421701)
and the merge artifact report the same counts as the exact-parent artifact below.
To avoid substituting
this equivalent-source baseline for exact-parent evidence, the provisioned
remote branch was first published at the exact parent and the existing CI was
dispatched with `gh workflow run ci.yml --repo stranske/Orchestrator --ref
closer-r1/issue-1242` (exit 0):
[42507097838](https://github.com/stranske/Orchestrator/actions/runs/42507097838).
The dispatch records the exact parent SHA. The branch then advances normally to
current main plus this document. A direct SHA dispatch first returned HTTP 422
(`No ref found`), without starting a run.

Artifact retrieval commands (exit 0):

```sh
gh run download 42507097838 --repo stranske/Orchestrator -n gate-coverage -D /tmp/issue1242-parent
gh run download 42501725219 --repo stranske/Orchestrator -n gate-coverage -D /tmp/issue1242-merged
```

For each `coverage.json`, read `files[path].summary.missing_lines` and
`missing_branches` when present. Both artifacts have
`meta.branch_coverage=false`, matching `[tool.coverage.run] branch=false`.
There are **no measured branches**, not proof of zero uncovered branches.
The exact-parent aggregate succeeded and published artifact `13627578532`.
Its measured counts and missing lines exactly match the #1246 artifact. The
comparison command below exited 0; no local tests or selftests were run by it.
Do not change coverage configuration to manufacture a branch figure.

```sh
/opt/anaconda3/bin/python3 - <<'PY'
import json
from pathlib import Path
for revision in ("parent", "merged"):
    data = json.loads(Path(f"/tmp/issue1242-{revision}/coverage.json").read_text())
    row = data["files"]["src/capability_admission.py"]
    print(revision, row["summary"]["missing_lines"],
          row["summary"]["num_statements"], data["meta"]["branch_coverage"])
PY
```

```text
parent 26 599 False
merged 26 599 False
```

| Rank | Target | Exact parent missing statements | #1246 missing statements | Reduction |
| --- | --- | ---: | ---: | ---: |
| 1 | capability_admission.py | 26 | 26 | 0 |
| 2 | capability_activation_audit.py | 4 | 4 | 0 |
| 3 | periodic_report.py | 2 | 2 | 0 |
| 4 | tick.py | 3 | 3 | 0 |
| 5 | repo_knowledge.py | 3 | 3 | 0 |
| 6 | adversarial.py | 3 | 3 | 0 |
| 7 | runtime_ac.py | 1 | 1 | 0 |
| 8 | research_subjects.py | 7 | 7 | 0 |
| 9 | keepalive_supervisor.py | 27 | 27 | 0 |
| 10 | evidence_schema.py | 35 | 35 | 0 |
| 11 | dry_seam_audit.py | 23 | 23 | 0 |
| 12 | runner_effect_bridge.py | 15 | 15 | 0 |
| Total | | 149 | 149 | 0 |

The target contains 599 statements, 573 covered (95.6594%), with the same 26
missing statement line numbers before and after. The added external-caller
assertions exercise already-covered statements; their value is detecting
incorrect admission, as the mutation demonstrates. They do not reduce combined
missing statements. Measured reduction is 0, a **180-unit shortfall** from the
approximate goal. True statement-plus-branch reduction remains unknown because
branches were never measured. Even the original 149-unit ranked list was 31
below 180. No extra coverage is added to pursue the number.

Ranking evidence is [issue #1242](https://github.com/stranske/Orchestrator/issues/1242):
escaped defects, then churn, then uncovered mass. Rank 1 had six escaped-defect
signals and churn 14, ahead of rank 2's five/churn 11. This explains selection
of admission behavior; it does not claim the highest uncovered lines were tested.

## Specific dispositions for unaddressed ranked gaps

These are explicit deferrals to the next owner-dispatched coverage round, not
claims that the production paths are unreachable. This repair is limited to
validating #1246. Each future test needs isolated state and its own mutation proof.

| Rank | Target and evidence | Disposition |
| --- | --- | --- |
| 1 | capability_admission.py: 26 gaps; audit fallback 86, symbol miss 518, decision/record lookup 997-1013, report 1196, selftest state paths 1388-1489, CLI 1664-1666 | Retain the external-proof regression. Defer filesystem/decision lookup and CLI cases to focused temp-directory tests; populated-ledger/waiver selftest paths need synthetic ledger fixtures. Do not force selftest exception handlers for metric gain. |
| 2 | capability_activation_audit.py: 2432-2433, 2436, 2448; five defects/churn 11 | Defer home-anchored real-fleet selftest checks to a synthetic populated-fleet fixture; hosted CI lacks that capability. Line 2448 restores a preexisting fleet-root override and needs an isolated nonempty-environment case. Do not depend on the operator's home state. |
| 3 | periodic_report.py: 2998, 3119; four defects/churn 11 | Keep the expected invalid-input rejection sentinel at 2998 defensive. Defer the valid `attempts_unresolved` mining-coverage assertion at 3119 to synthetic attempts with unresolved worker models; its absent scenario is a meaningful future coverage case. |
| 4 | tick.py: 897, 1289, 1293; four defects/churn 9 | Defer nonempty prior environment restoration cases to isolated selftest-wrapper coverage; these restore dispatch lane, exploration rate and handoff overrides. No tick scheduling or dispatch behavior change. |
| 5 | repo_knowledge.py: 1994-1995, 2490; four defects/churn 8 | Defer nonempty base-branch-override iteration to synthetic configuration fixtures; retain the expected-GitHub-failure assertion at 2490 as defensive rather than covering it by making error handling wrong. |
| 6 | adversarial.py: 1529, 1724, 1794; four defects/churn 7 | Defer preexisting ORCH_STATE_DIR restoration variants in the three selftest cleanup blocks; exercise only with temporary state, not live review dispatch. |
| 7 | runtime_ac.py: 1794; four defects/churn 7 | Expected confirm-run refusal's `raise AssertionError` sentinel. Keep defensive; do not weaken confirmation or run real verification commands to execute it. |
| 8 | research_subjects.py: 1463, 1505, 1539, 1563, 1575, 1618, 1625; three defects/churn 10 | All are selftest rejection sentinels for invalid domain/round/arm/finding/implementation inputs. Keep defensive; existing successful rejection is why these assertions are not executed. |
| 9 | keepalive_supervisor.py: 27 gaps; churn 3 | Defer closure error/identity, contention, bounded-loop and Stage 2 calibration/history/capacity branches to isolated fake-GitHub and replay fixtures. Heartbeat exception handling needs an injected failing recorder. No live closure or lock manipulation in this validation. |
| 10 | evidence_schema.py: 35 gaps; churn 3 | Defer contextual-gap SQLite writes/validation, recurrence-wait/apply cases and CLI presentation to temporary database fixtures with explicit apply confirmation; leave selftest failure sentinel 707 defensive. |
| 11 | dry_seam_audit.py: 23 gaps; churn 3 | Defer default router-prior loading, missing/zero observation cells, outcome classifications, lifecycle read failure and CLI verdict to synthetic report/feedback fixtures. No live router/state inspection required. |
| 12 | runner_effect_bridge.py: 15 gaps; churn 3 | Defer conflicting aliases and non-object output rejection plus CLI success/rejection exits to temporary input/ledger fixtures; never record synthetic effects in the live ledger. |
