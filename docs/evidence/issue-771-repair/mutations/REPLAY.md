# Original-five mutation replay

The existing per-test Markdown records preserve the earlier failure and restored
pass captures. The snapshot failure excerpt is truncated at its start. This
continuation does **not** replace those captures or claim a new historical pytest
run succeeded.

[`scripts/verify_issue771_mutations.py`](../../../../scripts/verify_issue771_mutations.py)
now provides a repeatable replay. Supply a clean, isolated checkout whose HEAD is
`18ad97a054dab7f6039dd42c8131bee04267748b`, an interpreter with pytest installed,
and an empty output directory outside that checkout:

```bash
python3 scripts/verify_issue771_mutations.py \
  --checkout /tmp/issue771-original-head \
  --python /path/to/venv/bin/python \
  --output /tmp/issue771-mutation-replay
```

The runner verifies the commit and the original source/test Git blob IDs, runs
all five tests together, then runs each deliberate mutation and its restoration
separately with `-m "not slow"`. Each run preserves complete combined output,
the command and exit status, and JUnit XML. The runner requires the exact test
IDs and distinguishes test failures from collection/setup errors and skips.
Every mutation restores `src/feedback.py` in a `finally` block. Each pytest
process uses a fresh bytecode cache so restored source cannot reuse a mutant.
`receipt.json` is written only after every failure/restoration pair is verified.

The selector mutation reintroduces the original duplicate invocation counting
in `role_activation_metrics`, exercising the metric assertion rather than the
earlier input-validation assertion. The other four mutations reproduce the
breaks documented in the existing records.

## Static validation in this continuation

The five proposed patches are preserved under [replay-patches/](replay-patches/).
[patch-validation.json](replay-patches/patch-validation.json) explicitly records
`pytest_executed: false`. Both original GitHub blob IDs were inspected at the
historical head through the connected GitHub API. The locally available squash
merge `b1511de27d025a65b4424144a405fd2625ffca28` has identical source and test
blobs; it was used solely to validate patch application and restoration. Each
mutated source parses as Python, each patch applies with `git apply`, and its
reverse restores the original bytes. This is **static patch validation**, not
mutation execution evidence or proof that the entire two commits' trees match.

Validation completed:

```text
python3 -m unittest discover -s tests -p test_issue771_mutation_runner.py -v
Ran 13 tests; OK
```

The seven existing fixture-free evidence guard functions in
`tests/test_issue_771_evidence.py` also passed via `unittest.FunctionTestCase`.
These safety tests use controlled subprocess results and do not constitute an
execution of the historical five tests.

## Remaining execution blockers

The historical head object is absent locally. The mounted `.git` directory is
read-only, and shell access to GitHub failed. Neither pytest nor Black is
installed, and the attempted package installation found no available packages.
The targeted pytest command failed with `No module named pytest`; the Black
formatting/check gate could not run because `black` was not found. A commit is
therefore blocked by both the required formatting gate and repository permissions.

The first unchecked mutation task remains incomplete pending a real replay and
preservation of its full outputs. No acceptance checkbox was newly marked complete.
