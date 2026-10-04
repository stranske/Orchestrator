# `orch-sync-mirror.sh` must be patched for the `src/` layout — BEFORE the next sync

**This is the one external dependency of the `src/` move, and it is the owner's file.**
`~/.codex/bin/orch-sync-mirror.sh` lives outside the repository, so this change cannot land it. It
must be applied by hand, and the deliberate manual mirror sync (`CLAUDE.md` §1: *"the only circuit
breaker between an agent's change and the dispatcher"*) is the natural gate for doing so.

## What breaks without it

Line 25 copies the modules **flat**:

```bash
cp "$SRC"/*.py "$SRC"/orchestrate.sh "$MIRROR"/
```

After the move there are no `.py` files at `$SRC` — they are all in `$SRC/src/`. The glob matches
nothing, `cp` fails, and launchd's hourly `orchestrate.sh --active` runs against a mirror with no
modules. The script's closing line would report `synced 0 .py`, so the failure is visible rather
than silent — but only to someone reading the output.

## The patch

Replace line 24–25:

```bash
find "$MIRROR" -maxdepth 1 \( -name '*.py' -o -name '*.sh' \) -delete 2>/dev/null || true
cp "$SRC"/*.py "$SRC"/orchestrate.sh "$MIRROR"/
```

with:

```bash
# Modules moved to src/ (2026-08-23). The mirror stays FLAT on purpose: everything that resolves
# paths here — paths.py in Python, $ORCH in orchestrate.sh — detects the layout rather than
# assuming it, so a flat mirror and a src/ checkout are both correct. Keeping the mirror flat also
# means the delete-then-copy below needs no new directory handling.
find "$MIRROR" -maxdepth 1 \( -name '*.py' -o -name '*.sh' \) -delete 2>/dev/null || true
MODSRC="$SRC/src"
[[ -d "$MODSRC" ]] || MODSRC="$SRC"          # tolerate a pre-move checkout
cp "$MODSRC"/*.py "$SRC"/orchestrate.sh "$MIRROR"/
```

and update the closing summary line to count from `$MODSRC` if you want the number to stay
meaningful.

## Why the mirror stays flat rather than gaining a `src/`

Both shapes work, because every path resolver detects the layout instead of assuming it:

| resolver | rule |
|---|---|
| `src/paths.py` | module dir named `src` ⇒ checkout is its parent; else the two coincide |
| `orchestrate.sh` | `ORCH="$ORCH_REPO/src"`, falling back to `$ORCH_REPO` when that directory is absent |

A flat mirror therefore needs no further change, and it keeps the existing delete-then-copy
one-liner. That symmetry is deliberate: the alternative — hardcoding `parent.parent` somewhere —
is the failure `capability_activation_audit._fleet_roots` already documents, where byte-identical
code scored 37 of 37 in the canonical tree and 36 of 37 in the mirror.

## Files the sync already copies by root path, and which are unaffected

`orchestrate.sh`, `.verify-floor.json`, `CLAUDE.md`, `IMPROVEMENT_BACKLOG.md`,
`experiments/*.json`, `data/feedback-snapshot.json`, and the Workflows registry all stay at the
checkout root, so their lines need no edit.

**One line does need attention:** the sync copies `.coveragerc`, which this change **deleted** —
the coverage settings moved into `pyproject.toml` because CI passes `--cov-config=pyproject.toml`
whenever that file exists, which would have made a surviving `.coveragerc` invisible. Change that
copy to `pyproject.toml`, or `test_verify_coverage_mode.py` will skip in the mirror for a missing
prerequisite rather than assert.

## How to confirm it worked

```bash
bash ~/.codex/bin/orch-sync-mirror.sh && ls ~/.codex/orchestrator-mirror/*.py | wc -l
```

Expect ~99, and then `cd ~/.codex/orchestrator-mirror && python3 verify.py` should be green — note
that in the FLAT mirror the command keeps its old form, with no `src/` prefix.

**What "green in the mirror" means, and why it was impossible until 2026-08-29.** The mirror is a
flat copy, so it is not a git repository and has no `.github/`; 31 tests skip there for exactly
those two named reasons, and the run reports `31/31 max [mirror_skipped_max]`. Those skips are
agreed, not tolerated — `.verify-floor.json` now carries a `mirror_skipped_max` alongside
`skipped_max`, because the latter was measured on a bare GitHub runner (a different deprivation:
missing CLIs and ledger rows, but a real checkout) and one number cannot bound both populations.
A mirror run was therefore RED on every input, correct trees included, which made the instrument
this doc points you at worthless. The shape is detected by `env_prereq.exec_mirror_shape()`, never
from `$CI`, and the summary's first line says which tree it decided it was in. If you ever teach
the sync to copy `.github/`, 12 of those skips become real checks and `mirror_skipped_max` must
come down to 19 in the same change. (Done on 2026-10-02, and it came down to 21: see the last
section, which also explains why the mirror's two marks had to change with it.)

## The cadence's contracts (2026-09-04)

`orch-sync-mirror.sh` copied `tests/*.py` only, so the flat mirror had no `tests/rail_exercises` and
the weekly rail-exercise cadence (PR #207) ran against an ABSENT tree. The runner now says so —
`report()` carries `"tree": "absent: <path>"` and the totals line and the periodic report print it,
so the zero is named rather than silent — and the sync script gained, after its `tests/*.py` copy:

```bash
if [[ -d "$SRC/tests/rail_exercises" ]]; then
  cp -R "$SRC/tests/rail_exercises" "$MIRROR/tests/"
fi
```

Confirm with `find ~/.codex/orchestrator-mirror/tests/rail_exercises -name contract.json | wc -l`
(49 at the time of writing) and a mirror run's totals line reading `tree=present`.

## The cadence's first run from the mirror, and the sync that never finished (2026-09-14)

Two independent defects, found the same night, both in the seam this document is about.

**The sync hung inside `rsync`.** Dropbox keeps most of `tests/rail_exercises` as online-only
placeholders (341 of 962 files on 2026-09-13; `ls -lO` prints `dataless`), and any content read of
one blocks until Dropbox fetches it. Two syncs sat in the rsync of that subtree for hours, and
`cp -R` reproduces the hang. The script runs under `set -e`, so nothing after that line ran: no
`chmod +x` (the copy had just replaced `orchestrate.sh` with a 100644 file, and three ticks died
with `Permission denied`), no `.verify-floor.json`, no `pyproject.toml`. The fix ships that
subtree from git, whose objects are always local because `.git` is dropbox-ignored:

```bash
if git -C "$SRC" rev-parse --verify -q HEAD:tests/rail_exercises >/dev/null 2>&1; then
  rm -rf "$MIRROR/tests/rail_exercises"
  git -C "$SRC" archive --format=tar HEAD tests/rail_exercises | tar -x -C "$MIRROR/tests" --strip-components=1
  echo "tests/rail_exercises shipped from git HEAD $(git -C "$SRC" rev-parse --short HEAD) — uncommitted fixture edits are NOT shipped"
elif [[ -d "$SRC/tests/rail_exercises" ]]; then
  rsync -a --delete --exclude '*conflicted copy*' "$SRC/tests/rail_exercises/" "$MIRROR/tests/rail_exercises/"
fi
```

> **Superseded 2026-10-01:** this bare `| tar -x` can abort the whole sync under load. The script
> now drains the stream — see the last section before copying it.

It finished in 0.4 s against the same checkout. The one semantic change is printed every run:
uncommitted fixture edits are not shipped. `orchestrate.sh` is also 100755 in git now, and
`test_orchestrate_sh_is_executable` fails any tree where the bit is missing, so the copy step can
no longer produce a tick that cannot start.

**The cadence resolved the tree one level above the mirror.** `rail_exercise.py` derived its root
as `Path(__file__).resolve().parents[1]` — right under `src/`, wrong on the flat mirror, where it
named `~/.codex/tests/rail_exercises`, reported it absent, exited 0, and `orchestrate.sh` stamped
six days of success on a zero. It now takes the root from `paths`, builds a per-process view with a
`src/` link when the tree is flat (33 of 49 committed contracts say `src/` somewhere), and exits
non-zero on zero contracts, so the tick records a FAILURE and retries in six hours instead of
stamping. After the next sync, clear the stamp so the cadence runs again before its week is up:

```bash
rm -f ~/.codex/orchestrator/.last-rail-exercise
```

## `scripts/` ships whole, and the unsyncable fixture names are gone (2026-09-14, later that night)

Two of the 49 committed contracts (`docs-drift-fix-agent/arm-a` and `arm-b`) run
`scripts/docs_drift_fix_agent.py` from the tree they execute in. The sync copied only
`scripts/check_checks_reported.py`, so both failed on the mirror for that absence alone — the only
two failures left once the root fix above landed. The script now ships the whole `scripts/` tree
from `git archive HEAD` (the placeholder reasoning above applies), keeping the repository-relative
path and the `+x` on `check_checks_reported.py`, which the lanes execute directly:

```bash
if git -C "$SRC" rev-parse --verify -q HEAD:scripts >/dev/null 2>&1; then
  mkdir -p "$MIRROR/scripts"
  find "$MIRROR/scripts" -mindepth 1 -delete 2>/dev/null || true
  git -C "$SRC" archive --format=tar HEAD scripts | tar -x -C "$MIRROR"
  chmod +x "$MIRROR/scripts/check_checks_reported.py"
fi
```

> **Superseded 2026-10-01:** same race as the `tests/rail_exercises` pipeline — see the last
> section before copying it.

Witnessed in a scratch flat mirror: both contracts pass with correct break demos.
`env_prereq.repo_files_absent` detects the FILE, so the tests that assert against `scripts/` now
run on the mirror instead of skipping; the mirror's skip count goes down, never up.

Separately, seven contracts had carried a duplicate fixture directory named
`capability-activation-audit ` — trailing space — since #207. Dropbox cannot hold that name: the
owner's checkout collapsed the two into "conflicted copy" files and then deleted the live `run.py`
beside each one, so a working-tree sync would have shipped the damage (the git-archive sync above
did not). The 14 paths are removed, and `rail_exercise.unsyncable_paths()` refuses a committed tree
whose path components carry leading or trailing whitespace, a trailing dot, or one of `<>:"|?*\` —
enforced by the module selftest, which CI runs; the selftest also plants a `trailing ` directory in
its tripwire tree and asserts the guard names it.

## Both `git archive | tar -x` pipelines could abort the sync under load (2026-10-01)

macOS `/usr/bin/tar` (bsdtar 3.5.3) stops reading at the end-of-archive marker, but `git archive`
writes the tar record padding after it (5,120 bytes for `tests/rail_exercises` at `db0a9be`; the
amount varies with the tree). When tar exits before git's final `write()` has landed, git dies of
SIGPIPE, `pipefail` makes the pipeline 141, and `set -e` ends the script **without printing
anything**. By then the modules, the tests and `tests/rail_exercises` are already replaced, while
`scripts/`, the data snapshots, `.verify-floor.json`, `pyproject.toml`, `CLAUDE.md`,
`IMPROVEMENT_BACKLOG.md` and the registry are not — so the half-synced mirror surfaces later, as
layout failures in a mirror `verify.py` (a scratch copy collected `901 tests collected, 1 error`: a
`FileNotFoundError` for `scripts/check_checks_reported.py`, and no floor file to compare against).

It is a scheduler race, so it hides: 12 of 12 runs aborted at load average ~100–128 on 2026-09-30,
while the unpatched pipeline passed every run at load ~26–62 on 2026-10-01. A test shim that holds the padding back for one
second makes it deterministic: the previous script exits 141 with no output, the patched one exits 0
and its mirror collects exactly the floor. Each reader now drains the rest of the stream, which
keeps the pipe open until git exits:

```bash
git -C "$SRC" archive --format=tar HEAD tests/rail_exercises | { tar -x -C "$MIRROR/tests" --strip-components=1 && cat >/dev/null; }
git -C "$SRC" archive --format=tar HEAD scripts | { tar -x -C "$MIRROR" && cat >/dev/null; }
```

`&&`, not `;`: with `;` a failed extraction is masked by `cat`'s exit 0. Reverting either drain on
its own brings the 141 back at that line. The script also gained an `ERR` trap, so an abort now names
its line and says the mirror is half-synced instead of ending silently. The previous script is kept
beside it as `orch-sync-mirror.sh.bak-2026-10-01`.

> **Superseded 2026-10-02:** the `tests/rail_exercises` pipeline is gone. All of `tests/` now ships
> in one archive that keeps this drain; see the last section.

## Nothing under `tests/` travels but the modules and `rail_exercises/` (2026-10-02)

> **Superseded later on 2026-10-02:** the sync now ships the whole `tests/` tree, so the contract
> at the end of this section no longer applies. The fixture stays inlined; see the last section.

Under `tests/`, the sync ships exactly two things: the top-level `tests/*.py`, copied from the
working tree, and `tests/rail_exercises/`, from `git archive HEAD`. PR #349 added a third kind of
file, `tests/fixtures/ux_review_adversarial_2026_09_22.txt`, which
`tests/test_ux_review_adversary.py` read through `Path(__file__).parent / "fixtures"`. The sync
never carried it, so a mirror `verify.py` of main `db0a9be` was red on that one test
(`FileNotFoundError`: 866 passed, 1 failed, 40/40 mirror skips) while it passed in every checkout
and in CI, neither of which runs in this shape.

**The fix is in the repository, and this script needs no patch.** The capture is now a string
constant in the test module, byte-for-byte, with the git blob id of the file #349 committed pinned
beside it, so an edit to the capture fails the test instead of quietly changing what it parses. The
fixture and its directory are deleted. It was the only tracked file under `tests/` that is neither a
top-level `.py` nor inside `tests/rail_exercises/`, and every other test that resolves a path from
its own `__file__` reads something the sync ships: the test modules themselves,
`tests/rail_exercises/`, `scripts/` or `orchestrate.sh`. A `git archive` block for `tests/fixtures/`
would now guard a directory that does not exist, and could not be witnessed doing anything.

**The contract for the next test.** A file a test reads from beside itself must be one the sync
ships, or live in the test module. If test data ever outgrows a string constant, ship its directory
the way `tests/rail_exercises/` ships — a `git archive HEAD <dir>` block with the
`&& cat >/dev/null` drain from the section above — in the same change that adds the data, and
witness it in a scratch mirror built by this script. `ORCH_MIRROR` alone does not isolate that
witness: the script also copies the Workflows registry to `$HOME/.codex/orchestrator/`, the live
runtime directory, so point `HOME` at a scratch directory too (with `.codex/orchestrator/` created
inside it) and keep `gh` authenticated with `GH_CONFIG_DIR`, which must come first so it expands
against the real home:

```bash
GH_CONFIG_DIR="$HOME/.config/gh" HOME=<scratch-home> ORCH_MIRROR=<scratch-mirror> ~/.codex/bin/orch-sync-mirror.sh <checkout>
```

Then `cd <scratch-mirror> && python3 verify.py`, whose summary must open with
`tree: EXEC MIRROR — mirror_* ceilings apply`. Witnessed that way for this change:
`866 passed, 1 failed, 40/40` on unmodified main (the red, reproduced) and
`867 passed, 0 failed, 40/40` with the capture inlined, 97 of 97 selftests and five of five gates in
both, with the live mirror and the live registry untouched by either.

## The whole tracked `tests/` tree now travels (2026-10-02)

The current machine-local `~/.codex/bin/orch-sync-mirror.sh` replaces the top-level `tests/*.py`
copy and the special `tests/rail_exercises/` archive with one archive of the complete tracked test
tree:

```bash
git -C "$SRC" archive --format=tar HEAD tests | { tar -x -C "$MIRROR" && cat >/dev/null; }
```

The `cat` drain is retained so macOS `tar` cannot close the pipe before `git archive` writes its
padding. Extraction and draining must be joined with `&&`, never with `;` or a bare newline between
the commands, because either of those lets `cat`'s exit 0 hide a failed extraction (the 2026-10-01
section above). Wrapping the line immediately after `&&` preserves the same short-circuit behavior.
Uncommitted test edits still do not travel: the archive is built from `HEAD`, not from the working
tree.

The repository guard in `tests/conftest.py` enforces the other half of that contract. It compares
the tracked files under `tests/` with the files in the actual `git archive HEAD tests` result, then
fails any test that reads an untracked, ignored, or `export-ignore`d input. Comparing with the
archive itself also covers an `export-ignore` rule placed on an ancestor directory. A fixture staged
for the next commit is absent from `HEAD`'s archive but normally ships with that commit. The guard
therefore checks staged additions against Git's cached `export-ignore` attributes: an ordinary
staged fixture is allowed, while one already covered by an export-ignore rule remains watched.
That includes a rule on a directory above the file. Git applies such a rule to the directory,
not to each file inside it, so the guard asks about every parent directory with its trailing
slash, the way `git archive` asks; the file's own path reports nothing (2026-10-04, six rule
styles measured against `git archive`).

Witness the installed script without touching either live runtime by isolating both `HOME` and the
mirror while preserving GitHub authentication:

```bash
GH_CONFIG_DIR="$HOME/.config/gh" \
HOME=<scratch-home> \
ORCH_MIRROR=<scratch-mirror> \
~/.codex/bin/orch-sync-mirror.sh <checkout>
cd <scratch-mirror> && python3 verify.py
```

The witness is complete only when the sync exits zero, identifies the exec-mirror tree, collects
exactly the recorded floor, stays within the mirror skip ceiling, and passes every selftest and
capability gate. The earlier fixture-inline and two-pipeline sections remain incident history;
this section is the current procedure their supersession notes reference.

Witnessed that way on 2026-10-02. Each row is a fresh scratch mirror built by the script named, with
`HOME` and `ORCH_MIRROR` both scratch; the live mirror and the live registry moved only with the
owner's own syncs:

| source | sync script | scratch-mirror `verify.py` |
|---|---|---|
| `db0a9be` (the fixture still a file) | previous (`orch-sync-mirror.sh.bak-2026-10-02`) | RED: 866 passed, 1 failed (`FileNotFoundError`), 40/40, the shape observed live |
| `db0a9be` | current | 867 passed, 0 failed, 40/40, 907 of floor 907, 97/97 selftests, 5/5 gates |
| `db5a65f` (#371 merged) | current | 970 passed, 0 failed, 40/40, 1010 of floor 1010, 98/98 selftests, 5/5 gates |

Two deliberate breaks, made on a byte-identical copy of the installed script and reverted to
`cmp`-identical bytes:

* **Leaving one unnamed directory out of the archive** (`':(exclude)tests/fixtures'`, sourced from
  `db0a9be`) brought the `FileNotFoundError` back: 1 failed, 5 passed. After the revert, 6 passed.
* **Dropping the drain from the `tests/` line alone** made the sync abort at that line with rc 141
  and the half-synced trap message, under a test shim that holds back `git archive`'s 512 bytes of
  record padding for one second. After the revert it exited 0 under the same shim.

The guard was also run against the incident's own file. A repository rebuilt from
`git archive db0a9be` with the fixture never added failed exactly
`test_2026_09_22_adversary_fixture_survives_aggregation` in the checkout ("not tracked by git:
`git add` it"). With the fixture committed, the guard was silent and 6 passed.

## The wrapper takes its verdict BEFORE the live copy (2026-10-02)

This patch is for `~/.codex/bin/orch-mirror-sync.sh`, the guarded wrapper you run to sync, not for
the copy script above. The wrapper used to copy into the live mirror and THEN run `verify.py` there.
That ordering has two defects:

- **The verdict arrived after launchd could already run the code.** A red told you about a tree
  that was live from the moment the copy finished.
- **A run in the live mirror can read a torn tree.** The pre-copy verdict avoids taking the verdict
  on that moving tree; it does not yet make the legacy in-place publication atomic for other live
  readers. That separate generation-switch design must also preserve mirror-local runtime output.

`verify.py` checks the WHOLE tree it runs in, never one session's work, so on any one tree only the
latest run counts. CI already runs the same suite on every PR head and on main. The one run a sync
needs is the one CI cannot do, on this machine (live ledger, installed CLIs, the flat mirror shape),
on the exact tree about to go live.

`scripts/verify_before_sync.sh SRC` takes that run:

1. It builds a throwaway mirror from `SRC` with the real copy script, isolated the way the witness
   procedure above prescribes: scratch `HOME` and `ORCH_MIRROR`, with `GH_CONFIG_DIR` keeping gh
   authenticated.
2. It runs `verify.py` there on a scratch COPY of the live state: the ledger, the stamps and
   the small directories, plus the Brain through SQLite's backup API. `verify.py`'s
   `ledger validate` gate is a writing load (`capabilities.load(create=True)`). Pointed at the
   live ledger before the copy, it would write the declarations of a tree that is not deployed,
   even when the verdict then stops the copy. `HOME` stays real, so the installed CLIs and
   skills are seen. A directory over `VERIFY_BEFORE_SYNC_MAX_DIR_MB` (default 50) is skipped and
   named: `agent-runtime`, `frontend-verify`, `repos`, `reviews` and a few smaller ones today.
3. It hashes the deployment-owned bytes before and after `verify.py`, and also re-checks that `SRC`
   did not change. A verifier write, checkout switch, or concurrent source edit therefore voids the
   verdict instead of silently changing what the wrapper can deploy.
4. With `--snapshot-out DIR --digest-out FILE`, it publishes the verified scratch mirror and the
   verifier's final payload digest only after every gate is green. The wrapper requires that receipt
   and passes it to `scripts/install_verified_snapshot.py`; the installer rejects any retained
   snapshot change before mutating the live mirror. It never re-reads mutable `SRC` or fetches the
   registry a second time.

The installer first builds a private deployment payload and checks its complete digest against
the receipt, including permissions and symlink targets. It validates the copied docs manifest and
registry JSON before removing any live deployment files. Installation and the separate registry
update read that private payload, so the retained snapshot is no longer needed once preparation
passes. Symlinks must be relative and stay within the payload. Preparation failures leave the live
mirror untouched; the subsequent copy still runs in place, so reader consistency and recovery
remain pending under #389.

### Deployment and runtime ownership

`scripts/install_verified_snapshot.py` defines the current ownership boundary. It does not
enumerate all mirror files as deployment input or copy runtime output into its private payload:

| Paths | Owner and publication behavior |
|---|---|
| Root `*.py`, `*.sh`; `tests/`, `scripts/`, `.github/` | Deployment-owned. Old entries are removed, including files no longer shipped. Do not store runtime output in these trees. |
| `docs/` | Merged directory. Only the file or symlink paths listed in `.docs-shipped.txt` are deployment-owned. Parent directories do not grant ownership of their children. Old manifest leaves are removed; new leaves come from the retained snapshot. Unlisted reports remain runtime-owned, including `docs/reports/issue_completion_*`. |
| `.docs-shipped.txt`, `.gitignore`, `.verify-floor.json`, `.coveragerc`, `pyproject.toml`, `ruff.toml`, `CLAUDE.md`, `IMPROVEMENT_BACKLOG.md`, `repo_review_registry.json` | Named deployment files, removed if absent from the new snapshot. |
| `experiments/hypotheses.json`, `experiments/features.json`, `experiments/repo_knowledge.json`, `data/feedback-snapshot.json`, `config/coverage-baseline.json` | Named deployment files only. Other children and their parent directories remain runtime-owned. |
| `experiments/.last-ship-gate`, unlisted docs/reports, other paths outside deployment ownership | Runtime-owned. Publication leaves these in place, preserving open file handles, concurrent updates, and newly created reports. |
| `~/.codex/orchestrator/repo_review_registry.json` | Separate runtime-registry copy. Validated deployment bytes supply its update after generation publication; there is no atomic transaction across these locations. The file is synced before replacement and its parent directory afterward; failures propagate and a fresh install retries the update. |

Before touching live deployment entries, the installer rejects a file-ownership path that now
contains a real directory, or a merged parent that is a symlink or a non-directory. This prevents
an obsolete shipped-doc entry from recursively deleting runtime children and prevents writes
through parent symlinks into runtime storage. Leaf removal uses `unlink`, so a directory created
after this check also cannot be removed recursively. A conflicting layout must be resolved before
retry; it is not permission to delete runtime data. A leaf symlink itself may be removed without
deleting its target.

The separate runtime-registry destination must be outside both the retained snapshot and the
mirror. The installer rejects overlapping destinations before removing any live entries,
including destinations whose parent symlink points into either tree. This prevents a registry
update from overwriting verified deployment bytes after the digest check or consuming mirror-local
runtime reports. The update uses the resolved parent path, so retargeting the original parent
alias during preparation cannot redirect it into the mirror. A registry leaf symlink outside those
trees is replaced without following its target.

`tests/test_install_verified_snapshot.py` synchronizes a writer with the live copy, including
handles opened before publication and a report created during it. It checks that the writes
survive, obsolete deployment docs disappear, and the installed deployment digest still matches
the receipt. The current publisher prepares and validates a private generation, then exchanges it with
the live directory under the mirror-specific publication lock. Reader exclusion, runtime backing
references, and interruption/retry witnesses are described in the PR #390 sections below. The
installed incumbent copier remains unsafe until its entry guard is installed after merge and pull.

The verifier's exit codes are 0 VERIFIED, 1 NOT VERIFIED, 2 nothing verified, and 3 VOID (`SRC` moved). It
never writes the live mirror, the live registry copy, the live ledger or the live Brain.
`tests/test_verify_before_sync.py` covers every exit path, the isolation of the copy, and the
state copy, with stand-ins for the copy script and `verify.py`. The `verify.py` stand-in writes
to the ledger and Brain it is handed, as the real gate does, and the live originals are checked
byte- and row-identical afterwards.

**The patch.** In `~/.codex/bin/orch-mirror-sync.sh`, replace everything from the line
`echo "== syncing"` (and the bare `echo` before it) to the end of the file with:

```bash
# THE VERDICT COMES BEFORE THE COPY (2026-10-02). verify.py checks the whole tree it runs in, so the
# one machine-local run a sync needs is on the exact tree about to go live: taken in a scratch mirror
# built by the same copy script. The green path installs that exact verified snapshot, never mutable
# SRC. A red or missing pre-verifier stops before deployment; --no-verify remains the explicit copy.
print_unverified_override() {
  printf '  to copy anyway, unverified:' >&2
  printf ' %q' "$0" --no-verify >&2
  [[ "$ALLOW_UNMERGED" == "1" ]] && printf ' %q' --allow-unmerged >&2
  printf ' %q\n' "$SRC" >&2
}

PRE="$SRC/scripts/verify_before_sync.sh"
if [[ "$RUN_VERIFY" == "0" ]]; then
  echo
  echo "== syncing without a verdict (--no-verify)"
  PUBLISHER="$SRC/scripts/publish_unverified_snapshot.sh"
  if [[ ! -f "$PUBLISHER" ]]; then
    echo "NOT SYNCED: no guarded unverified publisher in $SRC." >&2
    exit 3
  fi
  bash "$PUBLISHER" "$SRC" "$MIRROR"
  echo
  echo "== skipped the mirror verify (--no-verify). The copy is NOT a verdict."
  exit 0
fi

if [[ ! -f "$PRE" ]]; then
  echo "NOT SYNCED: $SRC has no scripts/verify_before_sync.sh; no pre-copy verdict exists." >&2
  print_unverified_override
  exit 3
fi

STAGE_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/orch-verified-sync.XXXXXX")"
trap 'rm -rf "$STAGE_ROOT"' EXIT
SNAPSHOT="$STAGE_ROOT/mirror"
DIGEST_RECEIPT="$STAGE_ROOT/verified-payload.sha256"
echo
echo "== verdict BEFORE the live copy, from a scratch mirror of $SRC"
if ! bash "$PRE" --snapshot-out "$SNAPSHOT" --digest-out "$DIGEST_RECEIPT" "$SRC"; then
  echo >&2
  echo "NOT SYNCED: the live mirror still runs the code it ran before this command." >&2
  print_unverified_override
  exit 3
fi
EXPECTED_DIGEST="$(cat "$DIGEST_RECEIPT" 2>/dev/null || true)"
if [[ ! "$EXPECTED_DIGEST" =~ ^[0-9a-f]{64}$ ]] ||
   [[ "$(wc -l < "$DIGEST_RECEIPT" 2>/dev/null | tr -d ' ')" != "1" ]]; then
  echo "NOT SYNCED: the verified snapshot has no valid one-line digest receipt." >&2
  print_unverified_override
  exit 3
fi
if [[ -n "${ORCH_VERIFIED_RECEIPT_OUT:-}" ]]; then
  if ! (set -o noclobber; cat "$DIGEST_RECEIPT" > "$ORCH_VERIFIED_RECEIPT_OUT"); then
    echo "NOT SYNCED: cannot retain verifier receipt; live mirror untouched." >&2
    exit 3
  fi
fi
INSTALLER="$SNAPSHOT/scripts/install_verified_snapshot.py"
if [[ ! -f "$INSTALLER" ]]; then
  echo "NOT SYNCED: the verified snapshot has no installer." >&2
  print_unverified_override
  exit 3
fi

echo
echo "== installing the verified snapshot (SRC is not read again)"
if python3 -I "$INSTALLER" "$SNAPSHOT" "$MIRROR" \
  --expected-digest "$EXPECTED_DIGEST" \
  --runtime-registry "$HOME/.codex/orchestrator/repo_review_registry.json"; then
  :
else
  install_rc=$?
  printf 'INSTALL FAILED (exit %s): publication or registry update failed; retain the complete visible generation and retry.\n' \
    "$install_rc" >&2
  printf '  to retry verified installation from a fresh snapshot:' >&2
  printf ' %q' "$0" >&2
  [[ "$ALLOW_UNMERGED" == "1" ]] && printf ' %q' --allow-unmerged >&2
  printf ' %q\n' "$SRC" >&2
  print_unverified_override
  exit "$install_rc"
fi
echo
echo "== the exact deployment snapshot above received the verdict; no second run"
```

Also change the `--no-verify` help line to say it is the way to copy after a red:
`#   --no-verify       copy only, no verdict (also how to copy anyway after a red)`.

**What changes for you.**

| Verdict | What the wrapper does |
|---|---|
| Green | It installs the retained deployment snapshot that received the verdict, and does not read `SRC`, refetch the registry, or run `verify.py` a second time. The copy now lands 10–25 minutes after you start the command, not immediately. |
| Red | It does not copy. The live mirror keeps running the code it already ran, and the last line printed is the one command that copies anyway. A false red (a ledger row registered by a sibling session's unmerged branch, say) therefore costs one command, never a blocked sync. |
| VOID | It does not copy. Re-run once the clone is settled. |
| Missing pre-verifier | It fails closed before copying and prints the quoted `--no-verify` override. There is no copy-first fallback. |
| Installer failure | It preserves the installer's status and prints both retry paths. The complete old or new generation remains visible; the separate registry may still need retry. Remaining reader/runtime acceptance stays tracked by #389. |

**How to confirm it.** Run the script on its own first. It writes nothing live:

```bash
bash ~/.codex/orchestrator-src/scripts/verify_before_sync.sh ~/.codex/orchestrator-src
```

It must end with `== verify-before-sync: VERIFIED for …`, and the `verify.py` summary above that line
must open with `tree: EXEC MIRROR — mirror_* ceilings apply`. Then confirm that the live mirror's
`orchestrate.sh` mtime and `~/.codex/orchestrator/repo_review_registry.json` did not move.

**Latched-gate answers for the stop-on-red.**

1. *What clears it?* A green verdict on the next run, or the printed `--no-verify` command. The
   override is always available and needs nothing the stop forbids.
2. *Can that run while the gate is closed?* Yes. Neither the fix nor the override depends on the
   wrapper having copied.
3. *Measuring window = draining window?* Yes. The helper hashes the deployment-owned scratch bytes
   before and after the run, publishes them only on green, and writes the final digest through a
   separate receipt. The installer compares the retained snapshot with that receipt before touching
   the live mirror, then compares the installed payload again. `SRC` is never read again: a source
   change during verification returns VOID; a later source change is irrelevant to installation.
4. *What does it print when drained?* `== verify-before-sync: VERIFIED for <SRC> @ <sha> …`,
   then the install, then `the exact deployment snapshot above received the verdict; no second
   run`. `test_mirror_sync_patch.py` extracts and executes the documented block with stand-ins, so
   the live wrapper contract and the documentation cannot silently diverge.

**Applied 2026-10-02, at the owner's request, and re-applied as v2 after review.**
- `~/.codex/bin/orch-mirror-sync.sh` carries the block above byte for byte, plus the `--no-verify`
  help line.
- Backups: `orch-mirror-sync.sh.bak-2026-10-02-before-presync-verify` holds the version before any
  of this; `orch-mirror-sync.sh.bak-2026-10-02-presync-v1` holds the first version, which still
  copied first and verified afterwards for a source without the script.
- Both versions were witnessed with `HOME` pointed at a scratch directory. The copy script the
  wrapper calls and its default mirror were therefore stand-ins, and nothing live was reachable.
  For v2:
  - green exits 0 after one copy and no second verify;
  - red exits 3 without copying and prints the `--no-verify` command;
  - a source without the script exits 3 without copying;
  - `--no-verify` copies once;
  - a retained deployment snapshot that changes after the verdict is rejected before installation;
  - an installer failure preserves its status and prints verified-retry and unverified-override
    recovery commands.
- The live mirror's `orchestrate.sh` and the live registry copy did not move.
- The real copy script has not yet been run through the wrapper. The confirmation step above is
  that witness.

## `.github/`, `docs/`, `.gitignore` and `ruff.toml` travel, and the mirror's marks changed (2026-10-02)

**The red.** PR #352 added `tests/test_gate_commit_status_fork_tolerance.py`, which opens
`.github/workflows/pr-00-gate.yml` with no env_prereq guard. The mirror had no `.github/`, so the
owner's sync of `36f00bc` ended `998 passed, 40 skipped, 19 errors` (every error that
`FileNotFoundError`) while CI and every checkout were green. Guarding those 19 would have taken the
mirror to 59 skips against a ceiling of 40, so the files they read travel instead, and the 19
`.github/workflows` skips become real checks with them. `mirror_skipped_max` comes down 40 → 21 in
the same change: a drain, measured where it is enforced.

**Shipping `.github/` alone would have traded that red for one only the LIVE mirror shows.**
`tests/test_ci_gate_config.py` skips unless `.github/workflows`, `docs` and `scripts` all exist. The
live mirror has a `docs/`, because something writes `docs/reports/issue_completion_*` there at run
time (untracked in this repository), so with `.github/` present those tests ran and five failed on
the absent `ruff.toml` and `docs/CI_LINT_BASELINE.md`. A fresh scratch mirror has no `docs/` at all
and stayed green. While `docs/` did not ship, a scratch mirror could stand for the live one only
with that run-time output planted in it. Shipping `docs/` closes the gap, because both trees now
carry the tracked docs, which is what lets the pre-sync scratch verdict in the section above stand
for the live mirror again. The rows below were still taken with the output planted.

**`docs/` is merged, never replaced**, for the same reason: a delete-then-extract would destroy that
run-time output. The paths the previous sync shipped are recorded in `.docs-shipped.txt` at the
mirror root, only those are removed before the new tree lands (so a doc deleted from the repository
does not linger), and each entry is re-checked against `docs/` and `..` before anything is removed.

The block, after the `scripts/` block of `~/.codex/bin/orch-sync-mirror.sh` (previous script kept as
`orch-sync-mirror.sh.bak-2026-10-02b`):

```bash
rm -rf "$MIRROR/.github"
rm -f "$MIRROR/.gitignore" "$MIRROR/ruff.toml"
if git -C "$SRC" rev-parse --verify -q HEAD:.github >/dev/null 2>&1; then
  config_paths=(.github)
  for root_file in .gitignore ruff.toml; do
    if git -C "$SRC" rev-parse --verify -q "HEAD:$root_file" >/dev/null 2>&1; then
      config_paths+=("$root_file")
    fi
  done
  git -C "$SRC" archive --format=tar HEAD "${config_paths[@]}" | { tar -x -C "$MIRROR" && cat >/dev/null; }
elif [[ -d "$SRC/.github" ]]; then
  rsync -a --delete --exclude '*conflicted copy*' "$SRC/.github/" "$MIRROR/.github/"
  # ...and the two root files, copied from the working tree
fi
if git -C "$SRC" rev-parse --verify -q HEAD:docs >/dev/null 2>&1; then
  if [[ -f "$MIRROR/.docs-shipped.txt" ]]; then
    while IFS= read -r shipped; do
      case "$shipped" in
        docs/*) [[ "$shipped" == *..* ]] || rm -f "$MIRROR/$shipped" ;;
      esac
    done < "$MIRROR/.docs-shipped.txt"
  fi
  git -C "$SRC" -c core.quotePath=false ls-tree -r --name-only HEAD docs > "$MIRROR/.docs-shipped.txt"
  git -C "$SRC" archive --format=tar HEAD docs | { tar -x -C "$MIRROR" && cat >/dev/null; }
fi
```

Both pipelines keep the `&& cat >/dev/null` drain from the 2026-10-01 section. Nothing shipped here
runs; they are inert files that tests read.

**The mirror's marks had to change with it.** `env_prereq.exec_mirror_shape()` required "no
`.github/`" AND "not a git repository", so the shipped mirror would have read as a CHECKOUT, taken
the runner's ceiling and printed `tree: checkout` — a mark that stops being true of the tree it
names mislabels it silently. The marks are now the shape this script builds on purpose (modules
FLAT at the root, by `paths.checkout_root`'s own rule) AND "not a git repository" (the absence that
still produces the mirror's only mirror-only skips). The AND keeps a git-less checkout or a
`git archive` export, both of which keep `src/`, on the base agreement.
`tests/test_exec_mirror_shape.py` asks the real function about real synthetic trees in all six
combinations of layout, `.github/` and git.

**Order of installation matters, in one direction only.** The lowered ceiling is valid only with
this script installed. A repository synced through the OLD script ships no `.github/`, skips 40
against 21 and goes red loudly — the strict direction. The patched script with an older repository
is merely mislabelled (`tree: checkout`, the runner's ceiling) until the repository change lands.

Witnessed on 2026-10-02 in scratch mirrors (`HOME` and `ORCH_MIRROR` both scratch, live mirror and
live registry untouched), each built by the script named from the source named:

| source | sync script | result |
|---|---|---|
| `ba5dfce` (main) | installed | RED, reproduced: PR #352's file `19 errors` |
| `ba5dfce` | `.github/` + `.gitignore` only, live-like `docs/reports` planted | 5 of `test_ci_gate_config.py` FAILED (`ruff.toml`, `docs/CI_LINT_BASELINE.md`) |
| `eb7e7c4` (this branch) | patched, live-like `docs/reports` planted | pytest `1058 passed, 21 skipped, 0 failed`; every skip the git family |

Merge behaviour, on a scratch mirror shaped like the live one: run-time `docs/reports` output
survived; a doc listed in the previous manifest was removed; manifest entries pointing outside
`docs/` were ignored. Deliberate break on a copy of the patched script: with the path guard removed,
a manifest entry `../outside/victim.txt` DELETED that file outside the mirror; with the guard it
survived; the copy was reverted to `cmp`-identical bytes.


## PR #390 runtime-write recovery status

The generation publisher serializes publication and the separate registry update with a
mirror-specific advisory lock. Runtime-only directories such as `docs/reports` refer to
retained storage instead of a copied directory. Runtime leaves in mixed directories use
symlinks to retained directory entries so both open-file appends and atomic replacement
through an already-open parent directory remain visible. Retired
trees are deliberately retained: the publisher must not delete backing storage or trees
that active readers might still require. Repeated publication preserves these references.

Publication builds a complete `MIRROR.generation-<unique-id>` directory and switches
the live mirror symlink to it. Its physical executable pathname stays fixed during
later publications. Initial migration uses a single native directory exchange
(`renamex_np(RENAME_SWAP)` on macOS, `renameat2(RENAME_EXCHANGE)` on Linux) to replace
the incumbent real directory with that symlink. The old directory lands at its runtime
backing path in the same operation. Unsupported platforms or filesystems fail before
changing the real directory; there is no two-rename fallback. Later publication uses
atomic symlink replacement. Completed generations and retired real directories are
retained conservatively, including after an interrupted publication syscall.

The tick reopens `orchestrate.sh` through `mirror_reader.py` after selecting the
physical generation under the publisher's shared lock. The helper exports that
physical `ORCH_DIR` and Python import path to children; `paths.py` also pins import
aliases and `checkout_root()` to its module generation. The inherited descriptor
stays open in the shell while it waits for Python children. For a permanently named
generation it is unlocked before exec, allowing publication while the tick runs.
For the incumbent real directory it stays locked until the reader exits, protecting
initial migration. Exec preserves the tick PID; the pinned tick arms its watchdog
before running steps. Generation selection runs before the watchdog, cadence registry,
and authentication preflight read executable modules. Authentication still precedes
heartbeat activation and dispatch. Bootstrap lock acquisition precedes watchdog arming.
Inherited descriptors are checked against the mirror-specific lock inode.
A synchronized observer loads an old module, completes two publications while
paused, then reads and imports old bytes again, including in a fresh child process.
Both active and shadow tick entries exercise this observer. The identical observer
crosses deployment bytes on the incumbent in-place publisher.

This is a partial recovery, not an atomic-publication completion claim. Standalone
Python entries wrapped with `mirror_reader.py run MIRROR python3 ...` now have a paired
production-publisher witness: the identical child observer crosses executable generations
on the incumbent in-place copier and stays on the old generation with the helper. Installed launchd commands
that do not use the wrapper still need migration and live verification after merge/pull. A regression
now covers creating new runtime leaves directly in mixed deployment/runtime directories
(`experiments/` with both shipped deployment bytes and runtime markers); existing runtime leaves
retain both append and atomic-replacement writes. The wrapper's
`--no-verify` route now stages the
incumbent copier under an isolated HOME and uses the same guarded publisher with
an explicit UNVERIFIED status. The direct-copier guard, initial real-directory exchange, overlapping publishers, and abrupt
process-death recovery now have the source witnesses listed below. Installed wrappers remain
unchanged until merge and pull; reader-command migration and guarded deployment evidence remain
pending. Source validation does not establish installed deployment completion.

The after-transfer regression test opens report/marker handles before installation, writes
after runtime transfer and after publication, creates a late report, and repeats installation.
It fails on the prior copying publisher and passes with shared runtime storage. A second
regression holds a mixed-directory descriptor and atomically replaces an existing marker
after transfer, after publication, and after retry. It fails with inode-only hard links
and passes with retained-entry references. Earlier
Node witnesses only observe the old tree before the switch and the new tree afterward;
they do not establish a pinned reader spanning publication.

### Guarded unverified wrapper route

After merge and pull, the wrapper block above invokes the pulled
`scripts/publish_unverified_snapshot.sh` for `--no-verify`. The helper runs the
installed legacy copier with a scratch `HOME` and `ORCH_MIRROR`, then hashes and
structurally validates that complete payload and calls its retained installer.
A copy failure or invalid payload leaves both live locations untouched. The
installer keeps the same exclusive publication lock and separate registry update,
but prints `installed UNVERIFIED snapshot`: the digest is an integrity binding,
not a verifier receipt. Neither helper nor wrapper runs `verify.py` on this route.
Direct invocation of the incumbent copier remains outside this protection. An isolated
real-copier witness pauses its first copy after deletion: a reader loses the live
module on the direct route, while the guarded unverified route keeps the live module
available because copying occurs in private staging. Both routes preserve the sampled
runtime report and marker; neither updates the separate live registry during staging.
That paired witness does not prove concurrent creation in mixed runtime directories
or authorize installing wrappers before merge/pull.


### Direct incumbent copier entry guard (pending merge and pull)

After merging and pulling #390, insert this immediately after the installed
`~/.codex/bin/orch-sync-mirror.sh` defines `SRC` and `MIRROR`, before its trap,
`mkdir`, `find`, or copy commands:

```bash
[[ -f "$SRC/scripts/incumbent_copy_guard.sh" ]] || {
  echo "NOT SYNCED: source has no incumbent copy guard; live mirror untouched." >&2
  exit 2
}
source "$SRC/scripts/incumbent_copy_guard.sh"
```

A direct call execs the guarded UNVERIFIED publisher and cannot fall back into
live deletion/copy when it returns. The verifier and unverified staging helpers
provide `ORCH_PRIVATE_COPY_ROOT`; the guard permits in-place copying only when
both HOME and MIRROR match that existing private root's staging layout. A stray
flag on the ordinary HOME or live mirror does not bypass the guard. Missing
publisher code fails before any live write. This does not verify the payload or
migrate standalone launchd readers. Installed files are unchanged until merge/pull;
paired source tests and isolated copier evidence must precede deployment claims.

The isolated actual installed copier (SHA256
`2db903979ab8f52ed151e55b5469da34d0ac7d4cb8ae0b0631e7518f18a52053`)
was replayed with and without this prologue. At its first copy, after deletion,
the same observer found `paths.py` missing on the incumbent and present on the
guarded direct route. Both runs completed and preserved sampled runtime report
and marker bytes. The registry stayed unchanged during staging. GH was stubbed
to its local fallback; this run proves registry preservation, not an authenticated
registry update. No installed copier or wrapper was edited. The source tests cover publication and recovery;
standalone launchd migration and post-merge deployment remain pending.

### Launchd reader migration (source evidence)

The primary hourly job (`com.stranske.orchestrator`) already runs
`orchestrate.sh --active` against the exec mirror. That entry re-acquires the
publisher shared lock through `mirror_reader.py` before reopening executable
code (see the `mirror-reader-reentry` anchor in `orchestrate.sh`). No plist edit
is required for that job once this branch merges and the mirror is refreshed.

Any additional LaunchAgent or cron line that executes Python directly against
`ORCH_MIRROR` must wrap the command as
`python3 "$ORCH/mirror_reader.py" run "$ORCH_REPO" …` so child imports pin one
generation. Source tests `test_tick_reader_excludes_real_publisher_across_child_imports`
and `test_standalone_python_reader_excludes_publisher_across_child_imports` are
the paired witnesses; post-merge live plist review remains operator-owned.

Overlapping publishers contend on the exclusive `.publish.lock` and never observe
a partial generation; `test_overlapping_publishers_serialize_on_exclusive_lock`
covers that serialization path beside the interruption and retry regressions.


### Abrupt publisher death and retry (source evidence)

`test_process_death_preserves_generation_runtime_and_retry` exits a real child
process with `os._exit`, bypassing Python cleanup at three boundaries: private
generation staging, immediately before the native directory exchange, and
immediately after exchange. Before exchange the old executable generation
remains complete; after exchange the new verified generation remains complete.
An already-open runtime report descriptor remains writable across process death
and a second publication, and the runtime marker survives both. The separate
registry remains old until retry writes the validated new registry.

Retry removes the abandoned `.next-<digest>` staging directory while holding the
publication lock. Completed generations and retired trees are retained conservatively,
including a prepared tree left by death before exchange: this publisher cannot prove that an
old tree has no reader or runtime backing references, so it never bulk-deletes
`.retired-*` or `.generation-*`. These tests cover process interruption, not machine power loss or
filesystem durability after reboot. Installed-wrapper and guarded deployment
evidence remains pending until merge and pull.


### Acceptance evidence and remaining deployment boundary

The bootstrap marker `agents/codex-389.md` is not acceptance evidence. Completion of the
source implementation is established by the production publisher and reader entry points:

| Claim from source #389 | Executable evidence |
|---|---|
| Complete payload validation before a reader-visible change | `test_payload_copy_failure_keeps_live_outputs_and_cleans_staging`, `test_corrupt_copy_is_rejected_before_live_mutation`, and `test_install_never_reopens_snapshot_after_staging_validation` use the production installer. |
| A reader and its child imports stay on one generation | `test_tick_reader_excludes_real_publisher_across_child_imports` covers both active and shadow entry; `test_standalone_python_reader_excludes_publisher_across_child_imports` pairs the same observer with an incumbent in-place publisher that crosses generations. `tests/test_mirror_generations.py` also asserts late sibling imports and a fresh child remain pinned across two completed publications. |
| Initial real-directory publication and concurrent runtime writes survive retry | `test_runtime_writes_after_transfer_survive_switch_and_retry`, `test_new_runtime_leaf_in_mixed_directory_survives_publication_and_retry`, and `test_runtime_leaf_atomic_replacement_survives_publication_and_retry` exercise real retained backing paths. |
| Overlapping publishers and interruption before/after exchange | `test_overlapping_publishers_serialize_on_exclusive_lock`, `test_atomic_publication_has_no_missing_live_path`, and the three `test_process_death_preserves_generation_runtime_and_retry` phases cover lock serialization, syscall boundaries, abrupt death, and cleanup/retry. |
| Separate registry failure has an explicit retry disposition | The six `test_runtime_registry_sync_order_cleanup_and_retry` cases verify file-sync/replace/directory-sync order, retained old or new registry state, exception propagation, and successful retry. |
| Copying without verification retains publication safety | `test_unverified_route_stages_copier_and_never_claims_verification` covers success and preparation failures. `test_direct_copier_guard_keeps_live_tree_available` pairs guarded and incumbent entry routes, including copy failure. The actual installed copier replay above supplies an additional local integration witness; GH fallback is not authenticated registry evidence. |

The previous six-file acceptance command in source #389 passed 144 cases; the count
included the positive publisher tests and their negative controls, rather than treating an
incumbent-defect witness as proof of repair. That result predates the permanent-generation
recovery and must be rerun. The additional standard-library witness runs with
`PYTHONPATH=.:src python3 -m unittest discover -s tests -p test_mirror_generations.py -v`.
Its witnesses cover the paired observer, active/shadow tick entry (including startup
modules), refusal of authentication after pinning, link-switch interruption/retry,
and isolation of retained generations from the separate registry. Python bytecode
(`__pycache__/`, `.pyc`, and `.pyo`) stays with its old generation rather than becoming
shared runtime storage: a timestamp/size-valid cache can otherwise override changed
verified source bytes. The cache witness forces that collision and checks both new
imports and the retained reader's old code, alongside preserved runtime reports.
Shell syntax validation covers `orchestrate.sh`,
`verify_before_sync.sh`, `publish_unverified_snapshot.sh`, and `incumbent_copy_guard.sh`.
These results establish source behavior and process-interruption recovery, not power-loss
durability or a full `verify.py` verdict.

Deployment remains pending until all of the following are observed after gated merge and pull:

1. Record the pulled commit and confirm both installed wrappers match the documented
   publication paths, including the direct copier's entry guard.
2. Inspect actual launchd/cron commands. The primary `orchestrate.sh --active` entry takes
   the reader lock; wrap any auxiliary Python command that reads the mirror with
   `mirror_reader.py run` before claiming universal reader coverage.
3. Run the guarded verified publication, retain its receipt and exact deployment digest,
   and check runtime reports/markers and the separate registry at the installed locations.
4. Inspect durable `verify:compare` output and disposition source #389. Keep the source
   issue open until deployment and verifier evidence are complete.

### Capturing installed observations after merge and pull

The documented verified-wrapper block accepts `ORCH_VERIFIED_RECEIPT_OUT` to retain the
one-line verifier digest outside its temporary staging directory. It refuses an existing
receipt path before publication, so each run needs a fresh path. A retained verifier receipt
alone does not prove that publication succeeded. Install this block only after merge/pull,
along with the direct copier entry guard above.

After the gated merge, pull, and installed-wrapper update, use a fresh evidence directory:

```bash
set -o pipefail
source_root="$HOME/.codex/orchestrator-src"
evidence_dir="$(mktemp -d "${TMPDIR:-/tmp}/orch-deployment-evidence.XXXXXX")"
git -C "$source_root" rev-parse HEAD > "$evidence_dir/pulled-commit.txt"
if ORCH_VERIFIED_RECEIPT_OUT="$evidence_dir/verified-payload.sha256" \
  bash "$HOME/.codex/bin/orch-mirror-sync.sh" "$source_root" \
  2>&1 | tee "$evidence_dir/publication.log"; then
  publication_rc=0
else
  publication_rc=$?
fi
printf '%s\n' "$publication_rc" > "$evidence_dir/publication-exit.txt"
[[ "$publication_rc" == 0 ]] || exit "$publication_rc"
node "$source_root/scripts/capture_mirror_deployment_evidence.js" \
  "$source_root" "$HOME/.codex/orchestrator-mirror" \
  "$evidence_dir/verified-payload.sha256" \
  "$HOME/.codex/orchestrator/repo_review_registry.json" \
  "$HOME/.codex/bin" "$evidence_dir/publication.log" "$evidence_dir/observations.json"
```

The Node collector reads installed state and writes only the new observation file. It records
the checkout commit, both installed-wrapper SHA256 values and presence of their documented
blocks, the retained receipt and physical-generation digest, publication-log SHA256 and
success markers, and the separate registry's correspondence with the generation. A mismatch
returns exit 2. A publication overlapping collection also returns exit 2; collect again using
the receipt and log for the currently active generation. Existing evidence is never overwritten.
Move the evidence directory to retained operator storage before clearing temporary files.

Even matching observations carry `deployment_status: pending-operator-review`: block presence
does not prove wrapper control-flow placement, and log markers do not prove command exit.
Review the retained exit status, match the checkout commit to the actual merge/pull, inspect
launchd/cron entries, compare runtime reports/markers before and after publication, and retain
durable `verify:compare` output before checking the deployment task complete. This repository
change does not install wrappers or supply those live observations.

## One copy contract: the repository defines what the mirror carries (2026-10-04)

**The incident.** On 2026-10-04 the owner's verified sync of main was refused, for three independent
defects that existed only in the flat exec mirror (#408): four tests read `src/<module>` by path,
three loaders seeded deployment-owned registries under the verdict, and a test child seeded one
into the deployable tree. Each was green in CI on its own PR, because CI verified a CHECKOUT. The
only check of the flat shape was `scripts/verify_before_sync.sh`, and it runs when the owner syncs,
after merge.

**What changed.**

- `scripts/build_exec_mirror.sh` builds everything the mirror carries FROM THIS REPOSITORY. It is the
  repository part of `~/.codex/bin/orch-sync-mirror.sh`, moved without changing what it does: the
  same files, the same `git archive` drains, the same `docs/` merge and the same executable bits.
  One deliberate exception, reachable only if the repository ever drops `docs/` entirely: the
  previous manifest's docs and the manifest itself are then removed, where the copier left them for
  the installer to deploy. It needs a git checkout and an explicit destination, and has no default
  that could name the live mirror. Witnessed on 2026-10-04 by running the installed copier and the builder on the same source
  into two scratch trees: 2,103 entries each, identical in contents and permissions. The one
  difference is `repo_review_registry.json`, which the copier fetches from Workflows.
- CI's new `exec-mirror` job runs `scripts/verify_before_sync.sh` itself, with
  `ORCH_SYNC_SCRIPT=scripts/build_exec_mirror.sh`. A PR is therefore judged in the flat shape, by the
  same procedure and the same payload-digest check as a sync. A mirror-only defect now fails on the
  PR that introduces it.
- On a runner that flat copy is a THIRD deprived shape. Like the owner's mirror, it is flat and not a
  repository, so it skips the git family. Like a runner checkout, it has no local prerequisite, so
  it skips the runner family too. `env_prereq.bare_machine()` detects that machine from three
  marks: no agent seat with a CLI or credential file, no reference skill resource, and no
  version-capable Codex binary. `verify.tree_shape()` then prints `tree: BARE EXEC MIRROR` and
  applies `bare_mirror_*` from `.verify-floor.json`, measured by that job. If any mark is present,
  or one cannot be read, the machine is provisioned and the owner's `mirror_*` agreement applies.
- `verify_before_sync.sh` takes `VERIFY_BEFORE_SYNC_TREE`, the `tree:` label it requires: `EXEC
  MIRROR` by default, `BARE EXEC MIRROR` in CI. The match is on the label's start, so the owner's
  sync refuses a bare classification and its larger ceilings, and CI refuses the provisioned one.
  `VERIFY_BEFORE_SYNC_FLOOR_MAY_LAG=1` passes `--floor-may-lag`: a lagging floor is the checkout
  job's business, and a collection drop still fails.
- The source identity now covers `AGENTS.md` and `ORCHESTRATOR.md`, which the copy reads from the
  working tree, and `install_verified_snapshot.OWNED_FILES` now owns both. Before, the installer
  never installed them, so the live mirror kept stale copies. `tests/test_build_exec_mirror.py`
  fails any file the builder ships that the installer does not own, and any working-tree input the
  identity does not cover.

**Every sync now says whether CI verified what it ships.** `verify_before_sync.sh` also builds the
contract's tree from the same source, before `verify.py` runs, and compares it with the copier's,
leaving out the registry. One line reports the result, `copy contract: ... built exactly the tree
scripts/build_exec_mirror.sh builds` or `!! copy contract ... built DIFFERENT trees`, naming the
first differences. It is FYI only and never changes the verdict, because the sync still judges the
copier's own tree, which is what deploys. Until the copier below is installed, the two agree only
because neither has changed since the move, and the first change to the builder makes this line
report the difference.

**The copier, whole.** After this change is merged and `~/.codex/orchestrator-src` is pulled,
replace the contents of `~/.codex/bin/orch-sync-mirror.sh` with the block below. It keeps only what
is not repository content, the Workflows registry, and calls the builder for everything else, so a
sync copies exactly what it copied before and nothing about HOW the copier is called changes.

It deliberately does NOT carry #390's entry guard. That guard routes a direct call through the
guarded publisher, which is #389's deployment step and belongs with that section's wrapper. If you
have already inserted the guard ("Direct incumbent copier entry guard" above), keep its five lines
immediately after the `MIRROR=` line. If you have not, that section's insertion applies to this file
unchanged when you deploy #389, at the same place.

```bash
#!/usr/bin/env bash
# orch-sync-mirror.sh — copy the canonical orchestrator CODE to the local exec-mirror launchd runs.
#
# WHY: launchd (and cron) cannot read macOS CloudStorage/Dropbox paths — they get EPERM ("Operation
# not permitted"), and a symlink does NOT bypass it. So the scheduled tick runs from local disk, and
# this copies a checkout there. Run it from an interactive context, through orch-mirror-sync.sh.
#
# WHAT IT COPIES IS DEFINED IN THE REPOSITORY (2026-10-04). $SRC/scripts/build_exec_mirror.sh builds
# everything that comes from the repository, and CI's exec-mirror job builds its flat copy with the
# same script, so the tree CI verifies on every PR is the tree this copies. Never add a copy step
# here: add it to that script, in the change that needs the file. This keeps only what is not
# repository content, the Workflows registry. (docs/MIRROR_SYNC_PATCH.md, "One copy contract")
set -euo pipefail
SRC="${1:-$HOME/Library/CloudStorage/Dropbox/Learning/Code/Orchestrator}"
MIRROR="${ORCH_MIRROR:-$HOME/.codex/orchestrator-mirror}"
trap 'echo "ERR: orch-sync-mirror.sh aborted at line $LINENO (rc $?): $MIRROR is HALF-SYNCED -- fix the cause and re-run before trusting a mirror verify." >&2' ERR
if [[ ! -f "$SRC/orchestrate.sh" ]]; then
  echo "ERR: no orchestrate.sh in $SRC — Dropbox unreadable (launchd context?) or wrong path." >&2
  exit 1
fi
BUILDER="$SRC/scripts/build_exec_mirror.sh"
if [[ ! -f "$BUILDER" ]]; then
  echo "NOT SYNCED: $SRC has no scripts/build_exec_mirror.sh, which defines what the mirror carries; pull the clone first. $MIRROR was not touched." >&2
  exit 2
fi
bash "$BUILDER" "$SRC" "$MIRROR"
# repo_review_registry.json lives in the Workflows repo (NOT $SRC). keepalive_outcomes.py reads it,
# and launchd cannot read CloudStorage paths, so the mirror carries a local copy.
WF_REGISTRY="$HOME/Library/CloudStorage/Dropbox/Learning/Code/Workflows/config/repo_review_registry.json"
# PREFER WHAT IS MERGED ON GITHUB. The Dropbox checkout of Workflows is a partial clone pulled by
# hand, so copying it could silently revert a registry change merged upstream. `gh` reads main
# directly; the Dropbox file is the fallback when GitHub is unreachable.
WF_REGISTRY_GH="$(mktemp)"
if gh api repos/stranske/Workflows/contents/config/repo_review_registry.json --jq .content 2>/dev/null | base64 -d > "$WF_REGISTRY_GH" 2>/dev/null \
   && python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$WF_REGISTRY_GH" 2>/dev/null; then
  registry_src="$WF_REGISTRY_GH"; registry_from="GitHub main"
elif [[ -f "$WF_REGISTRY" ]]; then
  registry_src="$WF_REGISTRY"; registry_from="Dropbox checkout (GitHub unreachable)"
else
  registry_src=""; registry_from="none"
fi
if [[ -n "$registry_src" ]]; then
  cp "$registry_src" "$MIRROR/repo_review_registry.json"
  cp "$registry_src" "$HOME/.codex/orchestrator/repo_review_registry.json" 2>/dev/null || true
  echo "repo_review_registry.json from $registry_from"
else
  echo "repo_review_registry.json: NONE (GitHub unreachable and no Dropbox checkout); the mirror keeps whatever copy it had" >&2
fi
rm -f "$WF_REGISTRY_GH"
```

The steps, about two minutes, once. Pull, keep a backup, write the block above over the file (the
one-liner extracts it from this document exactly as the tests do), and check the syntax:

```bash
git -C ~/.codex/orchestrator-src pull --ff-only
```

```bash
cp ~/.codex/bin/orch-sync-mirror.sh ~/.codex/bin/orch-sync-mirror.sh.bak-2026-10-04
```

```bash
python3 -c 'import pathlib as p; h=p.Path.home(); t=(h/".codex/orchestrator-src/docs/MIRROR_SYNC_PATCH.md").read_text(); i=t.index("**The copier, whole.**"); s=t.index("```bash\n",i)+8; (h/".codex/bin/orch-sync-mirror.sh").write_text(t[s:t.index("\n```",s)]+"\n")'
```

```bash
bash -n ~/.codex/bin/orch-sync-mirror.sh && grep -c build_exec_mirror ~/.codex/bin/orch-sync-mirror.sh
```

Then sync as usual. The pre-sync output carries `copy contract: ... built exactly the tree
scripts/build_exec_mirror.sh builds, which CI's exec-mirror job verifies`; to undo, copy the backup
back. `tests/test_build_exec_mirror.py` extracts the block above and runs it in a scratch world: it
builds through the builder and adds only the registry, and it refuses a source without the builder
before writing anything. It also inserts #390's documented guard exactly where that section says and
checks the result: a staging call still builds, a direct call goes to the guarded publisher, and
the guard block is present byte for byte, which is what the deployment-evidence collector looks for.

**Latched-gate answers for CI's `exec-mirror` job and the `bare_mirror_*` ceilings.**

1. *What clears a red?* The PR that fixes the mirror-only defect, or that ships the missing file from
   the builder. Otherwise, a deliberate ceiling raise that names what goes unchecked.
2. *Can that run while the gate is closed?* Yes. Nothing here blocks a push, a re-run or the floor
   reconcile job, and a fix is judged by the same job that went red.
3. *Measuring window = draining window?* Each `bare_mirror_*` number is measured by this job, the one
   place that shape is detected, and read only there through `verify.ceiling_limit`. The digest is
   the installer's one function, taken before and after verification.
4. *What does it print when drained?* `tree: BARE EXEC MIRROR — bare_mirror_* ceilings apply`, each
   ceiling as `N/N max [bare_mirror_...]`, and `== verify-before-sync: VERIFIED`. Every green run of
   the job is an input that produced it, and `verify.py --selftest` renders it by construction.

The copy-contract line is not a gate: it reports and never blocks, and it has no state that could
latch.
