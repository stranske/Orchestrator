# Captured operand assertions (PR #549)

**Historical runner evidence, superseded for current validation.** The restrictions
and unexecuted commands below describe the earlier captured-paths runner only.
The parent README and proof.json record the subsequent successful production
pytest and Node executions; this historical record is retained without claiming
these earlier restricted results are the current verdict.

Source base: `f1bac523536748b89ab7a7a91b1c93043afd7f01`.

The three production Node option witnesses now capture the actual argv at the
production exec boundary. After real publication, before resuming execution,
they assert the script and preload operands name the selected physical generation
and the relative data argument remains `mirror/data.json`. These assertions pass
in all three forms before the runner's Node child-process restriction prevents
the subsequent child import witness. No production Python files changed.

Validation on this runner:

- `node --check tests/test_mirror_reader_reentry.js` and `git diff --check`: pass.
- Existing Python operand-capture test: pass (`capture.log`).
- Real-publication capture control with the production reader, five command forms:
  baseline 5 pass, option-only revert 5 intended operand assertion failures,
  restored source 5 pass (`capture-mutation.log`, `capture-mutation.json`).
  Production checkout source was never mutated; a scratch source copy was
  restored byte-for-byte, SHA256
  `3007f0ba4a57e5ff57c2cef9f78a86e905419469989a96806b9316b92b473872`.
  This supplementary control is not the acceptance criterion's named pytest test.
- Standard-library Python suite: 16 tests; three Node subtests fail at
  `execFileSync` with `spawnSync ... EPERM` (`python-unittest.log`). The bash
  option subtests pass. This is not a claim of a passing pytest invocation.
- Required `node --test tests/test_mirror_reader_reentry.js`: exit 1 (`node.log`).
  Diagnostic retry with `--test-isolation=none --test-reporter=tap`: nine incumbent
  controls pass; three option witnesses fail only at the child-process result
  assertion, after the new captured-path assertions passed (`node-in-process.log`).
- pytest and Black are absent, and installing them from PyPI fails DNS resolution.
  The required pytest invocation could not run (`pytest-unavailable.log`). No
  `.py` changes are included, so the pre-commit Python formatting gate is not
  applicable to this commit.

The checkout `.git` is read-only: `git add` fails creating `.git/index.lock`.
The JS changes and raw evidence are prepared as a commit in a separate writable
checkout and an importable patch. The PR branch has not been updated. Acceptance
checkboxes remain pending until the required commands pass and evidence reaches
the PR. No workflow or protected configuration files changed.

The GitHub connector also rejected the attempted first-task checkbox update,
raw-evidence PR comment, and `needs-human` label: mutations require approval,
while this run's approval policy is `never`. No PR metadata was changed. The
connector confirmed PR #549 remains open with `draft=false` at the source base
above.
