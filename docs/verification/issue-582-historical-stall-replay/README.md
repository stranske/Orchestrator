# Issue #582: offline replay and cap regression proof

Base: `34bfcff4816cecd9d18bdc957e22c44ba7ab7740`.

The source corpus contains twelve stalled reports, separate from the existing
three synthetic fixtures. All twelve are represented as anonymized inputs in
`tests/fixtures/redirect_stalls/`; the synthetic inputs are retained under its
`synthetic/` subdirectory and their existing callers now name that directory.
Targets are anonymized consistently, preserving repeated-versus-distinct lane
identity. Policy action/confidence are mapped inside fixtures only. The private
opener receipt binds each original corpus line/report to its committed fixture.

Validation:

- `python3 -m pytest tests/test_redirect_chain_gate.py tests/test_redirect_apply_prescreen.py tests/test_redirect_chain_backend_and_agent.py -q`: 57 passed.
- `node --test tests/test_redirect_sweep_shell_backend.js`: 7 passed.
- `python3 src/redirect_apply.py --replay-stalls tests/fixtures/redirect_stalls`: authorized 12/12 with named agents (dry-run).
- Black, Ruff, and `git diff --check`: PASS.
- Exact collection: 3297 = base 3295 + 2 new executed tests. The collection and
  pass floors increase by two; every skip/selftest/gate/mypy ceiling is unchanged.
  This is executed-node union evidence, not a full-suite PASS. Hosted `verify.py`
  in checkout and flat-mirror shapes remains required.

The historical replay test refuses dispatch, apply, claims, role-run, influence
and corpus writes, including attempted writes swallowed by a caller. The daily
cap test first proves that the same lane is eligible below the cap, then requires
exactly the daily-bound refusal at the cap.

Deliberate mutations and byte-identical restoration:

1. Replace `if applies_today >= MAX_APPLIES_PER_DAY:` with `if False:` in the
   existing production guard. The daily-cap test fails because a second apply
   is allowed. Restore the original bytes; the same test passes.
2. Remove `historical-12.json`. The replay test fails with `11 != 12`.
   Restore the original bytes; the same test passes.

Console red/green logs are adjacent (trailing line whitespace normalized; raw
logs remain in the private opener receipt). Restored production source SHA256:
`b0908ad15d28b348dd526f8227a50080502bf682950f3dd48be8f2490bbd1be7`.
Restored historical-12 fixture SHA256:
`cb382589b66d4274486129d90cbc076286aa19b4a57fdd493acc4f1f4d6d45c4`.

Replay authorizations are not model judgment, production accepted influence or
live stall resolution. No production code, owner flag or Stage-2 threshold
changes. The existing redirect cadence owns the next real bounded application;
issue #582 remains open for the 2026-10-20 replay checkpoint and the
2026-11-03 real-stall outcome checkpoint (or an explicit no-candidate result).

Current runner revalidation (2026-10-14):

- Added `tests/test_redirect_stall_fixtures.js` to require all twelve stalled
  experiment reports and explicit fixture mappings for both policy action and
  confidence. Replay's default `redirect` action previously allowed a missing
  mapping to go undetected by the authorization count.
- `node --test --test-isolation=none tests/test_redirect_stall_fixtures.js tests/test_redirect_sweep_shell_backend.js`: 8 passed, no failures or skips.
- In private scratch copies, missing policy mapping, a mismatched mapped action,
  and a missing fixture each fail the new regression. Byte-identical fixture
  restoration passes each time; repository fixture bytes are unchanged.
- `python3 src/redirect_apply.py --replay-stalls tests/fixtures/redirect_stalls`:
  authorized 12/12 with named agents (dry-run).
- The required focused pytest command could not run: this runner has no pytest,
  and network restrictions prevent installation. The named Python tests and
  daily-cap mutation were not reverified here. No Python files changed; the
  Python collection floors remain unchanged.
- The private shadow corpus is unavailable here, so the original source-to-fixture
  bindings were not independently reverified. GitHub CLI access fails, but the
  GitHub connector confirms PR #590 is open with `draft=false`. No remote task
  checkbox was changed.

These checks precede the scheduled replay and live checkpoints; they do not
establish an accepted live influence edge or a stall-to-resolution outcome.

Further runner verification (2026-10-14):

- Added a Node-driven check of the real Python replay and both public CLI output
  modes, using standard-library mocks. With live apply armed and heartbeats
  enabled, all twelve fixtures authorize named workers, every authorization
  remains dry, and dispatch, apply, claim, feedback and corpus write guards are
  untouched. The heartbeat setting is restored and the private state directory
  stays empty. Subprocess failures include the Python traceback.
- `node --test --test-isolation=none tests/test_redirect_stall_fixtures.js tests/test_redirect_sweep_shell_backend.js`:
  9 passed, no failures or skips.
- Scratch-copy controls: a corpus write whose guard exception is swallowed fails
  with `a swallowed write attempt still violates dry replay`; removing
  `historical-12.json` fails the replay assertion with `total: 11`. Restoring
  source and fixture byte-identically passes the new check after each control.
  Repository production source and fixture bytes remain unchanged.
- `python3 -m pytest tests/test_redirect_chain_gate.py tests/test_redirect_apply_prescreen.py -q -m "not slow"`
  still fails before collection with `No module named pytest`. No installed
  interpreter or local cached wheel supplies pytest; installation attempts
  cannot obtain it. The named pytest checks, focused pytest command and named
  daily-cap deliberate failure remain unverified in this runner. No acceptance
  checkbox or Python collection floor is changed.

This adds executable offline verification; the 2026-10-20 replay checkpoint and
2026-11-03 live outcome remain open. It provides no production candidate count,
accepted influence edge or stall-to-resolution comparison against a control.
