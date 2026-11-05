# Native original-input capture selection

Native terminal, metrics and closure artifacts are observations of the verifier
lifecycle. They do not each promise an original input manifest. The normative
input transport is exactly one member of the existing producer families
`comparison-results-<run>` or `evaluation-results-<run>`. Its name selects a
candidate only; existing run, repository, attempt, evaluated revision, archive,
manifest, source and retained-byte checks still authenticate the contents.

The authoritative NON_PASS reader now uses that same family selector as the
existing diagnostic reader. All bounded auxiliary metadata remains separately
retained, with `capture_requested=false` and `proof_status=not-native-input-proof`.
An unavailable auxiliary artifact never becomes fulfilled evidence. Missing or
ambiguous captures, incomplete inventory, corrupt canonical transport, missing
required members and source mismatches remain held. Numeric caps and action
priority are unchanged. Explicit task/proof obligations elsewhere are unchanged.

The producer also emits two different documents. `verifier-input-manifest.json`
is the transport snapshot (context/diff identities and source run).
`verification-event-manifest.json` contains the request, material and event.
The latter is now a distinct canonical receipt, with duplicate/path protections.
Corrective input joining requires exactly one event manifest beside the seeds
and report in the same selected archive. It recomputes request/material digests
and compares the validated event, preserving the input snapshot's separate run
and merge checks. No event metadata is invented in the input snapshot.

Actual retained run40620861168 reproduced the old hold at unchanged caps: one
complete comparison capture plus three auxiliary artifacts yielded PARTIAL.
The real collection action already prioritized native inputs correctly. After
this source repair, the actual collector and original-input bridge recover all
11 raw findings and native mappings. Original diagnostic receipts remain
unchanged. This read-only join is not queue admission, a role run, corrective
coding, publication, risk clearance or acceptance authority.

`closer_evidence.py` belongs to the selected model-source contract. This repair
therefore needs one fresh physical role callback after the combined source
freeze; an older qualification must not be rebound. No new model-quality
campaign, provider request, transport limit or action authority is introduced.
