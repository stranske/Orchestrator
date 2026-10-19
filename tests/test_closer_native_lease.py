"""Preview native lane lease protocol and renderer (no live ~/.codex mutation)."""

from __future__ import annotations

import concurrent.futures
import importlib.util
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODULE_DIR = ROOT / "src" if (ROOT / "src").is_dir() else ROOT
LEASE_PATH = MODULE_DIR / "closer_native_lease.py"
RENDER_PATH = ROOT / "scripts/render_closer_native_hooks.py"
CANONICAL_PRERUN = Path.home() / ".codex/bin/handoff-prerun.sh"
CANONICAL_POSTRUN = Path.home() / ".codex/bin/handoff-postrun.sh"


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


lease = _load_module(LEASE_PATH, "closer_native_lease")
renderer = _load_module(RENDER_PATH, "render_closer_native_hooks")


def _minimal_prerun() -> str:
    return textwrap.dedent("""\
        #!/bin/bash
        set -euo pipefail
        lane="${1:-closer}"
        agent="${HANDOFF_AGENT:-codex}"
        HANDOFF_BIN="${HANDOFF_BIN:-$HOME/.codex/bin/handoff.sh}"
        [[ -x "$HANDOFF_BIN" ]] || { echo "missing handoff" >&2; exit 3; }
        # --- Per-lane round mutex --------------------------------------------------------------
        LANE_MUTEX_DIR="${HANDOFF_LANE_MUTEX_DIR:-$HOME/.codex/handoff/.lane-$lane.lock}"
        _release_lane_mutex() { rm -rf "$LANE_MUTEX_DIR" 2>/dev/null || true; }
        mkdir "$LANE_MUTEX_DIR" 2>/dev/null || exit 1
        # Capture both stdout and stderr; route by exit code below.
        set +e
        stop_out="$("$HANDOFF_BIN" check-stop 2>&1)"
        stop_rc=$?
        set -e
        case "$stop_rc" in
          1) _release_lane_mutex; exit 1 ;;
        esac
        """)


def _minimal_postrun() -> str:
    return textwrap.dedent("""\
        #!/bin/bash
        set -euo pipefail
        lane="${1:-closer}"
        agent="${HANDOFF_AGENT:-codex}"
        LANE_MUTEX_DIR="${HANDOFF_LANE_MUTEX_DIR:-$HOME/.codex/handoff/.lane-$lane.lock}"
        launch_keepalive_label_repair() { :; }
        trap 'rm -rf "$LANE_MUTEX_DIR" 2>/dev/null || true; launch_keepalive_label_repair' EXIT
        echo postrun-body
        """)


def _stub_handoff(home: Path) -> None:
    bin_dir = home / ".codex/bin"
    bin_dir.mkdir(parents=True)
    handoff = bin_dir / "handoff.sh"
    handoff.write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
    handoff.chmod(0o755)


@pytest.fixture
def handoff_home(tmp_path):
    home = tmp_path / "home"
    handoff_root = home / ".codex" / "handoff"
    handoff_root.mkdir(parents=True)
    _stub_handoff(home)
    return home, handoff_root


def test_acquire_refuses_existing_directory(handoff_home):
    home, root = handoff_home
    lane_dir = root / ".lane-closer.lock"
    lane_dir.mkdir()
    with pytest.raises(lease.LeaseRefused):
        lease.acquire(root, "closer", "codex", 42, lane_dir_override=str(lane_dir))


def test_stale_expired_directory_never_stolen(handoff_home):
    home, root = handoff_home
    lane_dir = root / ".lane-closer.lock"
    lane_dir.mkdir()
    (lane_dir / "meta").write_text("1 1 closer codex HANDOFF_EXACT_TOKEN_V1:dead:beef:0\n")
    with pytest.raises(lease.LeaseRefused):
        lease.acquire(root, "closer", "codex", 99, lane_dir_override=str(lane_dir))


def test_release_exact_token_once(handoff_home):
    home, root = handoff_home
    lane_dir = root / ".lane-closer.lock"
    token = lease.acquire(root, "closer", "codex", 50, lane_dir_override=str(lane_dir))
    assert lease.release(root, "closer", token, lane_dir_override=str(lane_dir))
    assert not lane_dir.exists()
    assert lease.release(root, "closer", token, lane_dir_override=str(lane_dir))


def test_wrong_token_leaves_successor(handoff_home):
    home, root = handoff_home
    lane_dir = root / ".lane-closer.lock"
    token_a = lease.acquire(root, "closer", "codex", 1, lane_dir_override=str(lane_dir))
    assert not lease.release(root, "closer", token_a + "x", lane_dir_override=str(lane_dir))
    assert lane_dir.exists()
    # successor cannot acquire while directory remains
    with pytest.raises(lease.LeaseRefused):
        lease.acquire(root, "closer", "cursor", 2, lane_dir_override=str(lane_dir))


def test_legacy_four_field_meta_refuses_release(handoff_home):
    home, root = handoff_home
    lane_dir = root / ".lane-closer.lock"
    lane_dir.mkdir()
    (lane_dir / "meta").write_text("1 1700000000 closer codex\n", encoding="utf-8")
    token = f"{lease.PROTOCOL}:fake:deadbeef:0"
    assert not lease.release(root, "closer", token, lane_dir_override=str(lane_dir))
    assert lane_dir.exists()


def test_concurrent_acquire_single_winner(handoff_home):
    home, root = handoff_home
    lane_dir = root / ".lane-opener.lock"

    def attempt():
        try:
            return lease.acquire(
                root, "opener", "codex", os.getpid(), lane_dir_override=str(lane_dir)
            )
        except lease.LeaseRefused:
            return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: attempt(), range(16)))
    winners = [r for r in results if r]
    assert len(winners) == 1
    assert lane_dir.is_dir()


def test_token_newline_injection_not_released(handoff_home):
    home, root = handoff_home
    lane_dir = root / ".lane-closer.lock"
    token = lease.acquire(root, "closer", "codex", 7, lane_dir_override=str(lane_dir))
    injected = token + "\nHANDOFF_EXACT_TOKEN_V1:evil"
    assert not lease.release(root, "closer", injected, lane_dir_override=str(lane_dir))


def test_crash_fence_retained(handoff_home):
    home, root = handoff_home
    lane_dir = root / ".lane-closer.lock"
    lane_dir.mkdir()
    (lane_dir / ".lease-writing").write_text("1")
    assert (lane_dir / ".lease-writing").exists()
    with pytest.raises(lease.LeaseRefused):
        lease.acquire(root, "closer", "codex", 3, lane_dir_override=str(lane_dir))


def test_renderer_idempotent_hashes(tmp_path):
    src_p = tmp_path / "prerun.sh"
    src_o = tmp_path / "postrun.sh"
    src_p.write_text(_minimal_prerun())
    src_o.write_text(_minimal_postrun())
    out1 = tmp_path / "out1"
    out2 = tmp_path / "out2"
    renderer.render(src_p, src_o, out1)
    renderer.render(src_p, src_o, out2)
    h1 = (out1 / "handoff-prerun.sh").read_bytes()
    h2 = (out2 / "handoff-prerun.sh").read_bytes()
    assert h1 == h2
    assert b'rm -rf "$LANE_MUTEX_DIR"' not in h1


def test_renderer_refuses_bad_anchor(tmp_path):
    bad = tmp_path / "bad.sh"
    bad.write_text("#!/bin/bash\necho no mutex\n")
    good = tmp_path / "postrun.sh"
    good.write_text(_minimal_postrun())
    with pytest.raises(ValueError, match="mutex"):
        renderer.render(bad, good, tmp_path / "out")


def test_generated_prerun_subprocess_acquire_and_halt_release(handoff_home, tmp_path):
    home, root = handoff_home
    src_p = tmp_path / "prerun.sh"
    src_o = tmp_path / "postrun.sh"
    src_p.write_text(_minimal_prerun())
    src_o.write_text(_minimal_postrun())
    out = tmp_path / "rendered"
    renderer.render(src_p, src_o, out)
    prerun = out / "handoff-prerun.sh"
    env = {
        **os.environ,
        "HOME": str(home),
        "HANDOFF_AGENT": "codex",
        "HANDOFF_BIN": str(home / ".codex/bin/handoff.sh"),
        "HANDOFF_HANDOFF_ROOT": str(root),
        "CLOSER_NATIVE_LEASE_PY": str(LEASE_PATH),
        "CLOSER_NATIVE_LEASE_INTERPRETER": sys.executable,
        "HANDOFF_REFUSE_EXISTING_LANE": "1",
    }
    handoff = home / ".codex/bin/handoff.sh"
    handoff.write_text("#!/bin/bash\necho RELAY-HALTED: paused\nexit 1\n", encoding="utf-8")
    result = subprocess.run(
        ["/bin/bash", str(prerun), "closer"], env=env, capture_output=True, text=True
    )
    assert result.returncode == 1
    lane_dir = root / ".lane-closer.lock"
    assert not lane_dir.exists()


def test_generated_postrun_successor_survives_wrong_cleanup(handoff_home, tmp_path):
    home, root = handoff_home
    src_p = tmp_path / "prerun.sh"
    src_o = tmp_path / "postrun.sh"
    src_p.write_text(_minimal_prerun())
    src_o.write_text(_minimal_postrun())
    out = tmp_path / "rendered"
    renderer.render(src_p, src_o, out)
    postrun = out / "handoff-postrun.sh"
    lane_dir = root / ".lane-closer.lock"
    token_b = lease.acquire(root, "closer", "codex", 99, lane_dir_override=str(lane_dir))
    env = {
        **os.environ,
        "HOME": str(home),
        "HANDOFF_AGENT": "codex",
        "HANDOFF_HANDOFF_ROOT": str(root),
        "HANDOFF_LANE_MUTEX_DIR": str(lane_dir),
        "HANDOFF_EXPECTED_LANE_TOKEN": token_b + "wrong",
        "CLOSER_NATIVE_LEASE_PY": str(LEASE_PATH),
        "CLOSER_NATIVE_LEASE_INTERPRETER": sys.executable,
    }
    result = subprocess.run(
        ["/bin/bash", str(postrun), "closer", "no_op"], env=env, capture_output=True, text=True
    )
    assert result.returncode == 2 and "release refused rc=2" in result.stderr
    assert lane_dir.exists()
    assert lease.read_meta(lane_dir)["token"] == token_b


@pytest.mark.skipif(not CANONICAL_PRERUN.is_file(), reason="canonical prerun not on machine")
def test_canonical_sources_render_fail_closed(tmp_path):
    out = tmp_path / "preview"
    manifest = renderer.render(CANONICAL_PRERUN, CANONICAL_POSTRUN, out)
    body = (out / "handoff-prerun.sh").read_text(encoding="utf-8")
    assert renderer.MARKER_START in body
    assert "HANDOFF_REFUSE_EXISTING_LANE" in body
    assert manifest["protocol"] == "HANDOFF_EXACT_TOKEN_V1"


def test_cli_acquire_release_roundtrip(handoff_home):
    home, root = handoff_home
    lane_dir = root / ".lane-test.lock"
    env = {**os.environ, "HANDOFF_HANDOFF_ROOT": str(root)}
    proc = subprocess.run(
        [
            sys.executable,
            str(LEASE_PATH),
            "--handoff-root",
            str(root),
            "acquire",
            "--lane",
            "test",
            "--agent",
            "codex",
            "--owner-pid",
            "12",
            "--lane-dir",
            str(lane_dir),
        ],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    token = proc.stdout.strip()
    assert token.startswith(lease.PROTOCOL)
    meta_line = lease.read_meta_bytes(lane_dir).decode("utf-8")
    rel = subprocess.run(
        [
            sys.executable,
            str(LEASE_PATH),
            "--handoff-root",
            str(root),
            "release",
            "--lane",
            "test",
            "--expected-token",
            meta_line,
            "--lane-dir",
            str(lane_dir),
        ],
        env=env,
        capture_output=True,
        text=True,
    )
    assert rel.returncode == 0
    assert not lane_dir.exists()


def test_cli_handoff_root_after_subcommand_rejected(handoff_home):
    home, root = handoff_home
    proc = subprocess.run(
        [
            sys.executable,
            str(LEASE_PATH),
            "release",
            "--handoff-root",
            str(root),
            "--lane",
            "x",
            "--expected-token",
            f"{lease.PROTOCOL}:a:b:0",
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0
    assert "unrecognized arguments" in proc.stderr


def test_release_accepts_full_meta_credential(handoff_home):
    home, root = handoff_home
    lane_dir = root / ".lane-closer.lock"
    token = lease.acquire(root, "closer", "codex", 50, lane_dir_override=str(lane_dir))
    meta_line = lease.read_meta_bytes(lane_dir).decode("utf-8")
    assert lease.release(root, "closer", meta_line, lane_dir_override=str(lane_dir))
    assert not lane_dir.exists()
    assert token.startswith(lease.PROTOCOL)


def test_release_refuses_unknown_shape_without_deleting_meta(handoff_home):
    home, root = handoff_home
    lane_dir = root / ".lane-closer.lock"
    token = lease.acquire(root, "closer", "codex", 50, lane_dir_override=str(lane_dir))
    (lane_dir / "stray").write_text("x", encoding="utf-8")
    assert not lease.release(root, "closer", token, lane_dir_override=str(lane_dir))
    assert (lane_dir / "meta").is_file()
    assert lease.read_meta(lane_dir)["token"] == token


def test_meta_extra_line_refuses_release(handoff_home):
    home, root = handoff_home
    lane_dir = root / ".lane-closer.lock"
    lane_dir.mkdir()
    token = f"{lease.PROTOCOL}:fake:deadbeef:0"
    (lane_dir / "meta").write_text(
        f"1 1700000000 closer codex {token}\nsecond line\n",
        encoding="utf-8",
    )
    assert not lease.release(root, "closer", token, lane_dir_override=str(lane_dir))


def test_lane_dir_symlink_override_rejected(handoff_home):
    home, root = handoff_home
    target = root / "real.lock"
    target.mkdir()
    link = root / "link.lock"
    link.symlink_to(target)
    with pytest.raises(lease.LeaseShapeError):
        lease.acquire(root, "closer", "codex", 1, lane_dir_override=str(link))


def test_coord_lock_shared_for_same_canonical_lane_dir(handoff_home):
    home, root = handoff_home
    alt_root = home / "alias-root"
    alt_root.symlink_to(root)
    lane_dir = root / "custom.lock"
    lease.acquire(root, "closer", "codex", 1, lane_dir_override=str(lane_dir))
    with pytest.raises(lease.LeaseRefused):
        lease.acquire(
            alt_root.resolve(),
            "closer",
            "cursor",
            2,
            lane_dir_override=str(lane_dir),
        )
    token = lease.read_meta(lane_dir)["token"]
    assert lease.release(root, "closer", token, lane_dir_override=str(lane_dir))


def test_generated_prerun_acquire_and_release_mutex_only(handoff_home, tmp_path):
    home, root = handoff_home
    src_p = tmp_path / "prerun.sh"
    src_o = tmp_path / "postrun.sh"
    src_p.write_text(_minimal_prerun())
    src_o.write_text(_minimal_postrun())
    out = tmp_path / "rendered"
    renderer.render(src_p, src_o, out)
    prerun = out / "handoff-prerun.sh"
    env = {
        **os.environ,
        "HOME": str(home),
        "HANDOFF_AGENT": "codex",
        "HANDOFF_BIN": str(home / ".codex/bin/handoff.sh"),
        "HANDOFF_HANDOFF_ROOT": str(root),
        "CLOSER_NATIVE_LEASE_PY": str(LEASE_PATH),
        "CLOSER_NATIVE_LEASE_INTERPRETER": sys.executable,
        "HANDOFF_REFUSE_EXISTING_LANE": "1",
        "HANDOFF_PRERUN_MUTEX_ONLY": "1",
    }
    result = subprocess.run(
        ["/bin/bash", str(prerun), "closer"], env=env, capture_output=True, text=True
    )
    assert result.returncode == 0
    lane_dir = root / ".lane-closer.lock"
    assert not lane_dir.exists()


def test_generated_postrun_releases_with_full_meta_credential(handoff_home, tmp_path):
    home, root = handoff_home
    src_p = tmp_path / "prerun.sh"
    src_o = tmp_path / "postrun.sh"
    src_p.write_text(_minimal_prerun())
    src_o.write_text(_minimal_postrun())
    out = tmp_path / "rendered"
    renderer.render(src_p, src_o, out)
    postrun = out / "handoff-postrun.sh"
    lane_dir = root / ".lane-closer.lock"
    lease.acquire(root, "closer", "codex", 99, lane_dir_override=str(lane_dir))
    meta_line = lease.read_meta_bytes(lane_dir).decode("utf-8")
    env = {
        **os.environ,
        "HOME": str(home),
        "HANDOFF_AGENT": "codex",
        "HANDOFF_HANDOFF_ROOT": str(root),
        "HANDOFF_LANE_MUTEX_DIR": str(lane_dir),
        "HANDOFF_EXPECTED_LANE_TOKEN": meta_line,
        "CLOSER_NATIVE_LEASE_PY": str(LEASE_PATH),
        "CLOSER_NATIVE_LEASE_INTERPRETER": sys.executable,
    }
    subprocess.run(["/bin/bash", str(postrun), "closer", "no_op"], env=env, check=True)
    assert not lane_dir.exists()


def test_actual_pinned_runtime_adapter_invokes_rendered_token_helpers(
    handoff_home, tmp_path, monkeypatch
):
    import time

    import closer_runtime_binding as binding

    home, root = handoff_home
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HANDOFF_BIN", str(home / ".codex/bin/handoff.sh"))
    source_pre, source_post = tmp_path / "pre", tmp_path / "post"
    source_pre.write_text(_minimal_prerun())
    source_post.write_text(_minimal_postrun())
    source_pre.chmod(0o700)
    source_post.chmod(0o700)
    output = tmp_path / "rendered"
    renderer.render(source_pre, source_post, output)
    pins = {
        phase: {
            "path": str(output / f"handoff-{phase}.sh"),
            "sha256": binding._hash(output / f"handoff-{phase}.sh"),
        }
        for phase in ("prerun", "postrun")
    }
    pins["lease_module"] = {"path": str(LEASE_PATH), "sha256": binding._hash(LEASE_PATH)}
    cfg = {
        "native_hooks": pins,
        "handoff_agent": "codex",
        "handoff_dir": str(root),
        "dispatch_state": str(tmp_path / "state"),
    }
    adapter = binding.PinnedNativeHooks(cfg, runner=subprocess.run)
    code, token = adapter.prerun(time.time() + 15)
    assert code == 0 and token
    assert adapter.owns(token)
    assert token == (root / ".lane-closer.lock/meta").read_text()
    assert adapter.postrun(token, time.time() + 15) == 0
    assert not (root / ".lane-closer.lock").exists()


@pytest.mark.parametrize("repair_tail", [False, True])
def test_generated_postrun_preserves_matched_tail_and_cleanup_failure(
    handoff_home, tmp_path, repair_tail
):
    home, root = handoff_home
    lane = root / ".lane-closer.lock"
    lease.acquire(root, "closer", "codex", 99, lane_dir_override=str(lane))
    credential = lease.read_meta_bytes(lane).decode()
    original = _minimal_postrun()
    if not repair_tail:
        original = original.replace(
            renderer.POSTRUN_TRAP_ANCHORS[0], renderer.POSTRUN_TRAP_ANCHORS[1]
        )
        original = original.replace("launch_keepalive_label_repair() { :; }", "")
    postrun = tmp_path / "postrun.sh"
    postrun.write_bytes(renderer.patch_postrun(original.encode()))
    env = {
        **os.environ,
        "HOME": str(home),
        "HANDOFF_HANDOFF_ROOT": str(root),
        "HANDOFF_EXPECTED_LANE_TOKEN": credential,
        "CLOSER_NATIVE_LEASE_PY": str(LEASE_PATH),
        "CLOSER_NATIVE_LEASE_INTERPRETER": sys.executable,
    }
    # The default must derive from HANDOFF_HANDOFF_ROOT, independently of HOME.
    env.pop("HANDOFF_LANE_MUTEX_DIR", None)
    env["HANDOFF_HANDOFF_ROOT"] = str(root)
    env["HOME"] = str(tmp_path / "different-home")
    ok = subprocess.run(
        ["/bin/bash", str(postrun), "closer"], env=env, capture_output=True, text=True
    )
    assert ok.returncode == 0, ok.stderr
    assert "not found" not in ok.stderr and not lane.exists()
    lease.acquire(root, "closer", "codex", 100, lane_dir_override=str(lane))
    before = lease.read_meta_bytes(lane)
    marker = tmp_path / "must-not-execute"
    env["HANDOFF_EXPECTED_LANE_TOKEN"] = '$(touch "' + str(marker) + '")\nwrong credential\n'
    refused = subprocess.run(
        ["/bin/bash", str(postrun), "closer"], env=env, capture_output=True, text=True
    )
    assert refused.returncode == 2 and "release refused rc=2" in refused.stderr
    assert lease.read_meta_bytes(lane) == before and not marker.exists()


def test_generated_prerun_default_lane_uses_explicit_handoff_root(handoff_home, tmp_path):
    home, root = handoff_home
    custom = tmp_path / "custom-root"
    custom.mkdir()
    prerun = tmp_path / "prerun.sh"
    prerun.write_bytes(renderer.patch_prerun(_minimal_prerun().encode()))
    env = {
        **os.environ,
        "HOME": str(home),
        "HANDOFF_HANDOFF_ROOT": str(custom),
        "HANDOFF_BIN": str(home / ".codex/bin/handoff.sh"),
        "CLOSER_NATIVE_LEASE_PY": str(LEASE_PATH),
        "CLOSER_NATIVE_LEASE_INTERPRETER": sys.executable,
        "HANDOFF_PRERUN_MUTEX_ONLY": "1",
        "HANDOFF_REFUSE_EXISTING_LANE": "1",
    }
    env.pop("HANDOFF_LANE_MUTEX_DIR", None)
    result = subprocess.run(
        ["/bin/bash", str(prerun), "closer"], env=env, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert not (custom / ".lane-closer.lock").exists()
    assert not (root / ".lane-closer.lock").exists()


def test_postrun_release_shape_error_is_reported_and_propagated(handoff_home, tmp_path):
    home, root = handoff_home
    lane = root / ".lane-closer.lock"
    lease.acquire(root, "closer", "codex", 99, lane_dir_override=str(lane))
    before = lease.read_meta_bytes(lane)
    postrun = tmp_path / "postrun.sh"
    postrun.write_bytes(renderer.patch_postrun(_minimal_postrun().encode()))
    env = {
        **os.environ,
        "HOME": str(home),
        "HANDOFF_HANDOFF_ROOT": str(root),
        "HANDOFF_LANE_MUTEX_DIR": str(tmp_path / "outside-handoff-root"),
        "HANDOFF_EXPECTED_LANE_TOKEN": before.decode(),
        "CLOSER_NATIVE_LEASE_PY": str(LEASE_PATH),
        "CLOSER_NATIVE_LEASE_INTERPRETER": sys.executable,
    }
    result = subprocess.run(
        ["/bin/bash", str(postrun), "closer"], env=env, capture_output=True, text=True
    )
    assert result.returncode == 3
    assert "release refused rc=3" in result.stderr
    assert lease.read_meta_bytes(lane) == before
