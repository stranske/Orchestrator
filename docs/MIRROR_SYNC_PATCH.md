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
come down to 19 in the same change.

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
- **A run in the live mirror can read a torn tree.** A second sync landing while it runs leaves it
  reading a mix of old and new files.

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
4. With `--snapshot-out DIR`, it publishes the verified scratch mirror only after every gate is
   green. The wrapper installs that snapshot with its verified copy of
   `scripts/install_verified_snapshot.py`; it never re-reads mutable `SRC` or fetches the registry a
   second time.

Its exit codes are 0 VERIFIED, 1 NOT VERIFIED, 2 nothing verified, and 3 VOID (`SRC` moved). It
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
  "$HOME/.codex/bin/orch-sync-mirror.sh" "$SRC"
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
echo
echo "== verdict BEFORE the live copy, from a scratch mirror of $SRC"
if ! bash "$PRE" --snapshot-out "$SNAPSHOT" "$SRC"; then
  echo >&2
  echo "NOT SYNCED: the live mirror still runs the code it ran before this command." >&2
  print_unverified_override
  exit 3
fi
INSTALLER="$SNAPSHOT/scripts/install_verified_snapshot.py"
if [[ ! -f "$INSTALLER" ]]; then
  echo "NOT SYNCED: the verified snapshot has no installer." >&2
  print_unverified_override
  exit 3
fi
echo
echo "== installing the verified snapshot (SRC is not read again)"
python3 "$INSTALLER" "$SNAPSHOT" "$MIRROR" \
  --runtime-registry "$HOME/.codex/orchestrator/repo_review_registry.json"
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
   before and after the run, publishes them only on green, and the wrapper installs those same bytes.
   `SRC` is never read again. A changed source or changed scratch payload returns VOID.
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
  - a source that changes during the live copy exits 4.
- The live mirror's `orchestrate.sh` and the live registry copy did not move.
- The real copy script has not yet been run through the wrapper. The confirmation step above is
  that witness.
