# Native replay evaluated-revision binding

On base `beed63a`, the native cohort/publication CLI passed the saved PR head
to the reviewed replay runner. A squash merge can have a different identity,
so the runner correctly refused this caller before canonical refresh. This is
the concrete CLI defect retained in the [source #579 observation](https://github.com/stranske/Orchestrator/issues/579#issuecomment-6229128915).

The CLI now uses `_case_evaluated_sha` on the matching enriched original row,
the same selector used by qualification and source collection. A missing or
malformed evaluated anchor fails before replay. The saved PR snapshot stays
unchanged, and the producer still enforces its reviewed case/revision binding.
Publication locks, receipt authentication and compare-and-swap checks are unchanged.

Seven standard-library tests exercise the real CLI, Git objects, private replay
child, collector, journal and cohort/publisher. Both CLI modes handle distinct
PR-head and evaluated-merge commits, including the existing merge-anchor fallback.
Independent later truth comes from a separate SQLite fixture: a validated binary
receipt first remains ungraded, then grades after an independently inserted
durability record. Refresh changes neither that database nor the immutable
original, journal or qualification. Original paid identities remain attributable;
any provider invocation fails the tests. Three refusal tests cover absent and
malformed evaluated anchors and a mismatched reviewed runner.

Run the tests without external test dependencies:

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -p test_adjudicator_replay_revision.py -v
```

[Validation](validation.json) binds source/test/config and compressed raw logs.
Running the same seven tests with unchanged `HEAD:src/adjudicator_retro.py` in a
temporary import directory produces four errors at the original replay binding
and six failed refusal subtests. The fixed source passes all seven tests.
The existing 18 diagnostics and module's 23 self-checks also pass. The new test
module appears once in the normal shard plan and untimed inventory.

These are private fixture checks, not authentic production receipts. The
configured runtime directory, canonical report and independent outcomes database
are absent in this runner; no authentic alternative original/journal/deployment
paths were available. No production inventory, retry, canonical grading,
October 18 proof or next-20 observation was produced. All operational tasks
remain unchecked. This repair advances the native refresh path only.

The focused pytest command could not start because pytest is missing. Both the
required Black formatting command and repository-wide Black check could not
start because Black is missing; installation into `/tmp` failed with no package
versions available. No commit or push was attempted because the mandatory Black
gate could not pass. Adding `needs-human` and posting the blocker on PR #1081
were both rejected: those tools require approval and this run's policy is `never`.
