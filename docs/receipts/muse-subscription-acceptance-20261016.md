# Muse subscription acceptance recovery — source #612

PR #617 merged, but its native verifier reported FAIL / CONCERNS. This follow-up completes
its missing local execution/accounting contract; a new current-head CI and post-merge verifier
verdict remain necessary. No provider PASS is asserted here.

## Native execution evidence

Actual dispatcher offload `offload:muse:1792138458590067000` reviewed the source parser and
returned a concrete false-success counterexample. The CLI and normalized dispatcher both
exited 0; one actual invocation took 43.10492491722107 seconds. The review finding was repaired
and its exact counterexample became regression coverage. The final parser replays the retained
native JSONL to the same useful review. This was a real code-review task, not a smoke marker.

The recorded `runs` row joins by run ID to `costs` and `muse_subscription_usage`:

| Field | Observed value |
| --- | --- |
| agent / mode | muse / offload |
| invocation count / raw exits | 1 / `[0]` |
| cost / source | 0.0 / `imputed:subscription` |
| token input / output | NULL / NULL (unreported) |
| observed start / end | 1792138458 / 1792138501 |
| local rolling five-hour interval | 1792120458 / 1792138458 |
| provider reset | UNKNOWN |

The interval is the locally observed lookback at invocation start, not a provider quota reset
or a claim that the whole task fits inside a fixed allowance bucket. Subscription-flat cost is
an accounting imputation, never measured API spend or evidence of free/unlimited capacity.

A prior HOME-isolated attempt failed before model output because the existing subscription
keychain login was unavailable. Keeping HOME real while redirecting XDG configuration recovered
the login. A local non-provider configuration probe under the same real HOME returned exit 1
for deliberately invalid isolated user settings and exit 0 for clean isolated settings. The
child pins `MUSE_AUTH_PATH`, scrubs `META_API_KEY`, redirects mutable XDG directories, keeps the
sandbox, and disables foreign personal context and session logging. This proves configuration
selection and actual execution, not a hard filesystem boundary around HOME or an undocumented
provider credential-selection order.

One earlier native review had raw CLI exit 0 but normalized exit 70 because the initial parser
used the wrong wire protocol. Its failed normalized outcome remains recorded. Neither failed
trial nor older author runs were backfilled as successful qualification.

## Reproducible checks

Use the selected Python interpreter:

```sh
python3 -m pytest -q tests/test_muse_backend.py
python3 src/capacity.py --selftest
python3 src/router.py --selftest
```

The named argv/config, metered-seat and credential-missing tests satisfy the source issue's
required test entry points. Deliberately removing the Muse foreign-context flag caused the argv
test to fail; restoring the exact bytes passed. Removing the Muse capacity seat caused the seat
test to fail; exact restoration passed. Accounting coverage executes a legacy seven-column
`costs` insert and proves measured provider costs survive addition of subscription usage facts.
Parser controls reject progress-only, failed/unbound, conflicting, malformed and post-terminal
streams. Role controls preserve default backend choices and admit Muse only explicitly through
the existing backup guard; no selection algorithm or prior weight changes.

The machine receipts, raw redacted native JSONL, joined database evidence and deliberate
RED/GREEN logs are retained in the automation's `drain-20261016T0703` evidence directory.
The compatibility recovery retained all 8,598 existing cost rows byte-equivalently in their
original seven fields (SHA256 `5d2afe1e867c38920228943bf7ef1bfa37d24be988052f6ea13f5e0a498602db`)
and preserved two authentic trial measurements in the keyed additive extension. It also kept
a complete wide-table recovery backup. The production mirror remains the guarded published
version until this follow-up is merged and passes the guarded snapshot publisher.
