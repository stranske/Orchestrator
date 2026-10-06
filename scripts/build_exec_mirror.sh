#!/usr/bin/env bash
# build_exec_mirror.sh — everything THIS REPOSITORY puts into the flat exec mirror. The copy
# contract, defined once.
#
# WHY IT LIVES HERE (2026-10-04). Until then the whole contract lived in
# ~/.codex/bin/orch-sync-mirror.sh, a file on one machine that no CI job can read. CI therefore
# verified a CHECKOUT, and three defects that exist only in the flat copy reached main green; the
# owner's sync found them (#408). CI now builds its flat copy with this script, and the owner's
# copier calls this script for everything it takes from the repository (docs/MIRROR_SYNC_PATCH.md,
# "One copy contract"). The tree CI judges and the tree a sync ships therefore come from one
# definition. The copier keeps only what is not repository content: the Workflows registry.
#
# A file that a test or the tick reads at run time must ship from HERE, in the change that starts
# reading it. CI's exec-mirror job fails that change's PR when it does not.
#
# Usage: scripts/build_exec_mirror.sh SRC [MIRROR]
#   SRC     a git checkout of this repository.
#   MIRROR  the tree to build or refresh, else $ORCH_MIRROR. There is no other default: this script
#           writes in place, so the caller names the tree. Whether the LIVE mirror may be written
#           in place is the caller's policy (the guarded publisher and the copier's entry guard),
#           never this script's.
#
# The modules and the named root files come from the WORKING TREE. tests/, scripts/, .github/,
# .gitignore, ruff.toml and docs/ come from `git archive HEAD`, so uncommitted edits to those do not
# travel; the output says so. Any failure exits non-zero and names its line. A half-built mirror
# that exits 0 is what the ERR trap exists to prevent.
#
# No here-documents or here-strings: bash 5.3 feeds them through a pipe, and a pipe can block
# forever once system pipe memory is exhausted (tests/test_orchestrate_no_heredoc.py).
set -euo pipefail
SRC="${1:-}"
MIRROR="${2:-${ORCH_MIRROR:-}}"
if [[ -z "$SRC" || -z "$MIRROR" ]]; then
  echo "usage: build_exec_mirror.sh SRC [MIRROR]  (MIRROR defaults to \$ORCH_MIRROR, and to nothing else)" >&2
  exit 2
fi
trap 'echo "ERR: build_exec_mirror.sh aborted at line $LINENO (rc $?): $MIRROR is HALF-BUILT -- fix the cause and re-run before trusting a verify there." >&2' ERR
if [[ ! -f "$SRC/orchestrate.sh" || ! -d "$SRC/src" ]]; then
  echo "ERR: $SRC has no orchestrate.sh and src/ — not a checkout of this repository, or unreadable." >&2
  exit 1
fi
if ! git -C "$SRC" rev-parse --verify -q HEAD >/dev/null 2>&1; then
  echo "ERR: $SRC is not a git checkout with a HEAD; tests/, scripts/, .github/ and docs/ ship from git." >&2
  exit 1
fi
head_short="$(git -C "$SRC" rev-parse --short HEAD)"

mkdir -p "$MIRROR"
find "$MIRROR" -maxdepth 1 \( -name '*.py' -o -name '*.sh' \) -delete 2>/dev/null || true
# THE MODULES SIT FLAT at the mirror root (2026-08-23). paths.py (module dir named `src` => the
# checkout is its parent, else the two coincide) and orchestrate.sh ($ORCH="$ORCH_REPO/src", else
# $ORCH_REPO) both DETECT the layout, so flat and src/ are equally correct, and flat keeps this
# delete-then-copy one line. env_prereq.exec_mirror_shape() recognises the mirror by this flatness.
MODSRC="$SRC/src"
cp "$MODSRC"/*.py "$SRC"/orchestrate.sh "$MIRROR"/

# THE WHOLE tests/ TREE SHIPS FROM GIT HEAD, and nothing under it is named here (2026-10-02), so the
# next fixture travels without anyone adding a block for its directory. tests/conftest.py fails any
# test that reads a file under tests/ this archive would leave out (untracked, .gitignore'd or
# export-ignore), in the checkout and before merge.
# FROM GIT, NOT THE WORKING TREE (2026-09-14): Dropbox keeps checkout files as online-only
# placeholders, and any content read of one blocks until Dropbox fetches it; two syncs hung for
# hours that way. `.git` is dropbox-ignored, so `git archive` reads only local objects.
# `&& cat >/dev/null` IS LOAD-BEARING (2026-10-01) -- never "simplify" it back to `| tar -x`. bsdtar
# stops reading at the end-of-archive marker, before git writes the record padding after it. If tar
# exits first, git dies of SIGPIPE, pipefail makes the pipeline 141, and `set -e` aborts the build
# half-done. It is a scheduler race (12 of 12 aborted at load ~100-128). Draining keeps the read end
# open until git exits; `&&`, not `;`, so a failed extraction still fails the pipeline.
rm -rf "$MIRROR/tests"
if git -C "$SRC" rev-parse --verify -q HEAD:tests >/dev/null 2>&1; then
  git -C "$SRC" archive --format=tar HEAD tests | { tar -x -C "$MIRROR" && cat >/dev/null; }
  echo "tests/ shipped from git HEAD $head_short ($(find "$MIRROR/tests" -type f | wc -l | tr -d ' ') files) — uncommitted test edits are NOT shipped"
fi
chmod +x "$MIRROR"/orchestrate.sh
extra=0
mkdir -p "$MIRROR/experiments" "$MIRROR/data" "$MIRROR/config"
# Source snapshots, not mirror-local state. Remove the previous copies before the conditional copy
# so a file deleted from the repository cannot keep influencing the mirror, and leave every other
# mirror-local marker in these directories (experiments/.last-ship-gate, say) alone.
rm -f \
  "$MIRROR/experiments/hypotheses.json" \
  "$MIRROR/experiments/features.json" \
  "$MIRROR/experiments/repo_knowledge.json" \
  "$MIRROR/data/feedback-snapshot.json" \
  "$MIRROR/config/coverage-baseline.json"
for snapshot in \
  experiments/hypotheses.json \
  experiments/features.json \
  experiments/repo_knowledge.json \
  data/feedback-snapshot.json \
  config/coverage-baseline.json; do
  if [[ -f "$SRC/$snapshot" ]]; then
    cp "$SRC/$snapshot" "$MIRROR/$snapshot"
    extra=$((extra + 1))
  fi
done
# verify.py's collection floor travels with the code. Without it a mirror run reports "floor unset",
# and a silent drop in tests collected reads exactly like tests passing. (2026-08-21)
if [[ -f "$SRC/.verify-floor.json" ]]; then
  cp "$SRC/.verify-floor.json" "$MIRROR/.verify-floor.json"
  extra=$((extra + 1))
fi
# THE WHOLE scripts/ TREE, from git, at its repository-relative path (2026-09-14). The lanes execute
# scripts/check_checks_reported.py directly under launchd, which cannot read CloudStorage paths;
# rail contracts run scripts/docs_drift_fix_agent.py from the tree they execute in; and the sync's
# own verifier and installer ship here. `&& cat >/dev/null`: the race described at tests/ above.
if git -C "$SRC" rev-parse --verify -q HEAD:scripts >/dev/null 2>&1; then
  mkdir -p "$MIRROR/scripts"
  find "$MIRROR/scripts" -mindepth 1 -delete 2>/dev/null || true
  git -C "$SRC" archive --format=tar HEAD scripts | { tar -x -C "$MIRROR" && cat >/dev/null; }
  chmod +x "$MIRROR/scripts/check_checks_reported.py"
  echo "scripts/ shipped from git HEAD ($(find "$MIRROR/scripts" -mindepth 1 -maxdepth 1 | wc -l | tr -d ' ') entries)"
  extra=$((extra + 1))
fi
# Campaign contracts are read by the campaign CLI and its acceptance tests in both shapes.
# They are repository inputs, never the capability-program receipts in ORCH_STATE_DIR.
rm -rf "$MIRROR/campaigns"
if git -C "$SRC" rev-parse --verify -q HEAD:campaigns >/dev/null 2>&1; then
  git -C "$SRC" archive --format=tar HEAD campaigns | { tar -x -C "$MIRROR" && cat >/dev/null; }
  extra=$((extra + 1))
fi
# THE REPOSITORY CONFIGURATION AND docs/ TRAVEL TOO (2026-10-02), from git. PR #352's test opened
# .github/workflows/pr-00-gate.yml with no guard, so every mirror verify was red with 19
# FileNotFoundErrors while CI and every checkout were green; tests/test_ci_gate_config.py also needs
# ruff.toml and docs/. Nothing here RUNS: these are inert files that tests read. The tree still
# reads as the exec mirror because exec_mirror_shape() keys on what it still is: flat, and not a git
# repository.
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
  echo ".github/ shipped from git HEAD ($(find "$MIRROR/.github" -type f | wc -l | tr -d ' ') files) with ${config_paths[*]:1}"
  extra=$((extra + 1))
fi
# docs/ is MERGED, never replaced: the live mirror's docs/ also holds output written there at run
# time (docs/reports/issue_completion_*, untracked here), and a delete-then-extract would destroy it.
# The paths the previous build shipped are recorded in .docs-shipped.txt, and only those are removed
# before the new tree lands, so a doc deleted from the repository does not linger either. An entry
# outside docs/, or one carrying `..`, is never removed. The cleanup runs even when HEAD has no
# docs/ at all, and the manifest then goes too: a manifest left behind would keep naming the old
# docs, and the installer deploys whatever it names (CodeRabbit on #438).
if [[ -f "$MIRROR/.docs-shipped.txt" ]]; then
  while IFS= read -r shipped; do
    case "$shipped" in
      docs/*) [[ "$shipped" == *..* ]] || rm -f "$MIRROR/$shipped" ;;
    esac
  done < "$MIRROR/.docs-shipped.txt"
fi
if git -C "$SRC" rev-parse --verify -q HEAD:docs >/dev/null 2>&1; then
  git -C "$SRC" -c core.quotePath=false ls-tree -r --name-only HEAD docs > "$MIRROR/.docs-shipped.txt"
  git -C "$SRC" archive --format=tar HEAD docs | { tar -x -C "$MIRROR" && cat >/dev/null; }
  echo "docs/ shipped from git HEAD ($(wc -l < "$MIRROR/.docs-shipped.txt" | tr -d ' ') files, merged beside run-time output)"
  extra=$((extra + 1))
else
  rm -f "$MIRROR/.docs-shipped.txt"
fi
# pyproject.toml carries the coverage, mypy and pytest configuration (`pythonpath = ["src", "."]`
# is what resolves the flat modules here). It replaced .coveragerc on 2026-08-23, and a stale
# .coveragerc beside it is exactly what test_coverage_config_enables_parallel_mode refuses, so the
# file it replaced is removed.
if [[ -f "$SRC/pyproject.toml" ]]; then
  cp "$SRC/pyproject.toml" "$MIRROR/pyproject.toml"
  rm -f "$MIRROR/.coveragerc"
  extra=$((extra + 1))
fi
# Root documents that tests read from beside the modules: test_improvement_log.py reads CLAUDE.md and
# IMPROVEMENT_BACKLOG.md, and the terminal merge-contract test reads AGENTS.md and ORCHESTRATOR.md.
for root_doc in CLAUDE.md AGENTS.md ORCHESTRATOR.md IMPROVEMENT_BACKLOG.md; do
  if [[ -f "$SRC/$root_doc" ]]; then
    cp "$SRC/$root_doc" "$MIRROR/$root_doc"
    extra=$((extra + 1))
  fi
done

# Counted with find, not `ls glob | wc`: under pipefail a glob that matches nothing makes `ls` fail
# and `set -e` abort here, so the two refusals below could never print.
built_py="$(find "$MIRROR" -maxdepth 1 -type f -name '*.py' | wc -l | tr -d ' ')"
built_tests="$(find "$MIRROR/tests" -maxdepth 1 -type f -name '*.py' 2>/dev/null | wc -l | tr -d ' ')"
echo "built $built_py .py (from $MODSRC) + orchestrate.sh + $built_tests test file(s) + $extra data/config entries -> $MIRROR"
if [[ "$built_py" -eq 0 ]]; then
  echo "ERR: 0 modules reached $MIRROR from $MODSRC — the mirror has no code and a tick would do nothing." >&2
  exit 1
fi
if [[ "$built_tests" -eq 0 ]]; then
  echo "ERR: 0 test files reached $MIRROR/tests — a verify there would collect nothing." >&2
  exit 1
fi
