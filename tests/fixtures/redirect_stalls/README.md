# Redirect stall replay fixtures

The twelve `historical-*.json` reports are anonymized regression inputs derived
from the twelve recorded stalled experiment reports selected for issue #582.
Repeated original targets retain the same anonymized target; distinct targets
remain distinct. Agent, lane, liveness, hint, drift and policy facts are retained.
`policy_action` and `policy_confidence` are also mapped to `policy_decision` in
the fixtures, so the production replay does not acquire a schema adapter.
The original synthetic auth, drift and exited cases remain under `synthetic/`.

The replay supplies a fixed proposal and named worker, exercises the existing
plan/authorization chain, and never invokes a model or applies a plan. Its
`authorized 12/12` result is offline chain coverage, not a role judgment,
accepted influence edge, stall resolution or evidence of a live application.
Instance-specific corpus entry identities and source-to-fixture bindings remain in
the opener's private run receipt. No runtime flags or Stage-2 thresholds change.
The next real stall and its bounded live application remain separately owned
by the existing redirect cadence; the issue's future outcome checks still apply.
