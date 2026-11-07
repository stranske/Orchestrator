# Issue #645 acceptance evidence

Check date: 2026-11-03. Refs #645; routing-unit tracker #575.

Outcome 1: all seven configured agents appear in the historical capacity artifact,
published no later than 61 seconds after #657 merged (24-hour requirement met).
The focused suite passes 76/76; omitting the emitted capacity block produces a
named presence failure, and byte-identical restoration passes.

Acceptance command: `/opt/anaconda3/bin/python3 -m pytest tests/test_route_weights_export.py -q`.
Stop rule: two acceptance-repair attempts or 45 minutes; stop at the first broken
step if either limit trips. No production emitter repair was needed.

- [x] Inspect the inherited emitter and complete acceptance-test assertions.
- [x] Capture exact-head focused-test and deliberate-break results.
- [x] Independently inspect the historical published artifact and establish whether its publication met the 24-hour requirement.
- [ ] Retain successful exact-head CI verification evidence (final head and result links are recorded in the PR body after CI completes).

## Inherited implementation and bounded change

Base main: `12e7ba00f07326213080edf64e85e2717ba0185b`.
Test/evidence revision: `14cb40ab513c20be998e087d0bcbf0a4d1285f77`. The final documentation-only
commit retains exactly these emitter, producer and test bytes; its exact SHA,
focused rerun and successful CI verdict belong in the PR body (avoids a
self-referential commit hash in this file).

No selected repository virtual environment exists in this checkout. The owner
fallback `/opt/anaconda3/bin/python3` reports Python 3.12.2. No Apple Python was used.

Inspected `src/route_weights_export.py`, `tests/test_route_weights_export.py`, and
`src/capacity.py`. `build_document()` calls `_current_capacity_snapshot()` once
when no snapshot is supplied; that helper calls `capacity.build()` in process.
The producer computes each configured agent and supplies `generated_at`.
`_capacity_block()` iterates `capacity.AGENTS`, copies valid producer states and
windows, and uses the snapshot clock for `observed_at`. Existing regressions cover
stale `capacity.OUT`, the live shed marker added in #657, v1 schema/key preservation,
initial addition, state changes, observation-only changes and malformed entries.

The only test change completes `test_export_includes_capacity_block`: compare
all agent entries with a deterministic shed snapshot, distinguish observation 42
from export time 100, and verify the capacity block and schema survive
`write_document()` plus JSON readback. No tests were added, so no floor change is needed.
The exporter and #657's regression remain unchanged.

Later merged commit `63caaa1804c913f564b424f1aa3d10ef6f1231d0` (#767, implementing
#668) intentionally changed publication to state/pacing triggers with two-tick
damping. Current tests explicitly suppress window-only changes. The older #645
wording that promises window-triggered publication is therefore historical drift,
not a claim this PR proves or a reason to revert the subsequent contract.

## Exact-revision deliberate break and restoration

A disposable `git clone --shared --no-checkout` of this repository was checked out
at `14cb40ab513c20be998e087d0bcbf0a4d1285f77` under `/private/tmp`. Only in that checkout,
replaced the single line `document["capacity"] = _capacity_block(capacity_snapshot)`
with a comment. The `capacity_schema` emission and loader were left intact.
The working branch's production source was never mutated.

Command (in that checkout):

```sh
/opt/anaconda3/bin/python3 -m pytest tests/test_route_weights_export.py::test_export_includes_capacity_block -q
```

Exit 1, complete captured output:

```text
F                                                                        [100%]
=================================== FAILURES ===================================
_____________________ test_export_includes_capacity_block ______________________

tmp_path = PosixPath('/Users/teacher/.codex/orchestrator/agent-runtime/codex/tmp/pytest-of-teacher/pytest-241/test_export_includes_capacity_0')

    def test_export_includes_capacity_block(tmp_path: Path) -> None:
        database = tmp_path / "weights.db"
        export._fixture_db(database)
        snapshot = _capacity_fixture(codex_state=capacity.SHED, generated_at=42)
        document = export.build_document(database, now=100, capacity_snapshot=snapshot)
>       assert "capacity" in document
E       AssertionError: assert 'capacity' in {'schema': 'orchestrator.route-weights/v1', 'generated_at': '1970-01-01T00:01:40Z', 'source_version': 2, 'min_observations': 20, ...}

tests/test_route_weights_export.py:426: AssertionError
----------------------------- Captured stderr call -----------------------------
db=/Users/teacher/.codex/orchestrator/agent-runtime/codex/tmp/pytest-of-teacher/pytest-241/test_export_includes_capacity_0/weights.db source_version=2
=========================== short test summary info ============================
FAILED tests/test_route_weights_export.py::test_export_includes_capacity_block
1 failed in 0.62s
```

Restored the original emitter bytes in `finally`; before/after SHA-256:
`19dce5bd04684279a10314686d89ce9ceb3ce4219aaed0d866210ef88d932056`. The restored checkout's
`git diff --exit-code` is clean. Same command, exit 0:

```text
.                                                                        [100%]
1 passed in 0.26s
```

Full focused command, exit 0:

```sh
/opt/anaconda3/bin/python3 -m pytest tests/test_route_weights_export.py -q
```

```text
........................................................................ [ 94%]
....                                                                     [100%]
76 passed in 27.36s
```

## Independent historical publication inspection

Read the immutable artifact directly, rather than relying on #657's body or
publishing a new artifact:

```sh
gh api repos/stranske/Orchestrator/pulls/657 --jq '{merged_at,merge_commit_sha,head:.head.sha}'
git show -s --format=fuller 44c9f02173adee234bd7470be589eabc5fb2632b
git show 44c9f02173adee234bd7470be589eabc5fb2632b:config/route-weights.json
git show 07c975959abdf47dfe254fea8e512bb44cae9aea:src/capacity.py
git log origin/main --first-parent -1 --until=2026-10-17T03:53:46Z --format='%H %cI %s'
git rev-parse 44c9f02173adee234bd7470be589eabc5fb2632b:config/route-weights.json
git branch -r --contains 44c9f02173adee234bd7470be589eabc5fb2632b
gh api 'repos/stranske/Orchestrator/actions/runs?head_sha=44c9f02173adee234bd7470be589eabc5fb2632b&per_page=100'
```

Observed output and identifiers:

- #657 head: `647960222cea0b0000edf204b71794cc73d1cb69`.
- #657 merge: `07c975959abdf47dfe254fea8e512bb44cae9aea`, `2026-10-17T03:53:04Z`.
- This is also the last first-parent main commit at publication time.
- Deadline: `2026-10-18T03:53:04Z`.
- Published commit: `44c9f02173adee234bd7470be589eabc5fb2632b`;
  author and committer timestamp `2026-10-17T03:53:46Z` (42 seconds after merge).
- Artifact `generated_at`: `2026-10-17T03:53:44Z`.
- Blob: `e80276141a416945111474f68d40a42233ffba97`; artifact SHA-256:
  `f9d4b75f06519aa696a4fc4848a99a4ccb60779d6d352253c70dcd3e867ee95b`.
- `git branch -r --contains` includes `origin/exports/route-weights`.
- GitHub's server-created [workflow run 39000540987](https://github.com/stranske/Orchestrator/actions/runs/39000540987)
  records `event=pull_request`, `head_branch=exports/route-weights`,
  `head_sha=44c9f02173adee234bd7470be589eabc5fb2632b`, and
  `created_at=2026-10-17T03:54:05Z`.
  This is independent evidence that GitHub had the published head within 61
  seconds of merge; commit timestamps alone would not prove push time.
- The contemporaneous [publication comment](https://github.com/stranske/Orchestrator/pull/657#issuecomment-6229312180)
  was created and last updated `2026-10-17T03:54:47Z`. It corroborates publication,
  but is not the basis for verifying the artifact contents.

Compared the top-level `AGENTS` dictionary keys and each literal `window` in
`src/capacity.py` at the merge revision (AST inspection, without running historical
code) with the historical artifact:

```text
configured: ['aider', 'claude', 'codex', 'cursor', 'gemini', 'muse', 'vibe']
published:  ['aider', 'claude', 'codex', 'cursor', 'gemini', 'muse', 'vibe']
missing: []
extra: []
windows_match: True
schema: orchestrator.route-weights/v1
capacity_schema: orchestrator.route-weights-capacity/v1
```

Historical capacity block, read from that exact commit:

```json
{
  "aider": {
    "observed_at": 1792209226,
    "state": "ok",
    "window": "daily"
  },
  "claude": {
    "observed_at": 1792209226,
    "state": "ok",
    "window": "5h+weekly"
  },
  "codex": {
    "observed_at": 1792209226,
    "state": "ok",
    "window": "5h"
  },
  "cursor": {
    "observed_at": 1792209226,
    "state": "ok",
    "window": "monthly"
  },
  "gemini": {
    "observed_at": 1792209226,
    "state": "ok",
    "window": "5h+weekly"
  },
  "muse": {
    "observed_at": 1792209226,
    "state": "shed",
    "window": "5h+weekly"
  },
  "vibe": {
    "observed_at": 1792209226,
    "state": "ok",
    "window": "subscription"
  }
}
```

Conclusion: the historical **capacity-block publication deadline was met**;
no missed deadline was found. This says neither that the local snapshot's states
can be reconstructed independently today nor that the original command ran from
the owner's mirror. The artifact has `source_version: 0`, empty rankings and
`evidence_ok: false`; it is not proof of healthy learned-weight publication.
The later empty-evidence guard is outside this repair. Weekly downstream
`acted_on > 0` is also outside this bounded task and is not claimed.

## CI evidence boundary

The final PR body retains the final branch head, exact-head focused rerun, CI run
and successful verification job links. `.github/workflows/ci.yml` runs the selected
CI Python on `src/verify.py --pytest-shard ...`, then
`src/verify.py --aggregate shard-evidence --coverage --floor-may-lag` on PRs,
and verifies the flat mirror separately. Success must belong to this PR's exact
head/explicit merge-test revision, not to #657 or an earlier main commit.
No old `hollow_break` classification substitutes for the runnable commands above.
