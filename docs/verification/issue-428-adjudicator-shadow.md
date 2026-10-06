# Issue 428: retrospective adjudicator evidence

This extends the existing AdjudicatorAgent and Brain role-run recorder. Source implementation
and local replay are distinct from runtime publication, which remains manual.

## Validation

- Named suite `python3 -m pytest tests/test_adjudicator_retro.py -q`: 8 passed.
- Broader role/advisor regression suite: 56 passed.
- Outcome-write fault: injected an UPDATE of original outcomes in the replay loop; the
  outcome-snapshot regression failed (exit 1). Byte-identical source restoration passed (exit 0).
- Unconditional-offer fault: let an adjudicator entry survive without a contested verdict;
  eligibility regression failed (exit 1). Byte-identical restoration passed (exit 0).
- Black, Ruff, touched-source mypy and `git diff --check`: pass.
- Final stable-source `python3 src/verify.py`: exit 0; the full private-state run
  passed 1,963 tests with one named prerequisite skip, 1,964 collected, 101 module selftests,
  118 mypy modules with zero exemptions. The private ledger lacked value-chain-monitor, so one
  coverage test and the corresponding gate skipped within the existing ceilings. Those skips
  are not proof of the missing row. Measured collection floor advanced from 1,956 to 1,964;
  all skip and exemption ceilings were preserved. Final timing: pytest 4m14s, selftests 35s, gates 24s.

## Real replay

The live 90-day Brain population contains 95 dispute rows; the issue's 71-row count is an older
snapshot. Seven bounded replay packets have real router-selected Gemini role runs, all tagged
`source=retrospective`, for Orchestrator #458, #446, #443, #456, #414, Workflows #3748 and
Manager-Database #1751. Initial packet collection refused truncated check evidence; reading
all exact-head pages recovered those cases without changing an outcome. Saved case IDs prevent
repeat paid calls; `--retry` is needed for diagnosed failed attempts.

Their later durability is pending: graded count 0 and both agreement rates UNKNOWN. The
transport's zero-dollar ledger placeholders are not complete cost telemetry: measured-cost
count 0 and total/per-case cost UNKNOWN. Remaining population replay belongs to the local
Reviewed Repo Merge Verify Closer after source review/CI, using
`python3 src/adjudicator_retro.py --dispatch --limit 5`; it can inspect the local Brain and
saved state report. A GitHub runner without that Brain cannot substitute fixtures.

No label, merge, comment or delivery-outcome action follows an adjudicator decision. The
closer offer requires the caller's actual recorded verifier and merge-disposition verdicts.
Missing or agreeing verdicts withhold it. Weekly switch review consumes the saved state report
and names missing report/evidence, ungraded durability and incomplete cost telemetry.

Machine-local transcripts and exact role-run IDs are retained under
`~/.codex/automations/pd-workloop-resume/evidence/20261005T1102Z`; the operational report is
`$ORCH_STATE_DIR/capability-program/adjudicator-retro.json`. These artifacts are not committed
as portable production evidence. Current-head CI, expected checks, complete review threads,
seven-minute push floor and postmerge `verify:compare` remain the delivery gates.


## Closer review reconciliation, round1320

Nullable mergeCommit now has a distinct merge-evidence unavailable error before any
verifier selection, so the resumable case records a precise retry condition. The MCP
capability_advice schema accepts both recorded verdict facts and forwards them as
context. Annotation preserves its documented membership/order invariant; a named
post-annotation eligibility step withholds non-contested adjudicator offers and
reports them separately in precondition.withheld. The summary describes all annotated
candidates, including the explicitly withheld population. No outcome, model default,
shadow gate or publication authority changes.

Focused retrospective tests passed 12 cases including merge-null refusal, direct
annotation invariance, MCP forwarding and contested/unknown/equal verdict eligibility.
Advisor, MCP and retrospective module selftests passed. Current-head CI and private
mirror/live-state-copy validation are still required after integration and push.

## Keepalive comparison validation, 2026-10-06

- [x] Compare saved proposals with later durability and the merge-rule baseline, and
  persist agreement rates, case counts and measured per-case costs in the state report.

`node tests/test_adjudicator_retro_cli.js` passed all four CLI tests. The added regression
withdraws trusted observation dates from saved durable/reverted cases, confirms those
cases leave both comparison denominators, then restores the dates at the detection
boundary and verifies the original comparison returns. Role identities, measured costs
and the entire Brain snapshot remain unchanged by each refresh. Metadata proposals
remain separate from accepted verdicts.

Deliberately replacing the durability cutoff with zero made the added test fail (exit 1).
The Python source was restored byte for byte; the four CLI tests then passed (exit 0).
The existing five evidence unittest tests and the retrospective selftest also passed.

The named pytest acceptance tests and their required outcome-write/unconditional-offer
fault checks remain pending for this round: pytest and Black are absent, and PyPI could
not be reached from this runner. This commit changes JavaScript tests and this verification
note only; it does not change Python source or claim the named pytest checks passed.
