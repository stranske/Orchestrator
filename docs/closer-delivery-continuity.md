# Delivery continuity and verifier identity

A delivery objective continues after its PR merges. A closed source issue or a
successful workflow job is not, by itself, final source acceptance.

## Preserve admission through merge

`src/closer_admission_continuity.py` plans additional observations for previously
owned deliveries that disappear from the controller's open-only inventory. Its
locators live in the existing demand state. They are candidates for fresh reads,
not assignments, action permissions or synthetic completed operations.

For each locator, the adapter reads the actual source and delivery PR, checks the
current source association, head and merge identity, and calls the controller's
existing owner classification and routing functions. Current pauses, challenge
and retry ownership, suppression, diagnosed fingerprints and normal parking still
apply. An alias cannot acquire Closer ownership when its actual delivery is routed
to an escalator or ignored. Failed reads retain a per-item requirement and retry
trigger; unrelated open work continues.

A new comparison-report body can wake a previously diagnosed delivery. This is a
material-evidence signal, not a verdict or acceptance certificate. Discussion
activity alone does not manufacture work. The production adapter conservatively
retains unresolved objectives; it does not infer final resolution from source
closure or workflow success.

`scripts/render_closer_admission_hook.py` produces a distinct staged candidate for
the existing local demand router. It checks the expected source shape and pins the
helper bytes by SHA-256. The candidate integrates inside the router's existing
lock and normal assignment publication. A helper-integrity failure parks affected
continuity rows while preserving ordinary open-item routing.

Rendering does **not** publish the controller, change an automation setting, enroll
a locator, or install a runtime. Adoption requires review of the staged diff and
its exact helper path/hash. Keep the original router bytes and hash for rollback.
A runtime mirror update alone does not activate this external controller hook.
No schedule, status, credential, permission or model-policy change is required by
the source adapter.

For reviewed backfill, `plan_enrollment` hashes the existing state and retained
provenance references. `apply_enrollment` revalidates those bytes and applies an
exact compare-and-swap under `.demand.lock`. It writes only locators into the same
demand state. Fresh owner and identity checks still precede canonical admission.
Neither function authorizes action replay or historical fence removal.

## Bind verifier evidence to its delivery

`src/closer_verifier_boundary.py` distinguishes the subject PR and merge commit
from the verifier runtime's own workflow head. Timestamp proximity and an empty
workflow `pull_requests` list do not establish a subject.

Unlinked manual runs require a unique, authenticated input manifest from the
existing capped artifact reader. The manifest must bind the repository, PR,
merge commit, server run and attempt. Optional reports and enrichment are not
required merely to establish identity. Contradictory or incomplete identity is an
explicit recoverable read requirement; positively identified foreign runs do not
block the subject's verification request.

History reads follow the existing workflow endpoint through the merge-time cutoff.
An acquisition limit bounds new archive downloads per observation; verified retained
archives preserve progress on the next observation. Incomplete or unstable pages
remain recoverable reads. Repeated observations retain archive provenance without
writing another copy of unchanged polling payloads.

A proven same-delivery result returns to the existing evidence collection path.
Collection seals diagnostic evidence; it does not fabricate PASS, source
acceptance, a merge result, or a completed verification response judgment. Server
identity and artifact bindings are checked again at consumption.

Before any verification POST, incomplete reads and resource deferrals retain
native proof that the verification POST has not started. Their retry trigger
records the actual missing read, reset or owned request boundary. This is distinct
from a timeout after a POST: uncertain effects retain the normal UNKNOWN barrier.
No broad historical reconciliation or global fence deletion is introduced.

For retained attempts made before typed read proofs existed, the existing finite
reconciliation API recognizes only reviewed, version-pinned pre-write return
contracts. It verifies the installed source/package, original lifecycle and inner
operation receipts, and the retained source-to-delivery identity. Applying such a
plan terminalizes that proven no-effect attempt as HELD; it does not set READY,
replay an action, reinterpret foreign results or remove unrelated reservations.
A later action still requires a normal fresh observation and admission.

## Current review boundary

The merge screen counts current unresolved review threads, matching the exact-head
merge guard. Outdated thread data remains in the evidence packet, but its presence
alone does not route an otherwise green PR back to review. Current unresolved
threads, required check failures, conflicts and explicit pauses remain blockers.

A generic `needs-human` label is not itself a decision or an explicit pause.
Normal evaluation continues unless a current scoped owner disposition, assignment
hold or explicit pause applies. For a source-target invocation, that disposition
lookup includes the actual delivery PR as well as the source identities.
