Issue #567 / PR #568: complete and current synthesis delivery discovery.

Production discovery now accounts for every search hit before testing uniqueness. Each page must
explicitly report `incomplete_results=false`, the same integer `total_count`, the expected number
of distinct PR identities, and a total within GitHub's 1,000-result REST ceiling. Missing,
truncated, inconsistent, duplicate, over-bound or failed reads report `unavailable`; reconciliation
stays at `candidate_ready`. Every hit is then read through its repository-scoped PR endpoint and
checked for its number, canonical API URL, base repository and current body. Indexed bodies never
decide binding. Word characters, colon, period, slash, plus and hyphen extend identifiers;
whitespace, backticks, parentheses and commas delimit the exact, case-sensitive candidate marker.

The two reported regressions ran against the original production source before its first edit:
`red.log.gz` records two failures, both with `delivery_linked:owner/repo#77`. No production source was
swapped or restored. `source-hashes.json` identifies the original committed bytes and corrected
bytes, and proves that the floor, ceilings and lint/type configuration remained unchanged. Each
rejection test compares its disposable Brain's bytes before/after reconciliation, checks that the
persisted promotion has no delivery, and confirms that no outcome was written. All fixtures use
temporary experiments, repositories and Brain databases.

Verified local tasks:

- [x] Establish complete search coverage (including pagination and incomplete-result/error handling) before asserting uniqueness; fail closed when completeness cannot be proved.
- [x] Re-read each candidate PR directly, confirm repository/PR identity and exact candidate marker in its current body before binding. Define token-boundary semantics so an extended identifier cannot accidentally match.
- [x] Add focused tests through production discovery/reconciliation for truncated/incomplete search, >100 hits or an explicit safe bound, stale-search/current-body disagreement, direct-read failure, duplicate matches, marker-boundary cases and the valid unique-delivery positive control.
- [x] Preserve unique ingested-run binding, read-only Brain access, lock ownership and root-only capability attribution. Keep research launching and deployment/mirror activation under the existing owner's decisions.

Verified acceptance criteria and remaining delivery work:

- [x] Both reproduced failures are RED against the original production code and GREEN after correction, with byte-identical restoration/mutation evidence where used.
- [x] An incomplete/unreadable/ambiguous candidate search or current PR read never creates a delivery link or credits a capability outcome.
- [x] A complete unique current marker plus one ingested run still progresses through linked -> merged -> durable and credits exactly the root synthesis run.
- [ ] Focused existing synthesis/followup tests and selftests pass; ceilings/exemptions remain unchanged. Normal full exact-head expected checks, seven-minute review floor, zero active review threads and guarded merge apply, followed by actual verify:compare disposition.

`green.log` records 105 passing focused tests with `-m "not slow"`, including pagination at 101
and exactly 1,000 results. The positive control includes a resume run and credits only the root
synthesis run. Separate tests assert that discovery opens SQLite with `mode=ro`, rejects multiple
ingested runs, and holds the experiment lock through both search and direct reads. The synthesis
and exp_abcd CLI selftests both pass; their receipts are included. The broader selftest requires
`ORCH_LOCAL_RUNTIME` and `HANDOFF_DIR` under `/tmp` in this sandbox because the default home is
read-only. No launching, deployment, mirror activation or merge code changed.

Reproduce the focused suite:

```sh
pytest tests/test_synthesis_delivery_discovery.py tests/test_synthesis_promotion.py tests/test_followup_ship_gate_latch.py tests/test_exp_abcd_selftest_private_brain.py -m "not slow" -q
python3 src/synthesis_promotion.py --selftest
python3 src/exp_abcd.py --selftest
black --check --line-length 100 --exclude '(\.workflows-lib|node_modules)' .
```

The runner initially had neither pytest nor Black, and shell network access could not resolve
GitHub/PyPI. Validation used pytest 8.3.5 and the repo-pinned Black 26.10.0 source releases from
their official repositories, loaded only under `/tmp`. Black's unmodified CLI used one worker
and a periodic asyncio wake-up timer because the sandbox stalled completion notifications in
multi-file runs. This changes scheduling only; every formatting check still runs.

Full remote checks, review timing/threads, guarded terminal merge and actual postmerge
`verify:compare` disposition remain pending. PR #568 was confirmed open and ready for review.
Any terminal merge must use `src/merge_guard.py` with the expected head; these local receipts do
not authorize deployment or mirror activation.
