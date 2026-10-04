#!/usr/bin/env bash
# verify_before_sync.sh — this machine's verdict on a tree BEFORE the live exec mirror runs it.
#
# WHY (2026-10-02, the owner's decision). verify.py checks the WHOLE tree it runs in, never one
# session's work, so on any one tree only the latest run counts. CI already runs the same suite on
# every PR head and on main. What only THIS machine can check is the populated capability ledger,
# the installed agent CLIs and the flat mirror shape, so this is the one local run a sync needs.
# The sync wrapper used to take it AFTER copying into the live mirror: a red verdict arrived after
# launchd could already run the code, and a sync landing during a mirror run left that run reading
# a half-old, half-new tree.
#
# This builds a throwaway mirror from the sync's own source with the real sync script, isolated as
# docs/MIRROR_SYNC_PATCH.md prescribes. The scratch HOME and ORCH_MIRROR mean neither the live
# mirror nor the live registry copy is written, and GH_CONFIG_DIR keeps gh authenticated.
#
# It runs verify.py there against a SCRATCH COPY of the live state. verify.py's `ledger validate`
# gate is a WRITING load (`capabilities.load(create=True)` reconciles declarations and expires rows,
# then saves), and taken before the copy it would write the live ledger with the declarations of a
# tree that is not deployed, even when the verdict then stops the copy. The copy holds:
#   - the top-level files: the ledger, the plans and the .last-* stamps;
#   - the Brain, through SQLite's backup API;
#   - every directory up to VERIFY_BEFORE_SYNC_MAX_DIR_MB. Larger ones are skipped and named.
# HOME stays real, so the installed CLIs and skills are seen. Afterwards it re-checks that the
# source did not move while verify.py ran. It never touches the live mirror, the live ledger or the
# live Brain, and never runs the live sync: what to do with the verdict is the caller's decision.
#
# Usage: scripts/verify_before_sync.sh [--snapshot-out DIR] [--digest-out FILE] [SRC]
#                                            (SRC defaults to ~/.codex/orchestrator-src)
#        scripts/verify_before_sync.sh --identity [SRC]
#        prints a fingerprint of exactly what the copy reads from SRC, the same value a
#        VERIFIED run prints; retained for diagnostics, not as the wrapper's deployment gate
# Exit:  0 VERIFIED
#        1 NOT VERIFIED: verify.py failed (its exit code is printed), or passed without judging
#          the scratch tree as the exec-mirror shape, so the mirror's ceilings were not applied
#        2 NOTHING VERIFIED: SRC is not a checkout, or the scratch copy failed
#        3 VOID: SRC changed while verify.py ran, so the verdict is about a tree nobody would copy
# Env:   ORCH_SYNC_SCRIPT  the copy script (default ~/.codex/bin/orch-sync-mirror.sh)
#        PYTHON            the interpreter for verify.py (default python3)
#        ORCH_LOCAL_RUNTIME, ORCH_STATE_DIR, ORCH_CAPABILITIES_PATH, ORCH_FEEDBACK_DB
#                          where the live state is READ from (each defaults as the modules do);
#                          verify.py is handed the copies, never these
#        VERIFY_BEFORE_SYNC_MAX_DIR_MB  largest state directory copied (default 50)
#        VERIFY_BEFORE_SYNC_KEEP=1  keep the scratch directory and print its path
#
# No here-documents or here-strings: bash 5.3 feeds them through a pipe, and a pipe can block
# forever once system pipe memory is exhausted (tests/test_orchestrate_no_heredoc.py).
set -uo pipefail

real_home="$HOME"
mode="verify"
if [[ "${1:-}" == "--identity" ]]; then
  mode="identity"
  shift
fi
snapshot_out=""
if [[ "${1:-}" == "--snapshot-out" ]]; then
  [[ -n "${2:-}" ]] || { printf 'verify-before-sync: --snapshot-out needs a directory\n' >&2; exit 2; }
  snapshot_out="$2"
  shift 2
fi
digest_out=""
if [[ "${1:-}" == "--digest-out" ]]; then
  [[ -n "${2:-}" ]] || { printf 'verify-before-sync: --digest-out needs a file\n' >&2; exit 2; }
  digest_out="$2"
  shift 2
fi
if [[ -n "$digest_out" && -z "$snapshot_out" ]]; then
  printf 'verify-before-sync: --digest-out requires --snapshot-out\n' >&2
  exit 2
fi
[[ "$#" -le 1 ]] || { printf 'verify-before-sync: unexpected arguments\n' >&2; exit 2; }
src="${1:-$real_home/.codex/orchestrator-src}"
sync_script="${ORCH_SYNC_SCRIPT:-$real_home/.codex/bin/orch-sync-mirror.sh}"
python_bin="${PYTHON:-python3}"
gh_config="${GH_CONFIG_DIR:-$real_home/.config/gh}"
runtime_src="${ORCH_LOCAL_RUNTIME:-$real_home/.codex/orchestrator}"
statedir_src="${ORCH_STATE_DIR:-$real_home/.codex/orchestrator}"
ledger_src="${ORCH_CAPABILITIES_PATH:-$runtime_src/capabilities.json}"
brain_src="${ORCH_FEEDBACK_DB:-$runtime_src/feedback/orchestrator.db}"
max_dir_mb="${VERIFY_BEFORE_SYNC_MAX_DIR_MB:-50}"
skipped_dirs=""

say() { printf '%s\n' "$*"; }
fail() { printf 'verify-before-sync: %s\n' "$*" >&2; }

# A consistent snapshot of a SQLite database that may be mid-write: the backup API, read-only.
backup_sqlite() {
  "$python_bin" -c 'import pathlib, sqlite3, sys
src = sqlite3.connect(pathlib.Path(sys.argv[1]).resolve().as_uri() + "?mode=ro", uri=True)
dst = sqlite3.connect(sys.argv[2])
src.backup(dst)
dst.close()
src.close()' "$1" "$2"
}

# copy_state FROM TO: every top-level file, every *.db through backup_sqlite, and every directory
# up to max_dir_mb. A larger directory is skipped and named, never silently absent.
copy_state() {
  local from="$1" to="$2" entry name size_kb inner
  mkdir -p "$to" || return 1
  [[ -d "$from" ]] || return 0
  for entry in "$from"/* "$from"/.[!.]*; do
    [[ -e "$entry" ]] || continue
    name="$(basename "$entry")"
    if [[ -f "$entry" ]]; then
      case "$name" in
        *.db) backup_sqlite "$entry" "$to/$name" || return 1 ;;
        *.db-wal | *.db-shm) ;;
        *) cp -p "$entry" "$to/$name" || return 1 ;;
      esac
    elif [[ -d "$entry" && "$name" == "feedback" ]]; then
      copy_state "$entry" "$to/$name" || return 1
    elif [[ -d "$entry" ]]; then
      size_kb="$(du -sk "$entry" 2>/dev/null | cut -f1)"
      if [[ -n "$size_kb" && "$size_kb" -le $((max_dir_mb * 1024)) ]]; then
        cp -Rp "$entry" "$to/$name" || return 1
      else
        inner="$((${size_kb:-0} / 1024)) MB"
        skipped_dirs="$skipped_dirs $name ($inner)"
      fi
    fi
  done
}

# What the sync copies: a non-recursive module glob and selected root files from the WORKING TREE,
# plus tests/ and scripts/ from HEAD. Hash those exact working-tree inputs, including ignored
# modules; Git status is not the copy contract and can omit bytes that cp will deploy.
source_identity() {
  git -C "$src" rev-parse HEAD || return 1
  "$python_bin" -c 'import hashlib, os, pathlib, struct, sys
src = pathlib.Path(sys.argv[1]).resolve()
h = hashlib.sha256()
def add(label, payload):
    name = label.encode("utf-8", "surrogateescape")
    h.update(struct.pack(">Q", len(name)) + name)
    h.update(struct.pack(">Q", len(payload)) + payload)
mod = src / "src" if (src / "src").is_dir() else src
add("module-layout", mod.relative_to(src).as_posix().encode())
modules = sorted((p for p in mod.glob("*.py") if not p.name.startswith(".")), key=lambda p: os.fsencode(p.name))
if not modules:
    raise SystemExit(f"no Python modules selected from {mod}")
for path in modules:
    add(f"mirror/{path.name}", path.read_bytes())
for label, path in (
    ("mirror/orchestrate.sh", src / "orchestrate.sh"),
    ("mirror/experiments/hypotheses.json", src / "experiments/hypotheses.json"),
    ("mirror/experiments/features.json", src / "experiments/features.json"),
    ("mirror/experiments/repo_knowledge.json", src / "experiments/repo_knowledge.json"),
    ("mirror/data/feedback-snapshot.json", src / "data/feedback-snapshot.json"),
    ("mirror/config/coverage-baseline.json", src / "config/coverage-baseline.json"),
    ("mirror/.verify-floor.json", src / ".verify-floor.json"),
    ("mirror/CLAUDE.md", src / "CLAUDE.md"),
    ("mirror/IMPROVEMENT_BACKLOG.md", src / "IMPROVEMENT_BACKLOG.md"),
):
    add(label, path.read_bytes() if path.is_file() else b"<absent>")
config = src / "pyproject.toml"
if not config.is_file():
    config = src / ".coveragerc"
config_name = config.name if config.is_file() else "coverage-config"
add(f"mirror/{config_name}", config.read_bytes() if config.is_file() else b"<absent>")
print(h.hexdigest())' "$src" || return 1
}

# One line, so a caller can hold it: the fingerprint of the identity above.
fingerprint() { printf '%s' "$1" | git hash-object --stdin; }

if ! git -C "$src" rev-parse --git-dir >/dev/null 2>&1; then
  fail "NOTHING VERIFIED: $src is not a git checkout"
  exit 2
fi
if [[ "$mode" == "identity" ]]; then
  identity_now="$(source_identity)" || { fail "could not read the copy inputs of $src"; exit 2; }
  fingerprint "$identity_now"
  exit 0
fi
if ! before="$(source_identity)"; then
  fail "NOTHING VERIFIED: could not read the state of $src"
  exit 2
fi
if [[ -n "$snapshot_out" && ( -e "$snapshot_out" || -L "$snapshot_out" ) ]]; then
  fail "NOTHING VERIFIED: snapshot output already exists: $snapshot_out"
  exit 2
fi
if [[ -n "$digest_out" && ( -e "$digest_out" || -L "$digest_out" ) ]]; then
  fail "NOTHING VERIFIED: digest output already exists: $digest_out"
  exit 2
fi
head_short="$(git -C "$src" rev-parse --short HEAD)"
dirty="$(git -C "$src" status --porcelain | wc -l | tr -d ' ')"

if ! scratch="$(mktemp -d "${TMPDIR:-/tmp}/verify-before-sync.XXXXXX")"; then
  fail "NOTHING VERIFIED: could not create a scratch directory"
  exit 2
fi
# CANONICAL, because the live run's paths are. macOS's TMPDIR is /var/folders/..., a symlink to
# /private/var/..., and mktemp keeps TMPDIR's trailing slash as `//`. Handed to verify.py that way,
# the dispatcher selftest compares a resolved worktree path with an unresolved one and fails on
# every input (measured 2026-10-02, twice, on a tree that passes everywhere else). The live state,
# ~/.codex/orchestrator, is no symlink, so a scratch path that resolves differently is not the
# environment being vouched for.
if ! scratch_real="$(cd "$scratch" && pwd -P)" || [[ -z "$scratch_real" ]]; then
  fail "NOTHING VERIFIED: could not resolve the scratch directory $scratch"
  exit 2
fi
scratch="$scratch_real"
if [[ "${VERIFY_BEFORE_SYNC_KEEP:-0}" == "1" ]]; then
  say "scratch directory kept: $scratch"
else
  trap 'rm -rf "$scratch"' EXIT
fi
mkdir -p "$scratch/home/.codex/orchestrator"

say "== scratch mirror of $src @ $head_short ($dirty uncommitted) -> $scratch/mirror"
if ! GH_CONFIG_DIR="$gh_config" GH_NO_UPDATE_NOTIFIER=1 ORCH_PRIVATE_COPY_ROOT="$scratch" HOME="$scratch/home" \
  ORCH_MIRROR="$scratch/mirror" bash "$sync_script" "$src"; then
  fail "NOTHING VERIFIED: the scratch copy failed (see above); the live mirror was not touched"
  exit 2
fi
if [[ ! -f "$scratch/mirror/verify.py" ]]; then
  fail "NOTHING VERIFIED: the scratch copy holds no verify.py"
  exit 2
fi
installer="$scratch/mirror/scripts/install_verified_snapshot.py"
if [[ ! -f "$installer" ]]; then
  fail "NOTHING VERIFIED: the scratch copy holds no verified-snapshot installer"
  exit 2
fi
if ! payload_before="$("$python_bin" "$installer" "$scratch/mirror" --digest)"; then
  fail "NOTHING VERIFIED: could not hash the scratch deployment payload"
  exit 2
fi

runtime_copy="$scratch/state/runtime"
statedir_copy="$runtime_copy"
say "== scratch copy of the live state ($runtime_src), which verify.py may read AND write"
if ! copy_state "$runtime_src" "$runtime_copy"; then
  fail "NOTHING VERIFIED: could not copy the live state from $runtime_src"
  exit 2
fi
if [[ "$statedir_src" != "$runtime_src" ]]; then
  statedir_copy="$scratch/state/statedir"
  if ! copy_state "$statedir_src" "$statedir_copy"; then
    fail "NOTHING VERIFIED: could not copy the state directory $statedir_src"
    exit 2
  fi
fi
# A ledger or a Brain kept outside the runtime directory is brought in beside the rest.
ledger_copy="$runtime_copy/capabilities.json"
if [[ -f "$ledger_src" && "$ledger_src" != "$runtime_src/capabilities.json" ]]; then
  cp -p "$ledger_src" "$ledger_copy" || { fail "NOTHING VERIFIED: could not copy $ledger_src"; exit 2; }
fi
brain_copy="$runtime_copy/feedback/orchestrator.db"
if [[ -f "$brain_src" && "$brain_src" != "$runtime_src/feedback/orchestrator.db" ]]; then
  mkdir -p "$runtime_copy/feedback"
  backup_sqlite "$brain_src" "$brain_copy" || { fail "NOTHING VERIFIED: could not copy $brain_src"; exit 2; }
fi
if [[ -n "$skipped_dirs" ]]; then
  say "   not copied, over $max_dir_mb MB (checks that need them see nothing):$skipped_dirs"
fi

# features.py, repo_knowledge.py and research_scheduler.py SEED their registry on first load, at
# MODULE_DIR/experiments/ unless their variable names another path. In the flat mirror that is the
# deployment-owned experiments/*.json (install_verified_snapshot.OWNED_FILES), which the copy script
# ships only when the source has them, and a clean clone never does. Seeded there mid-run, they
# changed the payload under the verdict and VOIDed every verified sync from such a source
# (2026-10-04). So they load from here, beside verify.py's other writes. A registry the source
# ships is copied in, so verification judges the content that will deploy while every write stays
# in the copy; one it does not ship stays absent and seeds here, as the live mirror's first load will.
registry_copy="$scratch/state/registries"
if ! mkdir -p "$registry_copy"; then
  fail "NOTHING VERIFIED: could not create the scratch registry directory $registry_copy"
  exit 2
fi
for registry in features.json repo_knowledge.json hypotheses.json; do
  shipped="$scratch/mirror/experiments/$registry"
  if [[ -f "$shipped" ]] && ! cp -p "$shipped" "$registry_copy/$registry"; then
    fail "NOTHING VERIFIED: could not copy the shipped registry $shipped"
    exit 2
  fi
done
say "== verify.py in the scratch mirror, on the state copy (HOME stays real for the installed CLIs)"
(cd "$scratch/mirror" && ORCH_LOCAL_RUNTIME="$runtime_copy" ORCH_STATE_DIR="$statedir_copy" \
  ORCH_CAPABILITIES_PATH="$ledger_copy" ORCH_FEEDBACK_DB="$brain_copy" \
  ORCH_FEATURES_PATH="$registry_copy/features.json" \
  ORCH_REPO_KNOWLEDGE_PATH="$registry_copy/repo_knowledge.json" \
  ORCH_HYP_PATH="$registry_copy/hypotheses.json" \
  "$python_bin" verify.py) 2>&1 | tee "$scratch/verify.log"
pipe_status=("${PIPESTATUS[@]}")
rc=${pipe_status[0]}
tee_rc=${pipe_status[1]}

if [[ "$tee_rc" != "0" ]]; then
  fail "NOTHING VERIFIED: could not capture complete verify.py evidence (tee exit $tee_rc)"
  exit 2
fi

shape_ok=1
if ! grep -q 'tree: *EXEC MIRROR' "$scratch/verify.log"; then
  shape_ok=0
  say "!! verify.py did not judge the scratch tree as the exec-mirror shape, so it applied the"
  say "   checkout's ceilings: this is not the mirror's verdict, and it is NOT VERIFIED"
fi
if ! after="$(source_identity)" || [[ "$after" != "$before" ]]; then
  fail "VOID: $src changed while verify.py ran, so this verdict is about a tree the sync would"
  fail "not copy. Re-run once the source is settled."
  exit 3
fi
if ! payload_after="$("$python_bin" "$installer" "$scratch/mirror" --digest)"; then
  fail "VOID: could not re-hash the scratch deployment payload after verification"
  exit 3
fi
if [[ "$payload_after" != "$payload_before" ]]; then
  fail "VOID: verification changed deployment-owned bytes in the scratch mirror"
  exit 3
fi

if [[ "$rc" == "0" && "$shape_ok" == "1" ]]; then
  verdict="VERIFIED"
elif [[ "$rc" == "0" ]]; then
  verdict="NOT VERIFIED (verify.py passed, but not in the exec-mirror shape)"
else
  verdict="NOT VERIFIED (verify.py exit $rc)"
fi
say ""
say "== verify-before-sync: $verdict for $src @ $head_short ($dirty uncommitted)"
say "   scratch mirror built by $sync_script, verified on a copy of $runtime_src; the live mirror,"
say "   the live registry copy, the live ledger and the live Brain were not written"
if [[ "$rc" == "0" && "$shape_ok" == "1" ]]; then
  say "   verified source identity: $(fingerprint "$before")"
  if [[ -n "$snapshot_out" ]]; then
    digest_tmp="${digest_out}.tmp.$$"
    if [[ -n "$digest_out" ]] && {
      ! mkdir -p "$(dirname "$digest_out")" ||
      ! printf '%s\n' "$payload_after" > "$digest_tmp"
    }; then
      rm -f "$digest_tmp"
      fail "NOTHING VERIFIED: could not prepare the verified digest receipt at $digest_out"
      exit 2
    fi
    if ! mkdir -p "$(dirname "$snapshot_out")" || ! mv "$scratch/mirror" "$snapshot_out"; then
      rm -f "$digest_tmp"
      fail "NOTHING VERIFIED: could not publish the verified snapshot to $snapshot_out"
      exit 2
    fi
    if [[ -n "$digest_out" ]] && ! mv "$digest_tmp" "$digest_out"; then
      rm -f "$digest_tmp"
      fail "NOTHING VERIFIED: could not publish the verified digest receipt to $digest_out"
      exit 2
    fi
    say "   verified deployment snapshot: $snapshot_out"
  fi
  exit 0
fi
exit 1
