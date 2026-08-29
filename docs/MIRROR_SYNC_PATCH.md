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
