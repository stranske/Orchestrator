# PR #438 exact-head evidence disposition

Source retrieval is complete. Independent review remains **UNKNOWN** for the full
regression suite and the provisioned-machine observation. This is not a PASS or a
product-defect finding. Deployment was not performed or observed.

The original [provider comparison](https://github.com/stranske/Orchestrator/pull/438#issuecomment-5984380511)
reported CONCERNS after receiving only 57,029 of 264,363 changed-code characters.
Its machine-readable record names evaluated merge `6b625fc3aead91a1c4a0294847b3a761ac8cb5ad`
and PR head `883ee0b84f5bd5b3bbee7c85c586004aafd58e5f`. This follow-up binds source to
that PR head, rather than to today's working tree or main.

## Verified task progress

- [x] Retrieve complete exact-head builder, pre-sync verifier, shape/ceiling implementation,
  installer, documented copier, floor, and relevant tests; retain file/blob bindings.
- [x] Inspect deliberate-break runs 37228770087 and 37228771698 and the recorded
  provisioned-machine copier/builder witness; explicitly identify unavailable evidence.
- [ ] Complete independent verification through the existing Sol lane and the focused
  regression suite, including the provisioned-machine witness.
- [ ] Complete final adjudication after the remaining UNKNOWN evidence is resolved;
  route any demonstrated defect through a bounded repair PR and ordinary gates.

Acceptance reconciliation: the criterion permitting unavailable coverage to be explicitly
UNKNOWN with an owner and next action is satisfied by the table below. The durable-link and
no-unobserved-deployment-claim criterion is also satisfied. Validation of the complete
implementation remains open pending the independent review; focused witnesses alone do not
complete that criterion.

## Complete source retrieval

Authenticated GitHub connector reads retrieved the PR metadata, complete paginated
changed-file list, Git commit, and untruncated recursive tree. Fresh recovery reads retained
a [credential-free metadata export](pr-438-source-metadata.json), including all 2,165 tree
entries and all 14 PR-file records (one page at `per_page=100`). The commit binds the head to
tree `94bdda87c056fc7d8d07497fbb5c6915ed83ae82`. The terminal could not contact GitHub,
and the head commit is not in the local object database. The individual blobs are present,
however, and were retrieved by object ID, not by working-tree path or the squash commit.

[Source bindings](pr-438-source-evidence.json) retain all 14 changed paths and eight
supporting paths: Git blob ID, Git mode, byte count, SHA-256, and immutable exact-head
source URL. Each retrieved blob matched both its authenticated size and Git object hash.
The changed files total 570,728 full-file bytes; all 22 files total 689,871 bytes. These
are full-file byte counts, not a claim about the comparison's changed-code character count.

The original metadata export and byte artifacts were retained only under the ephemeral
`/tmp/pr438-bound-source-evidence/`. That original export is unavailable; its former
`metadata_sha256` (`6ad0b3dccdf25b5441210aa97a24d2836365779661f5e99c38da11c61813c04a`)
cannot reconstruct its contents. The fresh normalized export supersedes that acquisition
receipt, without claiming to recover the original export. The manifest now hashes the
committed export's exact bytes. Re-capture retrieved all 22 blobs and left every file/blob
binding unchanged. The export retains only PR/head/count, commit/tree identity, tree records,
PR-file records, requested paths and provenance URLs; it contains no request headers or
authentication material. Scratch blob artifact names remain relative to the output bundle.
Remote content remains retrievable by the retained blob IDs after scratch cleanup.

`scripts/capture_review_source_evidence.js` accepts an authenticated metadata export
containing `repository`, `pr_number`, `pull_request` (`head_sha`, `changed_files`),
`commit`, `tree`, `changed_paths`, `required_paths`, and `pull_request_files` (same `head_sha`,
`complete: true`, and aggregated `files` records from every authenticated PR-files page).
`retrieval_transport` and
`source_urls` record provenance. It does not authenticate an arbitrary export itself.
The caller must obtain the export through authenticated GitHub access. It refuses mismatched
head/tree bindings, truncated trees, and a changed-path set that differs from the complete
PR-file records, even when the file count agrees. Missing/incomplete file records are refused
before creating an output bundle. The exporter must obtain every page and confirm head identity;
the collector checks the declared bindings and exact set equality, not network authentication.
Incomplete or corrupt bytes are UNKNOWN with an owner and next action. Retrieval success
always leaves review PENDING and deployment NOT_OBSERVED.

Reproduce retention with the committed export and local Git object database:

```bash
node scripts/capture_review_source_evidence.js \
  docs/reviews/pr-438-source-metadata.json . /tmp/pr438-new-source-bundle \
  883ee0b84f5bd5b3bbee7c85c586004aafd58e5f
```

## Deliberate-break evidence

[Authenticated run/job records and log excerpts](pr-438-break-evidence.json) independently
confirm the two demonstrations. Authenticated commit reads confirm each break is one commit
above `57a828d34a60fd5664b7de086c49eb5c1d5c7987`, an earlier revision than the final head.
They validate the historical refusal behavior, not the final head's full acceptance.

| Run | Flat-mirror job | Checkout job | Observed refusal |
| --- | --- | --- | --- |
| [37228770087](https://github.com/stranske/Orchestrator/actions/runs/37228770087) | Failed, job 111513828615 | Succeeded, job 111513828644 | Missing `mirror/src/mirror_reader.py`; exit 1 |
| [37228771698](https://github.com/stranske/Orchestrator/actions/runs/37228771698) | Failed, job 111513833680 | Succeeded, job 111513833519 | All 1,417 executed tests passed, then deployment-owned bytes changed; VOID, exit 3 |

The B run is direct evidence that passing tests alone cannot establish a valid deployment
snapshot. Both logs report the bare shape and ceilings 47/7/2.

## Focused independent witnesses

- The new collector passed 15 Node tests, including real Git objects, working-tree drift,
  truncation, equal-length corruption, omitted files, unknown evidence, and output preservation.
- The existing deployment evidence collector passed all 18 Node tests.
- The six existing `test_mirror_generations.py` unittest cases passed. Hash checks bound
  that test, its installer, `paths.py`, and `mirror_reader.py` to the retrieved head.
- Stdlib-only witnesses of the hash-bound `env_prereq.py` and `verify.py` checked all three
  shape selections, each present machine mark, a raised probe, limits 26/21/47, and agreement
  between ceiling enforcement and rendering.
- A scratch synthetic repository executed the exact-head builder, installer ownership
  inspector, pre-sync identity function, and documented copier. All 24 shipped fixture files
  were installer-owned. All 13 inputs found by editing candidates and rebuilding changed
  source identity. `AGENTS.md` and `ORCHESTRATOR.md` participated in both checks. The copier
  matched builder bytes and permission bits, excluding only its Workflows registry. `gh`
  was stubbed with an empty registry; this does not validate the live registry fetch or the
  installed copier.
- The follow-up adds four executable Node contract witnesses in
  `tests/test_exec_mirror_contract_witness.js`, using Python's standard library and synthetic
  repositories. [Retained output and source binding checks](pr-438-contract-witness.json)
  record **4 passed, 0 failed, 0 skipped** against the complete bound inputs. All 22 manifest
  bindings matched before and after the run. The current checkout's floor had moved, so its
  historical blob was restored only in an isolated scratch checkout. The witnesses independently
  mutate source candidates, compare copier bytes and modes, test every present/unreadable
  machine mark, and check all three shape selections and each defined ceiling boundary. They
  validate the configured ceilings, not a final-head whole-suite measurement. The provisioned
  mirror's omitted selftest/gate ceiling keys correctly fall back to the base floor values.
  Existing source and deployment collectors also passed their 15 and 18 Node tests.
- The next follow-up adds actual scratch installation and refusal witnesses, recorded in
  [the exact-head installation receipt](pr-438-install-witness.json). All six contract witnesses
  passed with no skips. The installer preserved every builder-shipped byte and permission bit,
  replaced stale `AGENTS.md` and `ORCHESTRATOR.md`, removed retired modules and shipped docs,
  and preserved runtime reports and markers. Mutating retained snapshot bytes or executable
  permissions was refused before changing the destination or runtime registry. Publication
  happened only in disposable synthetic trees, with `--unverified` on the successful install.
- `scripts/review_contract_inputs.js` makes the historical witness run repeatable without
  rewriting the checkout floor. It verifies all 22 complete Git objects against the manifest,
  requires the executable inputs to be included, checks the other 21 working-tree inputs and
  their Git executable modes before and after the run, and supplies the floor directly from
  its bound object. Authenticated commit/tree/changed-file responses were re-read and matched
  every manifest binding. Fifteen checker tests passed, including byte/mode drift, symlinks,
  corrupt or unavailable objects, invalid paths, omitted inputs, and historical-floor handling.
  The existing 33 collector tests and six generation unittest cases also passed. No final-head
  whole-suite collection/skip measurement or provisioned-machine observation was added.

The exact-head floor has `collected=1566`, base test skips 26, provisioned-mirror skips 21,
and bare-mirror ceilings 47/7/2. The PR's reported 1464 collection count and the break logs
describe the earlier `57a828d` revision. They are not the final head's collection measurement.

## Remaining UNKNOWN evidence and next actions

| Evidence | Disposition | Owner | Next action |
| --- | --- | --- | --- |
| Full exact-head regression suite, including documented copier installer and pre-sync refusal paths | UNKNOWN; pytest is absent and PyPI DNS resolution failed | `imi-merge-verify-closer`, operated by stranske through the existing Sol lane | Install pytest in the existing Sol lane, run the full exact-head pytest regression suite against a clean exact-head checkout, and retain output including collection and skip measurements; the nine-file focused command below does not resolve this UNKNOWN |
| Recorded provisioned-machine copier/builder witness | PR body available as an author claim at `57a828d`; no independent machine receipt/log artifact retrieved | stranske, provisioned-machine operator | Supply the retained scratch-run log/receipt and copier bytes bound to that revision, or reproduce against the final head; retain bytes and permissions comparison |
| Installed deployment | NOT_OBSERVED | stranske, provisioned-machine operator | If deployment is intended, perform the documented owner step and capture installed observations; do not infer it from these scratch runs |
| Final independent Sol adjudication and complete changed-code inspection | UNKNOWN; this collection/focused witness run does not establish that lane's review | `imi-merge-verify-closer`, operated by stranske through the existing Sol lane | Review all bound source inputs and the evidence dispositions, retain a durable decision linked to the original comparison, and route demonstrated defects through normal repair gates |

The following nine-file command is focused regression verification. The full-suite action
above requires the entire exact-head suite with `pytest -m "not slow"`, including retained
collection, deselection and skip counts; it is not satisfied by this focused command.

```bash
pytest tests/test_build_exec_mirror.py tests/test_exec_mirror_shape.py \
  tests/test_verify_before_sync.py tests/test_install_verified_snapshot.py \
  tests/test_mirror_sync_patch.py tests/test_mirror_generations.py \
  tests/test_verify_private_state.py tests/test_verify_parallel_selftests.py \
  tests/test_verify_coverage_mode.py -m "not slow"
```

Reproduce the six focused witnesses against the retained historical source bindings:

```bash
ORCH_CONTRACT_SOURCE_MANIFEST=docs/reviews/pr-438-source-evidence.json \
ORCH_CONTRACT_EXPECTED_HEAD=883ee0b84f5bd5b3bbee7c85c586004aafd58e5f \
node --test --test-isolation=none tests/test_exec_mirror_contract_witness.js
```

This command requires the manifest's blobs in the local Git database and matching working-tree
inputs. It refuses drift instead of silently testing a newer implementation. Ordinary CI can
run the same test without those two variables to check the current implementation.

No defect in #438's product implementation was demonstrated by this follow-up. The comparison's truncation concern
is resolved for source acquisition, with the review and machine evidence gaps explicitly
retained. The overall acceptance criteria remain open pending the named actions above.

## Earlier delivery limitation

The earlier acquisition and four-witness changes are published in PR #445 at
`3b471dc19cc9f35fc393f2c0e7311f8c36c26751`. This follow-up rechecked all 22 local blobs against
fresh authenticated exact-head metadata and re-read both deliberate-break job lists. The
provisioned-machine claim still has no independently retrieved machine receipt.

The workspace's `.git` is read-only. The follow-up code/test/evidence change is retained in an
isolated repository at `/tmp/orch-pr438-install-review`, based on that published head.
Authenticated PR-body reconciliation was blocked: GitHub writes require approval, while
this session's approval policy is `never`. The proposed body is retained at
`/tmp/pr445-reconciled-body.md`, checking the first two tasks and the UNKNOWN-with-owner and
durable-link acceptance criteria in both lists (8 of 14 checkbox occurrences).
No remote acceptance update was made in that earlier run. Stranske or the next worker with Git write access
must publish the retained commit/patch and apply that verified reconciliation. PR #445
was observed open and ready for review. The independent Sol review remains UNKNOWN.

## Current-head review recovery

The [opener's review recovery request](https://github.com/stranske/Orchestrator/pull/445#issuecomment-5985449089)
identifies two active CodeRabbit findings at `720cd8e873d824c83b8d3862aac954902bb61dfe`.
Both were valid on inspection:

- [Metadata retention](https://github.com/stranske/Orchestrator/pull/445#discussion_r4179529427):
  repaired by the fresh committed export and re-captured manifest described above. The
  original scratch export remains unavailable; the fresh receipt is independently retained.
- [Changed-path set equality](https://github.com/stranske/Orchestrator/pull/445#discussion_r4179529429):
  repaired by comparing `changed_paths` with the complete authenticated PR-file records.
  The same-count substitution witness failed before the code repair and passed afterward.
  Regressions also cover absent/incomplete exports, wrong-head records, omitted/duplicate
  records, invalid filenames, ordering and durable metadata/hash/tree reproduction.

[Focused repair proof](pr-445-review-recovery.json) retains the inspected head, tested input
hashes and command; [the test output](pr-445-review-recovery-tests.txt) records **63 passed,
0 failed, 0 skipped**, including all six historical contract witnesses. The fresh collector
run retained **14/14 changed files** and preserved all **22** existing file/blob bindings.

The previous recovery's delivery record is historical. Its source/test/evidence repair
was subsequently published by keepalive in `187a717bd590ea1da78e7d11deba9aa5893ee245`.
Authenticated revalidation at that head confirms **both originating threads are resolved
by CodeRabbit**, each with an explicit addressed disposition for commits `720cd8e` through
`187a717`. No thread was self-resolved by this run. These acquisition repairs do not supply
the remaining full-suite, provisioned-machine, or independent Sol evidence.

## Revalidation at 187a717

[The new revalidation receipt](pr-445-review-revalidation.json) retains the authenticated
thread snapshot, exact tested input hashes and [65-test output](pr-445-review-revalidation-tests.txt).
The new tests reject a same-count substitution in the actual retained 14-file metadata
receipt before reading source or creating output, and replay all 22 historical Git objects
into a complete bundle identical to the retained manifest. They verify every retained
artifact's size, Git object identity and SHA-256. Review remains PENDING and deployment
NOT_OBSERVED in that source manifest; source acquisition is not independent adjudication.

The third [full-suite action finding](https://github.com/stranske/Orchestrator/pull/445#discussion_r4179590720)
was marked addressed by CodeRabbit at `720cd8e`, but current-head inspection found its
requested correction absent from both the contract receipt and this disposition. This
pass corrects both actions to require the entire exact-head suite and identifies the
nine-file command as focused. Renewed originating-reviewer disposition for this correction
remains PENDING. The whole-suite and machine observations remain UNKNOWN.

PR-body reconciliation was rejected by automatic approval review: GitHub writes require
approval and this session's approval policy is `never`. The proposed reconciliation reopens
the six duplicated checkboxes for independent/final validation steps unsupported by the
retained evidence; the other eight prior checked occurrences remain verified. The current
review task stays unchecked pending the corrected-action disposition. Owner `stranske`
must publish this pass's retained commit/patch and apply the proposed PR-body reconciliation.

## Installation receipt revalidation at d962843

The active [installation-manifest finding](https://github.com/stranske/Orchestrator/pull/445#discussion_r4179759913)
is valid: the installation receipt named the later re-exported source manifest while retaining
the original manifest's digest. This pass preserves the original bytes as
[the installation source manifest](pr-438-install-source-evidence.json), retrieved from the
published `720cd8e` Git blob `95c6124f60ccec9c15b5b74ce8aa7d0fa496765e`. Its SHA-256 is exactly
`97f48f31fbd8246948b405b376ce7481b3ed8a4e7a575320a30685ec494f46a5`, as recorded by the original
installation witness. All 22 source bindings match the later manifest; only the acquisition
receipt changed. Retaining these manifest bytes does not recover the original scratch metadata
export or replace the fresh normalized metadata receipt.

The installation receipt now names that retained file and its immutable provenance, preserving
the historical run output and input hashes. Its separate reproduction command pins both the
source head and manifest digest. The witness runner refuses a missing or replaced manifest
before executing any contract test. [A fresh six-witness run](pr-445-install-manifest-tests.txt)
passed with **6 passed, 0 failed, 0 skipped** using the retained historical manifest and current
runner. [The review regression run](pr-445-manifest-binding-tests.txt) also checks receipt bytes,
equivalent JSON with different serialization, and digest validation before source reads.
[The revalidation receipt](pr-445-manifest-binding.json) binds these runs to the exact inputs.

The remaining UNKNOWN action in the installation receipt also now requires the full exact-head
suite, aligning it with the earlier full-suite correction. That earlier thread's addressed
disposition predates the actual correction; renewed originating-reviewer disposition remains
pending for both this alignment and the installation-manifest repair. The two acquisition
findings remain resolved by CodeRabbit. No thread was self-resolved. Full-suite, machine and
independent Sol evidence remain UNKNOWN; installed deployment remains NOT_OBSERVED.

The corrected PR body reopens the six unsupported checkbox occurrences and leaves the current
review task unchecked. Its remote update was rejected by automatic approval review because
GitHub writes require approval and this session's approval policy is `never`. The proposed body
is retained at `/tmp/pr445-manifest-reconciled-body.md`. Workspace staging was refused because
`.git` is read-only; the source/test/evidence commit and patch are retained in
`/tmp/pr445-manifest-binding` and `/tmp/pr445-manifest-binding.patch` for publication.


## Independent Sol closeout, 2026-10-05

This section supersedes the remaining UNKNOWN actions above for this audit scope.
The configured Reviewed Repo Merge Verify Closer independently validated all 22 historical
source bindings against a clean checkout at `883ee0b84f5bd5b3bbee7c85c586004aafd58e5f`.
[The full suite](pr-445-sol-full-pytest.txt) ran 1,566 tests: all passed, zero skips, four subtests.
[Production copier verification](pr-445-sol-copier-verification.txt) on this machine reproduced
the final-head witness using private copied state: VERIFIED, 1,545 passing tests, 21 named
git-prerequisite mirror skips within the unchanged ceiling, 99 selftests and five green gates.
The source identity is `7db69330306b761604dc715614f3850a61962482`.
[Byte and mode inventory](pr-445-sol-copy-contract.json) compares the installed copier with
the historical builder: all 1,519 source leaves match, excluding only the instance registry.
The standalone copier also refreshed the instance registry; it did not install the live mirror
or write the Brain or capability ledger. This is recorded separately from the isolated verifier.

[Independent adjudication](pr-445-sol-disposition.json) covers builder/installer ownership,
source identity, copy equivalence and conservative machine classification. The original
comparison's missing-context finding is dispositioned with complete inputs and measured
replacement evidence. No product defect was observed in this bounded audit scope.
Installed deployment was not performed; source #389's actual publication acceptance remains
with PR #414 and is not closed by this evidence audit. PR #445 still requires current-head
CI, expected-check reconciliation, zero active threads, the seven-minute floor, guarded
squash and post-merge comparison before source #444 terminal disposition.
