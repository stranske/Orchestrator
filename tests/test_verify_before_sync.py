"""`scripts/verify_before_sync.sh` gives this machine's verdict on a tree before the live mirror runs it.

DECIDED 2026-10-02 (the owner). `verify.py` checks the whole tree it runs in, never one session's
work, so only the latest run on a tree counts. CI runs the same suite on every PR head and on main,
so the one local run a sync needs is the one CI cannot do: the live ledger, the installed CLIs and
the flat mirror shape. The sync wrapper took that run AFTER copying into the live mirror, so a red
arrived after launchd could already run the code. A sync landing mid-run would also have left the
run reading a mix of old and new files.

The script builds a throwaway mirror with the REAL copy script, isolated as
docs/MIRROR_SYNC_PATCH.md prescribes, verifies it with the real home, and re-checks that the
source did not move meanwhile. These tests stand in for both the copy script and verify.py, so
nothing here runs the real sync, writes the live mirror, or takes minutes. Each test pins one
promise the script makes to the caller that acts on its exit code.

verify.py's `ledger validate` gate is a WRITING load of the ledger it is pointed at. So the script
hands verify.py a scratch COPY of the live state (the ledger, the stamps, the small directories,
and the Brain through SQLite's backup API), and the stand-in verify.py below writes to what it is
handed, exactly as that gate would.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

import paths

SCRIPT = paths.REPO_ROOT / "scripts" / "verify_before_sync.sh"
INSTALLER = paths.REPO_ROOT / "scripts" / "install_verified_snapshot.py"

# Stands in for orch-sync-mirror.sh. It records the environment it was given, then writes a
# mirror whose verify.py records ITS environment and prints the summary lines verify.py prints.
FAKE_SYNC = r"""#!/usr/bin/env bash
set -euo pipefail
SRC="$1"
MIRROR="$ORCH_MIRROR"
source "$FAKE_COPY_GUARD"
printf 'sync_home=%s\nsync_mirror=%s\nsync_gh=%s\nsync_src=%s\n' \
  "$HOME" "$ORCH_MIRROR" "${GH_CONFIG_DIR:-}" "$1" >> "$FAKE_RECORD"
[[ "${FAKE_SYNC_RC:-0}" == "0" ]] || exit "$FAKE_SYNC_RC"
mkdir -p "$ORCH_MIRROR"
MODSRC="$1/src"
[[ -d "$MODSRC" ]] || MODSRC="$1"
cp "$MODSRC"/*.py "$ORCH_MIRROR"/
cp "$FAKE_VERIFY" "$ORCH_MIRROR/verify.py"
cp "$1/orchestrate.sh" "$ORCH_MIRROR/orchestrate.sh"
mkdir -p "$ORCH_MIRROR/scripts"
cp "$FAKE_INSTALLER" "$ORCH_MIRROR/scripts/install_verified_snapshot.py"
chmod +x "$ORCH_MIRROR/scripts/install_verified_snapshot.py"
"""

FAKE_VERIFY = r"""import os, pathlib, sqlite3, sys
env = os.environ
# Resolved exactly as capabilities.py and feedback.py resolve them: the variable, else under HOME.
default = pathlib.Path(env["HOME"]) / ".codex" / "orchestrator"
runtime = pathlib.Path(env.get("ORCH_LOCAL_RUNTIME", default))
statedir = env.get("ORCH_STATE_DIR", str(default))
ledger = pathlib.Path(env.get("ORCH_CAPABILITIES_PATH", runtime / "capabilities.json"))
brain_path = env.get("ORCH_FEEDBACK_DB", str(runtime / "feedback" / "orchestrator.db"))
brain = sqlite3.connect(brain_path)
seen = {
    "verify_cwd": os.getcwd(),
    "verify_home": env["HOME"],
    "verify_runtime": str(runtime),
    "verify_statedir": statedir,
    "verify_ledger": str(ledger),
    "verify_brain": brain_path,
    "saw_ledger": ledger.read_text().strip(),
    "saw_stamp": str((runtime / ".last-periodic-report").is_file()),
    "saw_docs": str((runtime / "local-docs" / "a.md").is_file()),
    "saw_big": str((runtime / "agent-runtime").exists()),
    "saw_rows": str(brain.execute("select count(*) from t").fetchone()[0]),
}
with open(env["FAKE_RECORD"], "a") as fh:
    fh.writelines(f"{k}={v}\n" for k, v in seen.items())
# What the `ledger validate` gate does: a writing load. It must land in the copy.
ledger.write_text('{"written": "by verify.py"}')
brain.execute("insert into t values (2)")
brain.commit()
brain.close()
touch = os.environ.get("FAKE_TOUCH_SRC")
if touch:  # another session moving the clone while verify.py runs
    pathlib.Path(touch).write_text("moved under the run\n")
touch_mirror = os.environ.get("FAKE_TOUCH_MIRROR")
if touch_mirror:
    (pathlib.Path.cwd() / touch_mirror).write_text("changed by verification\n")
expected = os.environ.get("FAKE_EXPECT_MIRROR_FILE")
if expected:
    with open(env["FAKE_RECORD"], "a") as fh:
        fh.write(f"copied_expected={pathlib.Path(expected).is_file()}\n")
print("  tree:       " + os.environ.get("FAKE_TREE", "EXEC MIRROR — mirror_* ceilings apply"))
rc = int(os.environ.get("FAKE_VERIFY_RC", "0"))
print("  VERIFIED — fake" if rc == 0 else "  FAILED — fake")
sys.exit(rc)
"""


def _git(src: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(src), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture()
def world(tmp_path: Path) -> dict:
    home = tmp_path / "home"  # stands in for the real home the caller runs from
    state = home / ".codex" / "orchestrator"  # and its live state
    (state / "local-docs").mkdir(parents=True)
    (state / "local-docs" / "a.md").write_text("policy\n")
    (state / "agent-runtime").mkdir()
    (state / "agent-runtime" / "big.bin").write_bytes(b"x" * (2 * 1024 * 1024))  # over 1 MB
    (state / ".last-periodic-report").write_text("")
    (state / "capabilities.json").write_text('{"capabilities": {"x": {}}}')
    (state / "feedback").mkdir()
    brain = sqlite3.connect(state / "feedback" / "orchestrator.db")
    brain.execute("create table t (v integer)")
    brain.execute("insert into t values (1)")
    brain.commit()
    brain.close()
    src = tmp_path / "src"
    src.mkdir()
    (src / "orchestrate.sh").write_text("echo tick\n")
    (src / "base.py").write_text("BASE = True\n")
    _git(src, "init", "-q")
    _git(src, "add", "-A")
    _git(src, "commit", "-q", "-m", "init")
    (tmp_path / "fake-sync.sh").write_text(FAKE_SYNC)
    (tmp_path / "fake-verify.py").write_text(FAKE_VERIFY)
    tmpdir = tmp_path / "tmpdir"
    tmpdir.mkdir()
    return {
        "tmp": tmp_path,
        "home": home,
        "src": src,
        "tmpdir": tmpdir,
        "record": tmp_path / "record.txt",
        "state": state,
    }


def _run(world: dict, *args: str, **env: str) -> tuple[subprocess.CompletedProcess, dict]:
    base = {
        "PATH": os.environ["PATH"],
        "HOME": str(world["home"]),
        "TMPDIR": str(world["tmpdir"]),
        "ORCH_SYNC_SCRIPT": str(world["tmp"] / "fake-sync.sh"),
        "FAKE_VERIFY": str(world["tmp"] / "fake-verify.py"),
        "FAKE_COPY_GUARD": str(paths.REPO_ROOT / "scripts/incumbent_copy_guard.sh"),
        "FAKE_INSTALLER": str(INSTALLER),
        "FAKE_RECORD": str(world["record"]),
        "VERIFY_BEFORE_SYNC_MAX_DIR_MB": "1",
    }
    result = subprocess.run(
        ["bash", str(SCRIPT), *(args or (str(world["src"]),))],
        env={**base, **env},
        capture_output=True,
        text=True,
        timeout=120,
    )
    record: dict[str, str] = {}
    if world["record"].exists():
        for line in world["record"].read_text().splitlines():
            key, _, value = line.partition("=")
            record[key] = value
    return result, record


def test_a_green_scratch_mirror_is_verified_and_nothing_live_is_touched(world):
    result, record = _run(world)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "== verify-before-sync: VERIFIED for " in result.stdout, result.stdout
    # The copy ran with a scratch HOME and a scratch mirror, both under TMPDIR, and never the
    # caller's home, so neither the live mirror nor the live registry copy could be written.
    scratch_home, scratch_mirror = Path(record["sync_home"]), Path(record["sync_mirror"])
    assert scratch_home != world["home"], record
    assert world["tmpdir"] in scratch_home.parents, record
    assert world["tmpdir"] in scratch_mirror.parents, record
    assert record["sync_src"] == str(world["src"]), record
    # gh keeps the caller's auth, resolved against the REAL home before HOME was replaced.
    assert record["sync_gh"] == str(world["home"] / ".config" / "gh"), record
    # verify.py ran inside the scratch mirror with the REAL home, so the gates read the live ledger.
    assert Path(record["verify_cwd"]).resolve() == scratch_mirror.resolve(), record
    assert record["verify_home"] == str(world["home"]), record
    # And the throwaway directory is gone afterwards.
    assert list(world["tmpdir"].iterdir()) == [], list(world["tmpdir"].iterdir())


def test_verify_py_reads_and_writes_only_a_copy_of_the_live_state(world):
    """The `ledger validate` gate rewrites the ledger it loads. Taken before the copy, on the live
    paths, it would write the declarations of a tree that is not deployed into the live ledger,
    even when the verdict then stops the copy. So verify.py is handed a copy, and its writes land
    there: the live ledger and the live Brain are byte- and row-identical afterwards."""
    ledger = world["state"] / "capabilities.json"
    before = ledger.read_bytes()
    result, record = _run(world)
    assert result.returncode == 0, result.stdout + result.stderr
    assert ledger.read_bytes() == before, ledger.read_text()
    brain = sqlite3.connect(world["state"] / "feedback" / "orchestrator.db")
    assert brain.execute("select count(*) from t").fetchone()[0] == 1
    brain.close()
    # What it read was the live content, through the copy.
    assert record["saw_ledger"] == before.decode().strip(), record
    assert (record["saw_rows"], record["saw_stamp"], record["saw_docs"]) == ("1", "True", "True")
    for key in ("verify_runtime", "verify_statedir", "verify_ledger", "verify_brain"):
        assert world["tmpdir"] in Path(record[key]).parents, (key, record)
    # A directory over the size bound is skipped and NAMED, never silently absent.
    assert record["saw_big"] == "False", record
    assert "not copied, over 1 MB" in result.stdout and "agent-runtime (2 MB)" in result.stdout


def test_a_symlinked_tmpdir_hands_verify_py_canonical_paths(world):
    """macOS's TMPDIR is /var/folders/..., a symlink to /private/var/..., and mktemp keeps its
    trailing slash as `//`. Handed that spelling, the dispatcher selftest compared a resolved
    worktree path with an unresolved one and failed on every input (2026-10-02, twice), while the
    live state is no symlink. So every path the copy and verify.py see must be canonical."""
    real = world["tmp"] / "real-tmp"
    real.mkdir()
    link = world["tmp"] / "linked-tmp"
    link.symlink_to(real, target_is_directory=True)
    result, record = _run(world, TMPDIR=f"{link}/")
    assert result.returncode == 0, result.stdout + result.stderr
    for key in ("sync_mirror", "sync_home", "verify_cwd", "verify_runtime", "verify_ledger"):
        path = record[key]
        assert "//" not in path and path == os.path.realpath(path), (key, path)
        assert real.resolve() in Path(path).parents, (key, path)


def test_a_red_verdict_exits_one_and_prints_verify_pys_own_code(world):
    result, _ = _run(world, FAKE_VERIFY_RC="7")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "NOT VERIFIED (verify.py exit 7)" in result.stdout, result.stdout


def test_a_green_verdict_can_publish_the_exact_verified_snapshot(world):
    snapshot = world["tmpdir"] / "verified payload"
    digest_receipt = world["tmpdir"] / "verified-payload.sha256"
    result, _ = _run(
        world,
        "--snapshot-out",
        str(snapshot),
        "--digest-out",
        str(digest_receipt),
        str(world["src"]),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"verified deployment snapshot: {snapshot}" in result.stdout
    verified = (snapshot / "base.py").read_bytes()

    # A later source edit cannot alter the installed bytes: the installer only reads the retained
    # scratch payload, never SRC.
    (world["src"] / "base.py").write_text("MOVED = True\n")
    mirror = world["tmp"] / "live mirror"
    installed = subprocess.run(
        [
            "python3",
            str(snapshot / "scripts" / INSTALLER.name),
            str(snapshot),
            str(mirror),
            "--expected-digest",
            digest_receipt.read_text().strip(),
        ],
        capture_output=True,
        text=True,
    )
    assert installed.returncode == 0, installed.stdout + installed.stderr
    assert (mirror / "base.py").read_bytes() == verified
    assert len(digest_receipt.read_text().strip()) == 64


def test_a_red_verdict_never_publishes_a_snapshot(world):
    snapshot = world["tmpdir"] / "must-not-exist"
    digest_receipt = world["tmpdir"] / "must-not-exist.sha256"
    result, _ = _run(
        world,
        "--snapshot-out",
        str(snapshot),
        "--digest-out",
        str(digest_receipt),
        str(world["src"]),
        FAKE_VERIFY_RC="7",
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert not snapshot.exists()
    assert not digest_receipt.exists()


def test_digest_receipt_requires_snapshot_publication(world):
    digest_receipt = world["tmpdir"] / "not-allowed.sha256"
    result, record = _run(
        world,
        "--digest-out",
        str(digest_receipt),
        str(world["src"]),
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "--digest-out requires --snapshot-out" in result.stderr
    assert not digest_receipt.exists()
    assert record == {}


def test_existing_snapshot_destination_creates_no_scratch_directory(world):
    snapshot = world["tmpdir"] / "already-there"
    snapshot.mkdir()
    result, record = _run(world, "--snapshot-out", str(snapshot), str(world["src"]))
    assert result.returncode == 2, result.stdout + result.stderr
    assert "snapshot output already exists" in result.stderr
    assert list(world["tmpdir"].iterdir()) == [snapshot]
    assert record == {}


def test_a_failed_copy_verifies_nothing(world):
    result, record = _run(world, FAKE_SYNC_RC="5")
    assert result.returncode == 2, result.stdout + result.stderr
    assert "NOTHING VERIFIED: the scratch copy failed" in result.stderr, result.stderr
    assert "verify_cwd" not in record, record
    assert "VERIFIED" not in result.stdout.replace("NOTHING VERIFIED", ""), result.stdout


def test_a_source_that_is_not_a_checkout_verifies_nothing(world):
    loose = world["tmp"] / "not-a-checkout"
    loose.mkdir()
    result, record = _run(world, str(loose))
    assert result.returncode == 2, result.stdout + result.stderr
    assert "is not a git checkout" in result.stderr, result.stderr
    assert record == {}, record


def test_a_source_that_moves_during_the_run_voids_the_verdict(world):
    """The clone is shared: another session can switch it while verify.py runs. A green verdict
    about the tree as it WAS must not let the caller copy the tree as it IS."""
    result, _ = _run(world, FAKE_TOUCH_SRC=str(world["src"] / "orchestrate.sh"))
    assert result.returncode == 3, result.stdout + result.stderr
    assert "VOID:" in result.stderr, result.stderr
    assert "== verify-before-sync: VERIFIED" not in result.stdout, result.stdout


def test_an_untracked_file_appearing_also_voids_the_verdict(world):
    """Modules are copied from the working tree, so an untracked one would ship as well."""
    result, _ = _run(world, FAKE_TOUCH_SRC=str(world["src"] / "new_module.py"))
    assert result.returncode == 3, result.stdout + result.stderr


def test_an_ignored_module_copied_to_the_mirror_is_part_of_the_identity(world):
    """Git ignore rules cannot hide bytes selected by the copier's working-tree glob."""
    src = world["src"]
    module_dir = src / "src"
    module_dir.mkdir()
    (module_dir / "base.py").write_text("BASE = True\n")
    ignored = module_dir / "ignored.py"
    ignored.write_text("VALUE = 'before'\n")
    (src / ".gitignore").write_text("src/ignored.py\n")
    _git(src, "add", ".gitignore", "src/base.py")
    _git(src, "commit", "-q", "-m", "src layout")
    assert (
        subprocess.run(["git", "-C", str(src), "check-ignore", "-q", "src/ignored.py"]).returncode
        == 0
    )

    result, record = _run(
        world,
        FAKE_TOUCH_SRC=str(ignored),
        FAKE_EXPECT_MIRROR_FILE=str(Path("ignored.py")),
    )
    assert record["copied_expected"] == "True", record
    assert result.returncode == 3, result.stdout + result.stderr
    assert "VOID:" in result.stderr, result.stderr


def test_a_tree_not_judged_as_the_mirror_shape_is_not_verified(world):
    """A pass under the checkout's ceilings is not the mirror's verdict, so it must not let the
    caller copy: the shape mismatch is a NOT VERIFIED (exit 1), never a warning beside a 0."""
    result, _ = _run(world, FAKE_TREE="checkout")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "did not judge the scratch tree as the exec-mirror shape" in result.stdout
    assert "NOT VERIFIED (verify.py passed, but not in the exec-mirror shape)" in result.stdout
    assert "verified source identity:" not in result.stdout, result.stdout


def test_identity_mode_prints_the_fingerprint_a_green_verdict_was_taken_on(world):
    """The wrapper copies AFTER the verdict, so it re-checks the source with `--identity`: the
    fingerprint must equal the one a VERIFIED run printed, and move when a copied input moves."""
    result, _ = _run(world)
    assert result.returncode == 0, result.stdout + result.stderr
    (line,) = [ln for ln in result.stdout.splitlines() if "verified source identity: " in ln]
    verified = line.split("verified source identity: ", 1)[1].strip()
    same, _ = _run(world, "--identity", str(world["src"]))
    assert same.returncode == 0 and same.stdout.strip() == verified, (same.stdout, verified)
    (world["src"] / "base.py").write_text("BASE = False\n")
    moved, _ = _run(world, "--identity", str(world["src"]))
    assert moved.returncode == 0 and moved.stdout.strip() != verified, moved.stdout


def test_a_verify_run_that_changes_deployment_bytes_voids_the_snapshot(world):
    snapshot = world["tmpdir"] / "must-not-exist"
    result, _ = _run(
        world,
        "--snapshot-out",
        str(snapshot),
        str(world["src"]),
        FAKE_TOUCH_MIRROR="base.py",
    )
    assert result.returncode == 3, result.stdout + result.stderr
    assert "verification changed deployment-owned bytes" in result.stderr
    assert not snapshot.exists()


def test_incomplete_verify_evidence_never_publishes_a_snapshot(world):
    fake_bin = world["tmp"] / "fake-bin"
    fake_bin.mkdir()
    tee = fake_bin / "tee"
    tee.write_text("#!/bin/sh\nexit 9\n")
    tee.chmod(0o755)
    snapshot = world["tmpdir"] / "must-not-exist"
    result, _ = _run(
        world,
        "--snapshot-out",
        str(snapshot),
        str(world["src"]),
        PATH=f"{fake_bin}:{os.environ['PATH']}",
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "capture complete verify.py evidence" in result.stderr
    assert not snapshot.exists()


def test_keep_leaves_the_scratch_mirror_for_inspection(world):
    result, record = _run(world, VERIFY_BEFORE_SYNC_KEEP="1")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "scratch directory kept: " in result.stdout, result.stdout
    assert (Path(record["sync_mirror"]) / "verify.py").is_file(), record


def test_the_script_feeds_no_here_document_to_anything():
    """The same structural rule orchestrate.sh follows, for the same bash 5.3 pipe deadlock."""
    assert ("<" + "<") not in SCRIPT.read_text(encoding="utf-8")
