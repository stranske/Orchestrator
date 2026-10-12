# Existing PR498 ingestion review repair

Recovery starts from remote58c4a36d5e4c06e2bed96727f0e58fd6536e8701.
The latest keepalive completion had left committed floor conflict markers,
on-demand marker-table DDL, a telemetry-preserving early return and digest-only
identity validation. These are code defects; they require no owner waiver.

The new regression cases fail on the original source: **6 failed /17 passed**:
connection-before-first-ingest; transportA followed by verifiedB telemetry;
four valid-digest artifact content mismatches (provider,model,profile,missingmodel).
The repair registers DDL in the connection schema, rewrites rows through the
existing transactional writers before sealing, and requires parsed artifact
profile/provider/model content to match the provider-resolved attempt.

The artifact remains evidence supplied by the designated collector; matching
contents and a digest do not constitute independent cryptographic issuer
attestation. The remote bridge's authenticated collection contract still applies.
Malformed JSON, unreadable files, oversized artifacts and mismatched fields
fail closed. No trial was ingested into the live Brain; tests use quarantine DBs.

Seven quality-rejection cases (nonmapping,unknownprofile,bool,nonnumeric,NaN,
infinity,out-of-range) were exercised against an actual validation bypass;
**7 failures** resulted. Fixed source restored byte-identically; the ingestion
module then passed **23 tests**, and the four focused modules passed **56**.

Full `python3.12 src/verify.py --update-floor`: **2366 tests passed**,
**103 module selftests**, **5/5 gates**; private ledger/Brain deleted by verifier.
Floors use actual collection/pass counts; all skip/type ceilings are unchanged.
Black/Ruff and touched-source mypy pass. Complete transcripts accompany this note.

The existing branch remains the delivery lane. Hosted CI, expected topology,
current active reviews and seven-minute floor remain required after push.
This source repair does not claim source431's actual provider trial, installed
mirror publication, cost/quality comparison or deployment acceptance.
