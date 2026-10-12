# Acceptance assertion follow-up for PR #549

**Historical runner record.** The pending checks and uncommitted assertions
below describe the earlier restricted runner. Current passing production
validation is in the parent README, proof.json and focused.log; the operand
assertions are committed in tests/test_mirror_generations.py.

Reviewed both PR commits, `f1bac52` and `a671633`. The seven implementation
checkboxes are already complete in the PR body. The three acceptance checkboxes
remain pending; none of the failing runner commands below qualifies as GREEN.

The JavaScript production witness now covers Bash `-o` and `+o` as well as the
three existing Node forms. Each Bash case pauses at the production exec boundary,
publishes another generation, checks the captured physical script operand and
unchanged relative data argument, then runs the script and its parent/child
Python imports. Both new Bash witnesses pass (`bash-options-green.log.gz`).

Python direct operand assertions were also prepared for the named acceptance
test. Their patch is `/tmp/issue528-python-capture-pending.patch`. They were
tested with unittest, but are **not committed or left in the source tree**:
Black is unavailable, so the required Python pre-commit gate cannot pass.
The Python logs and Python test hash in `receipt.json` describe that pending
patch, not the committed Python test file.

The pending Python witness under the option-only production revert produces
five intended assertions (`named-option-revert-red.log.gz`): four unpinned scripts
and one unpinned equals-form preload. That form's script was already pinned by
the incumbent parser. The scratch production file was restored byte-for-byte;
the production checkout reader was never changed. Baseline/restored runs pass
all direct operand assertions and both Bash execution cases, but fail the three
Node child executions with `EPERM`. These are not passing acceptance runs.

Runner limitations are preserved as raw output:

- Required pytest: unavailable (`required-pytest.log.gz`).
- Black formatting and repository check: unavailable (`black-format.log.gz`,
  `black-check.log.gz`). Installing the tools from PyPI found no distributions.
- Required Node command: exit 1 (`required-node.log.gz`). Diagnostic TAP with
  `--test-isolation=none` passes nine incumbent controls and both new Bash cases;
  the three unchanged Node child imports fail with `spawnSync ... EPERM`
  (`diagnostic-node.log.gz`).
- Full unittest suite with the pending Python patch: 16 tests, three Node
  subtest failures (`python-unittest.log.gz`).
- The repository `.git` is read-only; staging JavaScript fails creating
  `.git/index.lock`. A JavaScript/evidence commit is prepared in the writable
  checkout `/tmp/issue528-review-commit`; it does not include Python changes.

`receipt.json` records command arguments, exit codes, the starting PR head and
source hashes. The acceptance criteria require rerunning on a runner with
pytest/Black and working Node child processes, committing the formatted Python
patch, and retaining passing output at the published PR head.
