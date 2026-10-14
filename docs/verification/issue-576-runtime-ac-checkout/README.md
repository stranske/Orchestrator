# Issue 576: exact-head execution through production ingest

Dedup: runtime-AC authoring, execution, its Brain events and weekly reader already exist.
The panel also already provisions exact-head detached worktrees. This change shares that
provisioner with keepalive ingest; it adds no capability, role, outcome store or merge gate.
The existing runtime-ac-checks declaration retains this finding in its notes.

`test_ingest_executes_shadow_spec_on_exact_checkout` drives the real ingest and event writer
with a temporary Git repository, an injected checkout provider and a stub verifier. It observes
an executed, non-blocking event with the requested SHA and PASS verdict. Repeated ingest of
an existing run/spec also executes without reauthoring or modifying outcomes.

`test_ingest_runs_real_named_test_and_removes_its_checkout` goes further: only the local
canonical-repository lookup is redirected to a temporary Git repo. The shared provisioner
fetches its exact commit, creates a private detached tree, the real runtime verifier executes
its committed named pytest test, and the event retains PASS and a passing-test audit. The tree
and its Git registration are absent afterward. This is a local integration test, not production
usage or a deployed-mirror claim.

The follow-up fixture uses a separate local remote and clones the canonical repository before
creating the PR commit. It asserts that the commit is initially absent, checks `FETCH_HEAD`
after ingest, and requires the canonical HEAD and complete worktree registration list to stay
unchanged. Its named test reads a file delivered only in the PR commit, and the event audit must
identify that exact test with PASS, exit zero and `passing_named_test=true`.

Follow-up validation on 2026-10-14: all four touched-module selftests, Python syntax and
whitespace checks pass. A separate direct exercise of the real provisioner also passes local
fetch, detached checkout, committed contents and removal of a dirty checkout and its Git
registration. The strengthened pytest integration has **not** run: pytest, Black, Ruff and mypy
are absent, and package installation failed in the restricted environment. The mandatory Black
gate blocks committing the Python edit. The logs and manifest below describe the earlier
committed fixture; they are not evidence for this follow-up. A full `verify.py` attempt was
launched with output in `/tmp/runtime-ac-full-verify.log`; no full-suite PASS is claimed.

The other regressions prove the three-attempt bound across repositories, failed-provisioning
accounting and per-target reasons, disabled/dry-run behavior, panel clean reuse and dirty-tree
refusal, invalid/missing commits, cleanup after a verifier exception, and no outcome writes.
Command execution keeps its independent opt-in. Budget exhaustion is `planned` with
`shadow_execution_budget`; provisioning failure is `planned` with `shadow_checkout_failed`.
Checkout and cleanup failures increment `runtime_ac_checkout_failures` and retain reasons in
`runtime_ac_checkout_errors`; verifier exceptions stay in `runtime_ac_shadow_errors`.

Checkout paths and trailing log whitespace are normalized; assertions and exit codes are unchanged.

Validation retained here:

- `related-tests.log`: 190 passed plus six subtests across 12 related suites, no skips.
- `mypy.log`: four changed production modules passed focused mypy.
- `collection.log`: 3303 tests collected, eight above merged #577 base3295. All skip/selftest/gate/mypy ceilings
  are unchanged; the floor is collection evidence, not a whole-suite PASS claim.
- `manifest.json`: final production/test hashes and original/corrected/broken/restored exit codes.
- `original-red.log`: the original production ingest with the final regression fails (planned,
  zero executed events).
- `broken-red.log`: restore only `worktree=pr.get("_worktree")` at the unique production call site;
  the named regression fails. `corrected-green.log` and `restored-green.log` pass. Source bytes
  are restored exactly even if the replay fails.

Replay from an isolated development checkout, with a **new** output directory:

```sh
python3 docs/verification/issue-576-runtime-ac-checkout/replay.py --out /tmp/issue-576-proof-new
```

Hosted `verify.py` on the final PR head remains the full clean-machine verdict. This PR does
not install code into the execution mirror. After the normal manual mirror sync, the real
keepalive ingest and `switch_review.py` must supply the issue's live `executed > 0` evidence;
that post-sync acceptance remains unverified here. Shadow execution cannot block merges,
write a delivery outcome, or substitute for that production observation.
