# Exact proposed-toolchain baseline

`report.json` retains the actual strict measurement against code revision
`7728066fa679ff8941a696d4b52c5b7e460f4607`, script digest, authenticated proposed
pin digest, installed tool versions and raw Ruff/Black/mypy command outputs.
The pin bytes come from generated Orchestrator #517 at
`c93fe39be4fa497475fdb915ffe10631b4cb186e`; they are immutable evidence, not
replacement repository pins. No generated PR was edited.

Reproduce in an isolated environment with the versions in `proposed-pins.env`:

```sh
python -c 'import json; from pathlib import Path; from scripts import ci_lint_baseline as b; b.PIN_FILE=Path("docs/evidence/issue-518-toolchain-baseline/proposed-pins.env"); print(json.dumps(b.collect(), indent=2))'
```

The strict tool-version check remains enabled. The normal CLI still measures
the repository's current pin file. Two complete documented measurements allow
the repo-local preparation to merge before the source-owned pin regeneration;
the config regression rejects combining versions from different measurements.

The initial mypy observation counted informational notes as errors. Restoring
that original counter makes `test_mypy_success_notes_are_not_blocking_findings`
fail with `1 != 0`; restoring the fixed code makes it pass. The scratch-tree
regression also deliberately raises the identical extraction error after a
successful real `git archive`, then checks temporary-directory cleanup.

The coverage measurement here proves startup, not suite or combined coverage.
Main, mirror and combined-coverage commands retain their existing floors and
capability/skip guards, and their separate receipts must be checked before
claiming delivery acceptance. Local macOS proof is not hosted Linux acceptance.
