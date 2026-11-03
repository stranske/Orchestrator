# Dispatcher planning follow-up

The repository's `escaped_defect_priority.rank(Path.cwd(), limit=12)` selected
`src/dispatcher.py` first at the starting head `308eb02`: escaped-defect weight 45,
churn 64. Next were `feedback.py` (35, 59), `capability_advisor.py` (32, 68), and
`capabilities.py` (25, 57). The default lookback is 180 days. `ranking.json` retains
the output. No coverage report was available: tier-three zeros mean unmeasured,
not fully covered. The distinct first-tier scores determine this ordering.

Eight new tests exercise the actual `plan_dispatch` path: optional role failure,
accepted/rejected lineage deduplication, explicit profile overrides, command
failure classification, missing/present dry-run worktrees, and resolved Gemini
workspaces. Process, agent, provisioning, feedback-write, and socket boundaries
are denied and asserted unused. Both runtime environment roots point inside each
test's temporary directory. Only test and verification files and the floor change;
there is no retained production change and no newly discovered production bug.

Actual green command:

```sh
PYTHONPATH=src python3 -B -m unittest discover -s tests -p test_dispatcher_plan_boundaries.py -v
```

Result: **8 tests passed**. Each was then run separately against a deliberately
broken `plan_dispatch` implementation: **all 8 failed**. After restoring the exact
source bytes, all 8 passed again. `mutations.json` records each replacement,
exit status, and the original source SHA-256; `mutation-red.log` and
`restored-green.log` retain the actual runner output. Mutations changed the
behavior under assertion, rather than deleting functions or breaking imports.

The standard-library runner was used because this runner has no pytest, Black,
Ruff, or coverage installed. Installing them in `/tmp/pr710-venv` failed because
`pypi.org` could not resolve. These required commands were attempted and failed
because the respective executable or module is absent:

```sh
black --line-length 100 tests/test_dispatcher_plan_boundaries.py
black --check --line-length 100 --exclude '(\.workflows-lib|node_modules)' .
ruff check --extend-exclude .workflows-lib tests/test_dispatcher_plan_boundaries.py
python3 -m pytest tests/test_dispatcher_plan_boundaries.py --cov=dispatcher --cov-branch --cov-report=term-missing -m 'not slow'
python3 -m pytest --collect-only -q -m 'not slow'
```

The floor is provisionally 4883 = the existing branch floor 4875 + 8 new cases;
independent pytest collection and the full-suite pass remain required. No skip
ceiling changed. No coverage percentage or reduction toward 180 units is claimed.
CI's combined measurement remains authoritative; the issue's supplied 85.60%
baseline could not be refreshed here.

GitHub's API also could not be reached, so the PR body, current main, and PR
readiness could not be refreshed or verified. No PR was created, changed, or
merged by this follow-up. The mandatory Black gate prevents committing or pushing
the Python change until the tooling is available and the check passes.
