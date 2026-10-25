# Closer transport qualification evidence

Category 2 checkpoint, based on core qualification API commit
`ffa747fce4914e8cd4f0b0fdf23525e120fad645` (#940). Production role records: **0**.
No candidate/provider calls were made. No transport was activated. Missing
adapters describe an engineering gap, not a model capability result.

## Delivered offline boundary

`src/transport_qualification.py` audits saved observations and never launches a
CLI, invokes tools/MCP, reads credentials, changes settings, writes qualification
records, or alters routing. It uses `closer_role_qualification.profile_identity`
and `identity_digest`, keeping its reports separate from the production
`closer-role-qualification/v1` registry. Constrained repair remains core-owned;
this harness only audits bounded classification and named-criterion adjudication.

The synthetic suites cover ordinary evidence, hostile instruction text inside
data, false acceptance of retained NON_PASS, and uncertainty/security escalation.
The oracle is frozen separately from the capture and its input digest is bound
per case. These deliberately small exact-answer cases exercise the evidence
pipeline; they are not a comprehensive model quality benchmark. Candidate outputs
must not author or change the oracle used to score them.

Each capture pins the exact profile (including effort, role, adapter/prompt
versions and shared pool IDs), executable path/hash, adapter hash, configuration
snapshot/hash, argv, and isolated home/cwd/environment claims. Four retained
canary probe artifacts cover tool denial, MCP, hooks, and ambient project context.
A successful assistant turn must supply exactly one final JSON object, without
prose, code fences, event-stream/tool-output substitution, duplicate keys,
nonfinite values or oversized responses. Native JSON schema is optional.
Requested identity is not served identity: unresolved model/effort observations
fail the audit rather than silently treating a default or a startup alias as
provider attestation. A CLI without an independently observed effort control
remains an explicit evidence gap; a registry's effort label is not proof of one.

Accounting requires the original run ID, exact attempt count, no hidden retries,
and measured charges for every real shared account pool. Estimates, invented
per-model pools, unknown usage, missing artifacts and another run's charges fail.
The fixture's `synthetic_calls` are fabricated test input, never real usage.
No cost/quota number for an actual provider is asserted by these fixtures.

**Trust limit:** hashes bind content; they do not authenticate a collector or
prove confinement. Imported observations and `provider_observed` labels require
independent verification of the collector, executable, launch config, actual
blocked side effects, final-turn provenance, provider identity and usage source.
Even a complete imported capture returns `independent_review_required` and
`activation:false`. Relabeling a fixture cannot grant production authority.
Runtime launch must still rehash the uncached executable, check deployment
confinement and acquire the existing real shared pools through core-owned code.

## Reproduce without a provider

From the repository root:

```sh
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q tests/test_transport_qualification.py
python3 src/transport_qualification.py --selftest
python3 src/transport_qualification.py \
  --profile tests/fixtures/transport_qualification/profile.json \
  --suite tests/fixtures/transport_qualification/classification-suite.json \
  --capture tests/fixtures/transport_qualification/classification-capture.json \
  --role bounded_classification
python3 src/transport_qualification.py \
  --profile tests/fixtures/transport_qualification/profile.json \
  --suite tests/fixtures/transport_qualification/adjudication-suite.json \
  --capture tests/fixtures/transport_qualification/adjudication-capture.json \
  --role strong_criterion_adjudication
```

Plugin autoload is disabled because the host's unrelated pytest-rerunfailures
plugin attempts a socket bind. The repository's own fixture-shipping guard still
runs. Fixtures must be staged/tracked so they ship in the execution mirror.
These checks need one short Python process; no fleet regression, provider or
mirror deployment is necessary to reproduce the checkpoint.

## Candidate inventory and next evidence

`tests/fixtures/transport_qualification/candidate-inventory.json` freezes all 14
candidate rows, exact known model IDs and registered profile/effort/adapter/shared
pool metadata from the source commit. Muse Spark 1.2/1.3 IDs reflect the approved
local-profile observation; entitlement is unknown and no active registry entry
is invented. The inventory carries no role qualification or dispatch authority.

| Pool | Candidate interest | Current evidence gap |
| --- | --- | --- |
| Low | Codex Luna/Sol, Claude Haiku/Sonnet, Gemini Flash, Cursor Composer, Vibe Medium 3.5, Muse Spark 1.2/1.3/Wonder | Actual candidate outputs, verified launch confinement and identity/effort, measured shared usage |
| Strong | Opus 5.5/Sonnet, Sol/Astra, Argon/Wonder and alternatives after qualification | Named-criterion reliability and independent suitability evidence |
| Intermediate | Luna high | Intermediate only; does not become the strong default |

There is no routine Sonnet cap in this target inventory. Cursor ask/plan alone
cannot satisfy tool-free confinement. Vibe wildcard denial requires clearing any
conflicting allowlist in the isolated configuration; no global settings change
is authorized. Muse profile resolution does not establish entitlement. All
candidates are ready for **offline capture-format testing**, not activation.

Before any subscription evaluation is proposed, obtain read-only exact CLI
metadata and measured available shared quota, verify the isolated transport
controls with local canaries, freeze a reviewed suite, and state a numerical
provider/quota ceiling. A possible first batch is one candidate, one exact
model/effort/role, four frozen cases, at most four submissions, zero retries,
at most 4 KiB input and 2 KiB output per case (16 KiB input/8 KiB output total).
These byte/call bounds are not a provider-token or quota estimate. Paid/API
spending ceiling is $0; do not launch if the subscription path or numerical
quota ceiling cannot be established. No live evaluation is authorized or run by
this module. Future evidence remains a separate reviewed deliverable before
core can consider a production record and deployment activation.

## Validation checkpoint

At the frozen core API base, the new harness plus existing
`test_closer_role_qualification.py`, `test_closer_role_runtime_probe.py`, and
`test_offload_data_only.py` passed **269 tests in 7.07 seconds**. The existing
transport tests use local fake CLI binaries; this is not live provider evidence.
Ruff, Black check, focused mypy (`--follow-imports=silent`), module `--selftest`,
and staged whitespace checks passed. No full regression or production mirror
sync was run. The canonical Code-root audit ran report-only; this worktree is
under `/tmp` and no root cleanup was performed.

The core branch was rechecked remotely at the exact source SHA above. Hosted
checks/independent review of this new harness and integration after #940 remain
separate delivery gates. No PR is opened by this checkpoint.
