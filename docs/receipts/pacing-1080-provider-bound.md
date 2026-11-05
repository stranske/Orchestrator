# Claude fallback provider bound (#695 / #1080)

Written before continuation edits, checked 2026-11-04 America/Chicago
(2026-11-05 UTC): the acceptance outcome is the **deployed** mirror's
`/opt/anaconda3/bin/python3 ~/.codex/orchestrator-mirror/capacity.py pacing-report`
printing Claude with state other than unknown, source and sample age, alongside
the deployed revision after guarded merge and mirror sync. Branch-local and
forced-source output cannot satisfy this outcome.

Stop rule: three repair/validation cycles or 60 minutes. Gate blocks merge;
focused repair and CI inspection remain drainable while Gate is closed.

## Demonstrated failure

Gate run 41960109886, head 8e539072, explicitly reports Black would reformat
`src/pacing_usage.py` and `tests/test_pacing_usage.py`. Format only those files.
Its pytest workers also exit 4 and report zero tests, with no explanatory
assertion in the log or JUnit artifact. A passing separate CI aggregate does
not dispose of that Gate failure. No dependency pin is edited here.

Run 41967738321 reproduces the startup failure. Comparing its tested merge
tree (2044a8f) with the head's Gate matrix identifies 19 missing test paths:
the head manifest still names tests retired on main, whereas the merged
manifest is already correct. Gate builds the matrix from the head but tests
the merge tree. Merge current main into the existing branch to align those
inputs without changing the finished pacing implementation. The resulting
tree before this evidence update is identical to the CI-verified merge tree;
its Gate matrix has 618 unique modules and zero missing paths. Retain upstream
authors and dependency-pin ownership; do not hand-edit pins or shared workflows.

## Real-input reproduction and correction

Read existing local provider history and Claude project events; make no provider
request. Count events from each observation's own `resets_at - 604800` through
its timestamp, not through the current clock. The documented 0.34-versus-0.55
discrepancy reproduces at timestamp 1793692510, window
[1793509200, 1794114000]: 1,029,393,011 tokens / desktop cap
3,079,032,286.5800867 = 0.3343235521, while the provider observation is 0.55.
The matching provider-implied cap is 1,871,623,656.3636363.

The historical desktop median is not a stable quota conversion. Local raw
tokens include cache reads and span changing model/cache mixes and possibly
incomplete local coverage. Those are plausible causes; the evidence does not
identify their individual contributions. Absence of a live calibration file
also means calibrated-cap precedence alone cannot correct this observation.

Bound the desktop estimate using the newest positive provider pair from the
**exact same reset window**, counting tokens only through that pair's timestamp.
Do not take the minimum of all historical provider caps: the real history
would falsely estimate 1.055 at provider 0.55 from an early-week model mix.
Keep configured calibration precedence. Independently floor fallback usage at
the highest already observed same-window provider fraction, including when
local logs are incomplete. Integration must preserve this floor when it applies
calibration again. This floor expires at the reset; future observations, other
weekly buckets, rolled windows and different reset boundaries are excluded.
Provider history is read using the existing bounded tail reader. Cache identity
includes the sanitized provider observations. Report provider sample age and
the floor separately from the token sample's age.

After correction, real aligned replay prints:

```
ts=1793692510 provider=0.55 tokens=1029393011 old=0.3343235521 new=0.55
ts=1793852670 provider=0.78 tokens=1470045198 old=0.4869806246 new=0.78
```

This is a conservative bound against already observed underestimation, not a
guarantee about usage unseen since the last provider observation. Missing token
logs and changing model mixes can still make extrapolation inaccurate. The
normal fresh provider source remains preferable.

## Focused verification

`/opt/anaconda3/bin/python3 -m pytest tests/test_pacing_usage.py -q`:
44 passed in 0.38s after reverting deliberate breaks. The same file with
`-n 4 --dist=loadgroup`: 44 passed in 1.91s. This includes the retained corrupt
UTF-8 log and rolled-poll/pre-boundary desktop regressions.

Deliberate provider-bound bypass: the real-pair regression fails with
`0.3343235520740049 != 0.55` (1 failed). Bypassing both desktop and candidate
age checks: stale desktop and stale poll selection regressions fail (2 failed,
1 passed). Restore production code before the final focused run.

Required whole-tree verdict belongs to exact-head CI's `src/verify.py` sharded
and aggregate procedure. No full suite is run locally. Merge and deployed
acceptance remain pending until the actual required checks and review gates
pass, followed by authorized mirror synchronization.
