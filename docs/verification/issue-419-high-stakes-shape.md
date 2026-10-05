# Issue419 current-main recovery evidence

Integrated current main ea92f548 (including #418 withholding and #420 fixture provenance). Three valid review findings are repaired: mature advisor probes retain pure title security matching; truncated positive facts make the weekly population partial; shared GraphQL fact production includes source-issue labels and completeness. Legacy caches are refreshed within the existing fetch budget and remain unmeasured until refreshed. Shadow-only behavior, weekly maturity, flags and skip/type ceilings are preserved.

Current-main focused suite: 52 passed. Final full verifier: 2,105 passed, one named prerequisite skip (2,106 collected); all 100 selftests ran; four of five gates green with the value-chain-monitor ledger prerequisite named for the fifth; mypy checked all117 modules with zero exemptions. Black, Ruff and diff checks pass. Source/private-state evidence is not live dispatcher publication. The original large floor notes are retained in Git history and the round evidence; the floor note is now compact to avoid truncating acceptance packets.

Actual private-copy deliberate controls:

- workflow-return-none: baseline0, deliberate mutation1, restored0; byte-identical restoration.

```text
BASELINE
.                                                                        [100%]
tracked test inputs: NOT CHECKED -- /private/var/folders/qm/w0dtc0j132gd6ymf1hxtd2p00000gp/T/closer419-controls-niy5uydv is not a git checkout. In the exec mirror that loses nothing: its tests/ IS `git archive HEAD tests`, so every file in it was tracked
1 passed in 0.26s

BROKEN
F                                                                        [100%]
=================================== FAILURES ===================================
___ test_workflow_and_auth_path_changes_are_high_stakes_and_docs_only_is_not ___

    def test_workflow_and_auth_path_changes_are_high_stakes_and_docs_only_is_not():
        for path in (
            ".github/workflows/gate.yml",
            ".github/scripts/guard.js",
            "src/auth/session.py",
            "src/auth_tokens.py",
            "src/Authentication/session.py",
            "src/authorization/policy.py",
            "src/security/check.py",
            "src/db/store.py",
            "src/data/load.py",
            "src/database/connect.py",
            "src/migrations/upgrade.py",
            "src/persistence/save.py",
            "src/storage/write.py",
            "src/schema/validate.py",
        ):
>           assert adv.high_stakes_from_shape(facts([path])), path
E           AssertionError: .github/workflows/gate.yml
E           assert None
E            +  where None = <function high_stakes_from_shape at 0x107487f60>({'paths': ['.github/workflows/gate.yml'], 'files_total': 1, 'changedFiles': 1, 'additions': 1, ...})
E            +    where <function high_stakes_from_shape at 0x107487f60> = adv.high_stakes_from_shape
E            +    and   {'paths': ['.github/workflows/gate.yml'], 'files_total': 1, 'changedFiles': 1, 'additions': 1, ...} = facts(['.github/workflows/gate.yml'])

tests/test_adversarial_high_stakes_shape.py:50: AssertionError
tracked test inputs: NOT CHECKED -- /private/var/folders/qm/w0dtc0j132gd6ymf1hxtd2p00000gp/T/closer419-controls-niy5uydv is not a git checkout. In the exec mirror that loses nothing: its tests/ IS `git archive HEAD tests`, so every file in it was tracked
=========================== short test summary info ============================
FAILED tests/test_adversarial_high_stakes_shape.py::test_workflow_and_auth_path_changes_are_high_stakes_and_docs_only_is_not
1 failed in 0.27s

RESTORED
.                                                                        [100%]
tracked test inputs: NOT CHECKED -- /private/var/folders/qm/w0dtc0j132gd6ymf1hxtd2p00000gp/T/closer419-controls-niy5uydv is not a git checkout. In the exec mirror that loses nothing: its tests/ IS `git archive HEAD tests`, so every file in it was tracked
1 passed in 0.49s
```

- zero-as-unmeasured: baseline0, deliberate mutation1, restored0; byte-identical restoration.

```text
BASELINE
.                                                                        [100%]
tracked test inputs: NOT CHECKED -- /private/var/folders/qm/w0dtc0j132gd6ymf1hxtd2p00000gp/T/closer419-controls-niy5uydv is not a git checkout. In the exec mirror that loses nothing: its tests/ IS `git archive HEAD tests`, so every file in it was tracked
1 passed in 0.25s

BROKEN
F                                                                        [100%]
=================================== FAILURES ===================================
_____ test_weekly_line_counts_the_live_population_and_prints_zero_as_zero ______

tmp_path = PosixPath('/private/var/folders/qm/w0dtc0j132gd6ymf1hxtd2p00000gp/T/pytest-of-teacher/pytest-7653/test_weekly_line_counts_the_li0')

    def test_weekly_line_counts_the_live_population_and_prints_zero_as_zero(tmp_path):
        now = 1_790_000_000
        data = population(
            [facts([".github/workflows/gate.yml"]), facts(["src/auth.py"]), facts(["docs/readme.md"])],
            now=now,
        )
        (tmp_path / "fleet-shapes.json").write_text(json.dumps(data))
        section = switch_review.adversarial_shape_population(state_dir=tmp_path, now=now)
        assert section["shape_candidates"] == 2 and section["population"] == 3
        assert switch_review.adversarial_shape_line(section) == (
            "adversarial-review: high-stakes candidates 2 of 3 merged PRs (shape rule), 0 by label"
        )
        zero = population([facts(["docs/readme.md"])], now=now)["adversarial_shape"]
>       assert switch_review.adversarial_shape_line(zero) == (
            "adversarial-review: high-stakes candidates 0 of 1 merged PRs (shape rule), 0 by label"
        )
E       AssertionError: assert 'adversarial-...g population)' == 'adversarial-...), 0 by label'
E         
E         Skipping 33 identical leading characters in diff, use -v to show
E         - andidates 0 of 1 merged PRs (shape rule), 0 by label
E         + andidates unmeasured (missing population)

tests/test_adversarial_high_stakes_shape.py:104: AssertionError
tracked test inputs: NOT CHECKED -- /private/var/folders/qm/w0dtc0j132gd6ymf1hxtd2p00000gp/T/closer419-controls-niy5uydv is not a git checkout. In the exec mirror that loses nothing: its tests/ IS `git archive HEAD tests`, so every file in it was tracked
=========================== short test summary info ============================
FAILED tests/test_adversarial_high_stakes_shape.py::test_weekly_line_counts_the_live_population_and_prints_zero_as_zero
1 failed in 2.53s

RESTORED
.                                                                        [100%]
tracked test inputs: NOT CHECKED -- /private/var/folders/qm/w0dtc0j132gd6ymf1hxtd2p00000gp/T/closer419-controls-niy5uydv is not a git checkout. In the exec mirror that loses nothing: its tests/ IS `git archive HEAD tests`, so every file in it was tracked
1 passed in 0.24s
```

