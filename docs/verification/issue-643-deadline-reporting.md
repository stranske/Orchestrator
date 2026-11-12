# Issue 643: deadline reporting check (2026-11-12)

The weekly live reader now reports `timing UNVERIFIED` when the current graded
sample reaches 20 after the 2026-11-08 deadline. A current count cannot establish
when that sample filled. A sample below 20 still reports `MISSED`; qualification
and retirement continue to use the current sample and the 0.75 agreement rule.

The private-Brain M2 integration exercise fixes the reporting date to November 12
and checks the 19-to-20 transition, weekly text output, and report-loss recovery
for 0/20, 14/20, and 15/20 agreement. The added assertions failed against the
previous implementation and passed after the change.

Validation: 3 M2 integration scenarios, 139 focused Python tests with
`-m "not slow"`, and the live module selftest passed. Pytest and Black were loaded
temporarily from their official upstream sources because the runner lacks them.
Black formatted the changed Python file, and the required full-repository check
passed with 699 files unchanged. `git diff --check` also passed.

The production criterion remains unchecked. The [November 8 weekly check on
#575](https://github.com/stranske/Orchestrator/issues/575#issuecomment-6723832468)
reports advised 4, graded 4, agree 2, disagree 2. No production Brain or live
report is available in this runner. Owner evidence is needed to establish the
sync window and resolve the elapsed deadline; no replacement date was assumed.

Staging failed because `.git/index.lock` cannot be created on the read-only
`.git` mount. No commit was possible. GitHub rejected both the `needs-human`
label and blocker comment with `MCP tool call requires approval, but approval
policy is never`; neither was posted. PR #1356 was read back as open and ready
for review (`draft=false`).
