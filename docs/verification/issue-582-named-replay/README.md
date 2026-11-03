# Issue #582 — named replay and daily-cap acceptance evidence

**Closer repair check date:** 2026-11-03 (America/Chicago).  
**Git HEAD for all runs below:** `12e7ba00f07326213080edf64e85e2717ba0185b` (same as `origin/main` at repair time).  
**Interpreter:** `/opt/anaconda3/bin/python3`.  
**Stop rule:** one baseline run, then one mutation run per required break; stop at the first unexpected failure; 20-minute wall bound (actual wall ~3 minutes).

**Inherited implementation (do not redo):** PR #595 (`81fbab5`) on main restored the exact pytest node name `test_replay_of_the_twelve_historical_stalls_authorizes_at_least_nine` while keeping the stronger `authorized == 12` assertion and no-write guards. Production code and the twelve historical fixtures were already on main via earlier work documented under `docs/verification/issue-582-historical-stall-replay/`.

## Provenance (from committed records only)

- **Twelve-fixture corpus:** `tests/fixtures/redirect_stalls/historical-01.json` … `historical-12.json` (12 files) plus three synthetics under `synthetic/`. Fixture README states these are anonymized inputs derived from the twelve stalled experiment reports selected for #582; instance-specific shadow bindings remain in the private opener receipt (not in git).
- **Issue filer evidence (2026-10-14):** GitHub issue #582 body documents twelve `report.state == "stalled"` rows in `~/.codex/orchestrator/redirect-shadow/shadow.jsonl` and the schema mapping (`policy_action` → `policy_decision` in fixtures only). This repair did not re-open the private corpus.
- **Exact replay test:** `tests/test_redirect_chain_gate.py::test_replay_of_the_twelve_historical_stalls_authorizes_at_least_nine` — asserts `total == 12`, `authorized == 12`, dry-run, no dispatch/claim/learn side effects.
- **Exact daily-cap test:** `tests/test_redirect_apply_prescreen.py::test_redirect_apply_daily_cap_refuses_second_apply` — below cap allowed; at `MAX_APPLIES_PER_DAY` refuses with block `daily bound reached (1/1)`.

## Baseline (2026-11-03)

```bash
/opt/anaconda3/bin/python3 -m pytest \
  tests/test_redirect_chain_gate.py::test_replay_of_the_twelve_historical_stalls_authorizes_at_least_nine \
  tests/test_redirect_apply_prescreen.py::test_redirect_apply_daily_cap_refuses_second_apply -q
```

See `582-baseline-named.log` (2 passed).

```bash
/opt/anaconda3/bin/python3 -m pytest \
  tests/test_redirect_chain_gate.py tests/test_redirect_apply_prescreen.py -q
```

See `582-named-green.log` (25 passed).

```bash
/opt/anaconda3/bin/python3 src/redirect_apply.py --replay-stalls tests/fixtures/redirect_stalls
```

See `582-replay-cli.log`: `authorized 12/12 with named agents (dry-run)` — satisfies N ≥ 9 with zero refusals.

## Deliberate-break → restore (disposable checkout copy)

Production guard SHA256 (restored): `aba6e9354521d05caa83864b288fddff81371761f8589a340988f39a229915e4` (`src/redirect_apply.py`).  
`historical-12.json` SHA256 (restored): `cb382589b66d4274486129d90cbc076286aa19b4a57fdd493acc4f1f4d6d45c4`.

1. **Daily cap:** replace `if applies_today >= MAX_APPLIES_PER_DAY:` with `if False:` → `test_redirect_apply_daily_cap_refuses_second_apply` fails (`assert True is False`). Log: `582-daily-cap-mutation-red.log`. Restore guard → `582-daily-cap-restored-green.log` (1 passed).
2. **Fixture count:** remove `historical-12.json` → replay test fails `assert 11 == 12`. Log: `582-replay-fixture-mutation-red.log`. Restore fixture bytes; focused suite green again in `582-named-green.log`.

Historical missing-node proof (pre-#595): `582-named-red.log` (pytest exit 4, node not found on old main).

## Dated replay checkpoint (2026-10-20)

No machine log in this repository is timestamped 2026-10-20 for `--replay-stalls` or the named pytest node. The scheduled checkpoint was not captured as a standalone dated artifact here. Closest committed offline proof predates that date (`docs/verification/issue-582-historical-stall-replay/` at base `34bfcff`, CLI `authorized 12/12` documented without a 2026-10-20 stamp). **Do not treat 2026-10-20 as satisfied by this repair alone.**

## Live outcome checkpoint (2026-11-03)

No live stall apply was forced or replayed. Fresh switch review (read-only):

```bash
/opt/anaconda3/bin/python3 src/switch_review.py --text-out docs/verification/issue-582-named-replay/582-switch-review-2026-11-03.md
```

Console capture: `582-switch-review-run.log` (~181s). Relevant rows:

- `role-redirect`: `acted_on 0 (+763 assoc)`; `first_break not_acted_on`
- `redirect-apply-bootstrap`: `acted_on 0`; `ORCH_REDIRECT_APPLY_BOOTSTRAP` armed but `idle=never invoked`
- Stage-2 drain line: `drainable 0 of 0 current candidate(s)` — **candidates 0**, not pending

**Operational acceptance for bounded live apply remains unmet** on 2026-11-03. Issue #582 should stay open for a future real stall with `acted_on > 0` and outcome comparison; do not represent `candidates 0` as `acted_on > 0`.

## Acceptance status vs #582 criteria

| Criterion | Status on 2026-11-03 |
|-----------|----------------------|
| Named twelve-stall replay test + cap test + focused pytest | **Met** (logs above) |
| Deliberate-break → revert for cap and fixture | **Met** |
| `authorized N/12` with N ≥ 9 on replay checkpoint date | **Met on 2026-11-03 run** (N=12); **2026-10-20 dated record absent** |
| Live `acted_on > 0` / stall resolution on 2026-11-03 | **Not met** (0 candidates, 0 acted_on) |

No production code changes in this repair PR — evidence only.
