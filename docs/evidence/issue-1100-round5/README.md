# Issue #1100: observation-window identity conflicts

Production acceptance remains **unproved**. The first unchecked task is blocked by absent
production state on this runner. No production receiver was restarted, hook replayed, or lock
released. No operational task or acceptance checkbox is newly completed.

The fresh [environment readback](environment.json) at 2026-11-03T22:13:27Z confirms that
production Brain, relay pacing and exit logs, and the requested Anaconda interpreter are
absent, with no relevant path overrides set. The bounded read-only
[production audit](production-window.txt) exits 1 with unavailable/null counts
([terminal record](production-window.json)). These are unavailable inputs, not zero traffic.
Fresh issue #1100 discussion retrieval supplied no production receipts.

## Demonstrated failure and implementation

The existing raw audit discarded pacing decisions before the window start **before** checking
decision ID conflicts. A synthetic decision ID reused across deployment therefore incorrectly
earned an attributable accepted join. The new regression failed on the old implementation
with `accepted_window_joins` equal to 1 instead of 0.

`scripts/report_relay_join_window.js` now retains earlier pacing rows for identity conflict
checks. Only IDs with a decision inside the selected window enter the counts; the reported
decision timestamp comes from an in-window row. Rows after the window end remain excluded,
so later ID reuse cannot retroactively alter an earlier window's attribution verdict.

Two regressions demonstrate cross-deployment conflict refusal and independent historical
window assessment. They retain raw timestamps and verify CLI output and unchanged private
database/log bytes. This fixes an audit-tool defect; it does not establish a production
ingestion defect or substitute synthetic evidence for production observation.

## Bounded verification

- Relay CLI suite: **18 passed**, exit 0, including both new regressions
  ([output](relay-cli-tests.txt), [terminal record](relay-cli-tests.json)).
- Both changed JavaScript files pass `node --check`; `git diff --check` passes
  ([source](source-syntax.json), [tests](test-syntax.json), [diff](diff-check.json)).
- Repository pytest with `-m "not slow"`: exit 1 because pytest is absent
  ([output](repository-pytest.txt), [terminal record](repository-pytest.json)).
- `/opt/anaconda3/bin/python3 src/verify.py`: exit 127 because the interpreter is absent
  ([output](requested-verify.txt), [terminal record](requested-verify.json)).
- Requested-interpreter switch-review attempt: exit 127 for the same missing interpreter
  ([output](requested-switch-review.txt), [terminal record](requested-switch-review.json)).
  No fresh report or report-generation code failure was observed.

No Python source changed, so the Python-only Black pre-commit gate does not apply.
The full test and verifier acceptance criterion remains unchecked.

## Floor, deadline, and delivery blockers

The unchanged `.verify-floor.json` is byte-identical to assessed local `origin/main`
`8ad3fd93293f389fa1207e8f2b9597e77ce15c5e`. Both SHA-256 values are
`780186ee0caf07002daac988ecbd91c4d58efb8e1957adb164c8ebae25d0f849`.
A fresh GitHub main floor retrieval has the same blob ID as the local file
([comparison](source-and-floor.json)). This does not establish test success.

The deadline **2026-11-04T08:16:32Z** is still in the future at this observation. No
attributable production evidence was available to establish timely acceptance, and a missed
deadline cannot yet be declared. Later recovery evidence must remain separate.

Staging the tested source and tests failed with exit 128 because `.git` is read-only
([output](git-add.txt), [terminal record](git-add.json)). No commit or push was possible.
The changes remain in the working tree for the automation system to collect.

Fresh [PR metadata](pr-state.json) shows #1101 already merged externally, closed, and
`isDraft=false`. Issue #1100 is also closed. This run created, changed, or merged no PR;
external closure does not establish operational acceptance. A blocker comment on #1101 and
`needs-human` label on #1100 were attempted as instructed, but the connector rejected both:
`MCP tool call requires approval, but approval policy is never`
([action results](remote-update-results.json)). No remote task checkboxes were changed.

Completion still needs a production-readable seat with the requested interpreter and test
tools, writable Git metadata, and an authorized GitHub mutation channel.
