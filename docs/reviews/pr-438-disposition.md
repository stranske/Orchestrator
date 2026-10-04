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

## Complete source retrieval

Authenticated GitHub connector reads retrieved the PR metadata, complete paginated
changed-file list, Git commit, and untruncated recursive tree. The commit binds the head to
tree `94bdda87c056fc7d8d07497fbb5c6915ed83ae82`. The terminal could not contact GitHub,
and the head commit is not in the local object database. The individual blobs are present,
however, and were retrieved by object ID, not by working-tree path or the squash commit.

[Source bindings](pr-438-source-evidence.json) retain all 14 changed paths and eight
supporting paths: Git blob ID, Git mode, byte count, SHA-256, and immutable exact-head
source URL. Each retrieved blob matched both its authenticated size and Git object hash.
The changed files total 570,728 full-file bytes; all 22 files total 689,871 bytes. These
are full-file byte counts, not a claim about the comparison's changed-code character count.

The complete byte artifacts and original exported metadata were retained locally under
`/tmp/pr438-bound-source-evidence/`. That scratch directory is ephemeral. Durable
file/blob bindings and authenticated GitHub source URLs are committed here; the scratch
artifact names in the manifest are relative to that directory. Remote content remains
retrievable by the retained blob IDs after scratch cleanup.

`scripts/capture_review_source_evidence.js` accepts an authenticated metadata export
containing `repository`, `pr_number`, `pull_request` (`head_sha`, `changed_files`),
`commit`, `tree`, `changed_paths`, and `required_paths`. `retrieval_transport` and
`source_urls` record provenance. It does not authenticate an arbitrary export itself.
The caller must obtain the export through authenticated GitHub access. It refuses mismatched
head/tree bindings, truncated trees, and a path list that disagrees with the PR's file count.
Incomplete or corrupt bytes are UNKNOWN with an owner and next action. Retrieval success
always leaves review PENDING and deployment NOT_OBSERVED.

Reproduce retention with the exported metadata and local Git object database:

```bash
node scripts/capture_review_source_evidence.js \
  /tmp/pr438-source-metadata.json . /tmp/pr438-new-source-bundle \
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

The exact-head floor has `collected=1566`, base test skips 26, provisioned-mirror skips 21,
and bare-mirror ceilings 47/7/2. The PR's reported 1464 collection count and the break logs
describe the earlier `57a828d` revision. They are not the final head's collection measurement.

## Remaining UNKNOWN evidence and next actions

| Evidence | Disposition | Owner | Next action |
| --- | --- | --- | --- |
| Full exact-head regression suite, including documented copier installer and pre-sync refusal paths | UNKNOWN; pytest is absent and PyPI DNS resolution failed | `imi-merge-verify-closer`, operated by stranske through the existing Sol lane | Run the command below against a clean tree whose relevant files match the manifest; retain exact-head test output |
| Recorded provisioned-machine copier/builder witness | PR body available as an author claim at `57a828d`; no independent machine receipt/log artifact retrieved | stranske, provisioned-machine operator | Supply the retained scratch-run log/receipt and copier bytes bound to that revision, or reproduce against the final head; retain bytes and permissions comparison |
| Installed deployment | NOT_OBSERVED | stranske, provisioned-machine operator | If deployment is intended, perform the documented owner step and capture installed observations; do not infer it from these scratch runs |
| Final independent Sol adjudication and complete changed-code inspection | UNKNOWN; this collection/focused witness run does not establish that lane's review | `imi-merge-verify-closer`, operated by stranske through the existing Sol lane | Review all bound source inputs and the evidence dispositions, retain a durable decision linked to the original comparison, and route demonstrated defects through normal repair gates |

```bash
pytest tests/test_build_exec_mirror.py tests/test_exec_mirror_shape.py \
  tests/test_verify_before_sync.py tests/test_install_verified_snapshot.py \
  tests/test_mirror_sync_patch.py tests/test_mirror_generations.py \
  tests/test_verify_private_state.py tests/test_verify_parallel_selftests.py \
  tests/test_verify_coverage_mode.py -m "not slow"
```

No product defect was demonstrated by this follow-up. The comparison's truncation concern
is resolved for source acquisition, with the review and machine evidence gaps explicitly
retained. The overall acceptance criteria remain open pending the named actions above.

## Delivery limitation for this run

The workspace's `.git` is read-only, so the source/test/evidence change was committed in
an isolated repository at `/tmp/orch-pr438-evidence-commit`, with PR #445's current head
`b78a7f8dd474138ee5dc34b64277a3ebd845bde3` as its parent. Authenticated connector publication
was rejected: GitHub writes require approval, while this session's approval policy is
`never`. No remote commit or PR-body update was made. The verified checkboxes above describe
local evidence, not a remote acceptance update. Stranske or the next worker with Git write
access must publish the retained commit/patch and update only those verified task boxes.
