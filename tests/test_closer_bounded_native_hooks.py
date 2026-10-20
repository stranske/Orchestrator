"""Private simulated native hooks: real lease, fake validation/relay boundary."""

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

import closer_native_lease as lease
import closer_runtime_binding as binding

SPEC = importlib.util.spec_from_file_location(
    "bounded_renderer",
    Path(__file__).resolve().parents[1] / "scripts/render_closer_native_hooks.py",
)
renderer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(renderer)


def canonical_postrun():
    return """#!/bin/bash
set -euo pipefail
lane="$1"; outcome="$2"
LANE_MUTEX_DIR="$HANDOFF_LANE_MUTEX_DIR"
launch_keepalive_label_repair() { echo hygiene >> "$EVENTS"; }
trap 'rm -rf "$LANE_MUTEX_DIR" 2>/dev/null || true; launch_keepalive_label_repair' EXIT
# BEGIN CLOSER STATUS DISPATCHER STOP GATE v1
_closer_dispatcher="$HOME/.codex/orchestrator-mirror/closer_dispatcher.py"
if ! python3 "$_closer_dispatcher" validate-stop --state "$CLOSER_ROUND_STATE" --assignment "$HOME/.codex/automations/reviewed-repo-automation-controller/closer-assignment.json"; then exit 65; fi
HANDOFF_BIN="$FAKE_HANDOFF"
"$HANDOFF_BIN" end-round "$outcome"
echo normal-hygiene >> "$EVENTS"
RELAY_BIN="$FAKE_RELAY"; relay_lane="closer"
if "$RELAY_BIN" "$relay_lane"; then :; fi
"""


@pytest.fixture
def hooks(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    source = (
        tmp_path / ".codex/automations/reviewed-repo-automation-controller/closer-assignment.json"
    )
    source.parent.mkdir(parents=True)
    source.write_text('{"assigned_targets":["offline/repo#1"]}')
    snapshot = tmp_path / "native-assignment-snapshot.json"
    snapshot.write_bytes(source.read_bytes())
    dispatcher = tmp_path / ".codex/orchestrator-mirror/closer_dispatcher.py"
    dispatcher.parent.mkdir(parents=True)
    dispatcher.write_bytes(Path(binding.__file__).with_name("closer_dispatcher.py").read_bytes())
    scripts = {}
    for phase in ("prerun", "postrun"):
        script = tmp_path / phase
        script.write_text(
            "#!/bin/bash\n# HANDOFF_EXACT_TOKEN_V1\n# CLOSER_BOUNDED_NO_RELAY_V1\n# CLOSER_NATIVE_ADMISSION_V1\n"
        )
        scripts[phase] = dict(path=str(script), sha256=binding._hash(script))
    module = Path(binding.__file__).with_name("closer_native_lease.py")
    scripts["lease_module"] = dict(path=str(module), sha256=binding._hash(module))
    scripts["bounded_no_new_relay"] = True
    scripts["postrun_dependencies"] = {
        name: dict(path=str(path), sha256=binding._hash(path))
        for name, path in dict(
            interpreter=Path(sys.executable).resolve(), dispatcher=dispatcher, assignment=snapshot
        ).items()
    }
    cfg = dict(
        native_hooks=scripts,
        handoff_agent="codex",
        handoff_dir=str(tmp_path / "handoff"),
        dispatch_state=str(tmp_path / "state"),
        assignment=str(snapshot),
        budget_seconds=600,
    )
    lane = tmp_path / "handoff/.lane-closer.lock"
    events = []

    def runner(argv, **kwargs):
        events.append(
            "offline-B"
            if argv[0] == scripts["postrun_dependencies"]["interpreter"]["path"]
            else argv[0]
        )
        if argv[0] == scripts["prerun"]["path"]:
            lane.mkdir(parents=True)
            (lane / "meta").write_text("original-token\n")
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=(
                    "CLOSER_NATIVE_ADMISSION_V1 "
                    + kwargs["env"]["CLOSER_ADMISSION_NONCE"]
                    + " ordinary\nordinary pickup"
                ).encode(),
                stderr=b"",
            )
        if argv[0] == scripts["postrun"]["path"]:
            assert kwargs["env"]["HANDOFF_EXPECTED_LANE_TOKEN"] == "original-token\n"
            (lane / "meta").unlink()
            lane.rmdir()
        return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")

    return cfg, scripts, source, snapshot, lane, events, runner


@pytest.mark.parametrize(
    "output",
    [
        b"RELAY-REVIEW: peer proposal",
        b"RELAY-HALTED: stop",
    ],
)
def test_peer_review_zero_exit_never_admits_b_but_c_once(hooks, output):
    cfg, scripts, _, _, lane, events, original = hooks

    def runner(argv, **kwargs):
        result = original(argv, **kwargs)
        if argv[0] == scripts["prerun"]["path"]:
            result.stdout = output
        return result

    outcome, _ = binding.run_native_round(
        [scripts["postrun_dependencies"]["interpreter"]["path"], "offline-dispatcher.py"],
        cfg,
        runner=runner,
    )
    assert outcome != "ok"
    assert events == [scripts["prerun"]["path"], scripts["postrun"]["path"]]
    assert not lane.exists()


def test_original_snapshot_shared_b_c_despite_controller_refresh(hooks):
    cfg, scripts, source, snapshot, lane, events, original = hooks
    cfg["capacity_report"] = str(snapshot.parent / "scoped.json")

    def runner(argv, **kwargs):
        if argv[0] == scripts["postrun_dependencies"]["interpreter"]["path"]:
            assert kwargs["env"]["CLOSER_CAPACITY_REPORT"] == cfg["capacity_report"]
            source.write_text('{"new":"controller assignment"}')
        if argv[0] == scripts["postrun"]["path"]:
            assert kwargs["env"]["CLOSER_POSTRUN_ASSIGNMENT"] == str(snapshot)
            assert kwargs["env"]["CLOSER_POSTRUN_DEPENDENCIES_VALID"] == "1"
            assert kwargs["env"]["CLOSER_NO_NEW_RELAY"] == "1"
        return original(argv, **kwargs)

    assert (
        binding.run_native_round(
            [
                scripts["postrun_dependencies"]["interpreter"]["path"],
                "offline-dispatcher.py",
                "--assignment",
                str(snapshot),
            ],
            cfg,
            runner=runner,
        )[0]
        == "ok"
    )
    assert not lane.exists()


@pytest.mark.parametrize("change", ["snapshot", "dispatcher"])
def test_changed_dependency_after_a_enters_cleanup_only(hooks, change):
    cfg, scripts, _, snapshot, lane, _, original = hooks

    def runner(argv, **kwargs):
        if argv[0] == scripts["postrun_dependencies"]["interpreter"]["path"]:
            Path(
                scripts["postrun_dependencies"][
                    "assignment" if change == "snapshot" else "dispatcher"
                ]["path"]
            ).write_text("changed")
        if argv[0] == scripts["postrun"]["path"]:
            assert kwargs["env"]["CLOSER_POSTRUN_DEPENDENCIES_VALID"] == "0"
            original(argv, **kwargs)
            return subprocess.CompletedProcess(argv, 65)
        return original(argv, **kwargs)

    assert (
        binding.run_native_round(
            [
                scripts["postrun_dependencies"]["interpreter"]["path"],
                "offline-dispatcher.py",
                "--assignment",
                str(snapshot),
            ],
            cfg,
            runner=runner,
        )[0]
        == "unknown"
    )
    assert not lane.exists()


def test_foreign_snapshot_or_physical_pin_never_admits(hooks):
    cfg, scripts, source, _, lane, events, runner = hooks
    source.write_text("foreign routing")
    assert (
        binding.run_native_round(
            [
                scripts["postrun_dependencies"]["interpreter"]["path"],
                "offline-dispatcher.py",
                "--assignment",
                cfg["assignment"],
            ],
            cfg,
            runner=runner,
        )[0]
        == "unavailable"
    )
    assert not events and not lane.exists()
    scripts["postrun_dependencies"]["assignment"]["path"] = str(source)
    with pytest.raises(binding.BindingError):
        binding.PinnedNativeHooks(cfg, runner=runner)


@pytest.mark.parametrize("valid", [True, False])
def test_renderer_preserves_validation_end_round_hygiene_and_blocks_only_new_relay(tmp_path, valid):
    root = tmp_path / "handoff"
    root.mkdir()
    lane = root / ".lane-closer.lock"
    lease.acquire(root, "closer", "codex", os.getpid())
    token = (lane / "meta").read_text()
    events = tmp_path / "events"
    fake = tmp_path / "handoff"
    # root already occupies handoff; executable is a separate fake transport.
    fake = tmp_path / "fake-handoff"
    fake.write_text('#!/bin/bash\necho end-round >> "$EVENTS"\n')
    fake.chmod(0o700)
    fake_relay = tmp_path / "fake-relay"
    fake_relay.write_text('#!/bin/bash\necho forbidden-global-capacity-selector >> "$EVENTS"\n')
    fake_relay.chmod(0o700)
    validator = tmp_path / "validate.py"
    validator.write_text(
        'import os\nfrom pathlib import Path\nPath(os.environ["EVENTS"]).write_text("validate\\n")\n'
    )
    script = tmp_path / "postrun"
    script.write_bytes(renderer.patch_postrun(canonical_postrun().encode(), bounded_round=True))
    env = {
        **os.environ,
        "EVENTS": str(events),
        "FAKE_HANDOFF": str(fake),
        "FAKE_RELAY": str(fake_relay),
        "HOME": str(tmp_path),
        "HANDOFF_LANE_MUTEX_DIR": str(lane),
        "HANDOFF_HANDOFF_ROOT": str(root),
        "HANDOFF_EXPECTED_LANE_TOKEN": token,
        "CLOSER_NATIVE_LEASE_PY": str(Path(lease.__file__)),
        "CLOSER_NATIVE_LEASE_INTERPRETER": sys.executable,
        "CLOSER_POSTRUN_DEPENDENCIES_VALID": "1" if valid else "0",
        "CLOSER_POSTRUN_PYTHON": sys.executable,
        "CLOSER_POSTRUN_DISPATCHER": str(validator),
        "CLOSER_POSTRUN_ASSIGNMENT": str(tmp_path / "snapshot"),
        "CLOSER_NO_NEW_RELAY": "1",
        "CLOSER_ROUND_STATE": str(tmp_path / "state"),
    }
    result = subprocess.run(
        ["/bin/bash", str(script), "closer", "no_op"], env=env, capture_output=True
    )
    assert result.returncode == (0 if valid else 65), result.stderr
    assert not lane.exists()
    assert events.read_text().splitlines() == (
        ["validate", "end-round", "normal-hygiene", "hygiene"] if valid else ["hygiene"]
    )


def test_renderer_default_unchanged_and_unknown_body_refused():
    default = renderer.patch_postrun(canonical_postrun().encode())
    assert b"CLOSER_BOUNDED_NO_RELAY_V1" not in default
    assert b'if "$RELAY_BIN" "$relay_lane"; then' in default
    with pytest.raises(ValueError, match="anchors"):
        renderer.patch_postrun(
            canonical_postrun().replace('"$HANDOFF_BIN" end-round "$outcome"', ":").encode(),
            bounded_round=True,
        )


def test_generation_symlink_is_expected_but_retarget_never_authoritative(hooks):
    cfg, scripts, source, snapshot, lane, events, runner = hooks
    logical = source.parents[2] / "orchestrator-mirror"
    generation = logical.with_name("runtime-generation-one")
    logical.rename(generation)
    logical.symlink_to(generation, target_is_directory=True)
    entry = scripts["postrun_dependencies"]["dispatcher"]
    entry["path"] = str(generation / "closer_dispatcher.py")
    adapter = binding.PinnedNativeHooks(cfg, runner=runner)
    assert adapter._dependencies_match(admission=True)
    successor = logical.with_name("runtime-generation-two")
    successor.mkdir()
    (successor / "closer_dispatcher.py").write_bytes(
        (generation / "closer_dispatcher.py").read_bytes()
    )
    logical.unlink()
    logical.symlink_to(successor, target_is_directory=True)
    assert not adapter._dependencies_match(admission=True)
    assert adapter.prerun(10**12) == (65, None)
    assert not events and not lane.exists()


def test_cleared_pending_review_race_preserves_normal_admission(hooks):
    cfg, scripts, _, snapshot, lane, events, original = hooks

    def runner(argv, **kwargs):
        result = original(argv, **kwargs)
        if argv[0] == scripts["prerun"]["path"]:
            result.stdout += (
                b"RELAY-REVIEW-RACE: pending_review cleared; proceeding normally\nnormal pickup"
            )
        return result

    outcome, _ = binding.run_native_round(
        [
            scripts["postrun_dependencies"]["interpreter"]["path"],
            "offline-dispatcher.py",
            "--assignment",
            str(snapshot),
        ],
        cfg,
        runner=runner,
    )
    assert outcome == "ok"
    assert events == [scripts["prerun"]["path"], "offline-B", scripts["postrun"]["path"]]
    assert not lane.exists()


def test_bounded_policy_cannot_extend_round_beyond_900_seconds(hooks):
    cfg, scripts, _, _, lane, events, runner = hooks
    cfg.update(budget_seconds=1000, original_deadline=binding.time.time() + 1000)
    assert (
        binding.run_native_round(
            [scripts["postrun_dependencies"]["interpreter"]["path"], "offline-dispatcher.py"],
            cfg,
            runner=runner,
        )[0]
        == "unavailable"
    )
    assert not events and not lane.exists()


@pytest.mark.parametrize(
    "alternate", [["--assignment", "foreign"], ["--assignment=foreign"], ["--assign", "foreign"]]
)
def test_ambiguous_actual_b_assignment_never_runs_and_c_once(hooks, alternate):
    cfg, scripts, _, snapshot, lane, events, runner = hooks
    command = [
        scripts["postrun_dependencies"]["interpreter"]["path"],
        "offline-dispatcher.py",
        "--assignment",
        str(snapshot),
        *alternate,
    ]
    assert binding.run_native_round(command, cfg, runner=runner)[0] != "ok"
    assert events == [scripts["prerun"]["path"], scripts["postrun"]["path"]]
    assert not lane.exists()


@pytest.mark.parametrize("case", ["historic", "missing", "wrong", "ambiguous", "review"])
def test_fresh_admission_record_is_authority_not_resume_memory(hooks, case):
    cfg, scripts, _, snapshot, lane, events, original = hooks

    def runner(argv, **kwargs):
        result = original(argv, **kwargs)
        if argv[0] == scripts["prerun"]["path"]:
            nonce = kwargs["env"]["CLOSER_ADMISSION_NONCE"]
            record = "CLOSER_NATIVE_ADMISSION_V1 " + nonce + " "
            if case == "historic":
                result.stdout += b"old memory: RELAY-HALTED: old pause\nRELAY-REVIEW: old proposal"
            elif case == "missing":
                result.stdout = b"normal resume but no trusted gate record"
            elif case == "wrong":
                result.stdout = b"CLOSER_NATIVE_ADMISSION_V1 stale-nonce ordinary"
            elif case == "ambiguous":
                result.stdout += ("\n" + record + "review\n").encode()
            else:
                result.stdout = (record + "review\n").encode()
        return result

    outcome, _ = binding.run_native_round(
        [
            scripts["postrun_dependencies"]["interpreter"]["path"],
            "offline-dispatcher.py",
            "--assignment",
            str(snapshot),
        ],
        cfg,
        runner=runner,
    )
    assert (outcome == "ok") == (case == "historic")
    assert events.count("offline-B") == (1 if case == "historic" else 0)
    assert events.count(scripts["postrun"]["path"]) == 1
    assert not lane.exists()


@pytest.mark.parametrize(
    "mode,verdict", [("ordinary", "ordinary"), ("race", "ordinary"), ("review", "review")]
)
def test_rendered_prerun_emits_nonce_verdict_before_resume_memory(tmp_path, mode, verdict):
    original = """#!/bin/bash
set -euo pipefail
lane=closer; agent=codex
# --- Per-lane round mutex
old_mutex_placeholder=true
# Capture both stdout and stderr; route by exit code below.
case "$MODE" in
  review)
      "$HANDOFF_BIN" read-resume "$lane"
      exit 0
      ;;
  race) echo "RELAY-REVIEW-RACE: cleared, proceeding normally" ;;
esac

"$HANDOFF_BIN" pickup >/dev/null
"$HANDOFF_BIN" read-resume "$lane"
"""
    script = tmp_path / "prerun"
    script.write_bytes(renderer.patch_prerun(original.encode(), bounded_round=True))
    fake = tmp_path / "fake-handoff"
    fake.write_text(
        '#!/bin/bash\nif [[ "$1" == "read-resume" ]]; then echo "RELAY-HALTED: historical memory only"; fi\n'
    )
    fake.chmod(0o700)
    root = tmp_path / "private-handoff"
    root.mkdir()
    env = {
        **os.environ,
        "MODE": mode,
        "HANDOFF_BIN": str(fake),
        "HANDOFF_HANDOFF_ROOT": str(root),
        "HANDOFF_REFUSE_EXISTING_LANE": "1",
        "CLOSER_NATIVE_LEASE_PY": str(Path(lease.__file__)),
        "CLOSER_NATIVE_LEASE_INTERPRETER": sys.executable,
        "CLOSER_ADMISSION_NONCE": "offline-fresh-nonce",
    }
    result = subprocess.run(["/bin/bash", str(script)], env=env, capture_output=True, text=True)
    lane = root / ".lane-closer.lock"
    try:
        assert result.returncode == 0, result.stderr
        record = "CLOSER_NATIVE_ADMISSION_V1 offline-fresh-nonce " + verdict
        assert result.stdout.splitlines().count(record) == 1
        assert result.stdout.index(record) < result.stdout.index("RELAY-HALTED: historical")
    finally:
        if lane.exists():
            token = (lane / "meta").read_text()
            lease.release(root, "closer", token)


def test_bounded_lease_and_b_use_physical_python_and_retarget_fences(hooks, tmp_path, monkeypatch):
    cfg, scripts, _, snapshot, lane, events, runner = hooks
    physical = scripts["postrun_dependencies"]["interpreter"]["path"]
    alias = tmp_path / "python-alias"
    alias.symlink_to(physical)
    monkeypatch.setattr(binding.sys, "executable", str(alias))
    adapter = binding.PinnedNativeHooks(cfg, runner=runner)
    assert adapter.env["CLOSER_NATIVE_LEASE_INTERPRETER"] == physical
    assert adapter.env["CLOSER_POSTRUN_PYTHON"] == physical
    assert (
        binding.run_native_round(
            [str(alias), "offline-dispatcher.py", "--assignment", str(snapshot)], cfg, runner=runner
        )[0]
        == "ok"
    )
    alias.unlink()
    other = tmp_path / "other-python"
    other.write_text("not pinned interpreter")
    alias.symlink_to(other)
    assert not adapter._pins_match()
    assert not adapter._dependencies_match()
    assert adapter.prerun(10**12) == (65, None)
    assert not lane.exists()


def test_changed_interpreter_bytes_after_b_never_launches_cleanup_with_changed_binary(
    hooks, monkeypatch
):
    cfg, scripts, _, snapshot, lane, events, original = hooks
    physical = scripts["postrun_dependencies"]["interpreter"]["path"]
    original_hash = binding._hash

    def runner(argv, **kwargs):
        result = original(argv, **kwargs)
        if argv[0] == physical:
            monkeypatch.setattr(
                binding,
                "_hash",
                lambda path: "0" * 64 if str(path) == physical else original_hash(path),
            )
        return result

    try:
        outcome, detail = binding.run_native_round(
            [physical, "offline-dispatcher.py", "--assignment", str(snapshot)], cfg, runner=runner
        )
        assert outcome == "unknown" and "ACTION C65" in detail
        assert events == [scripts["prerun"]["path"], "offline-B"]
        assert (lane / "meta").read_text() == "original-token\n"
    finally:
        # Disposable fixture only, not a native lease or production state.
        (lane / "meta").unlink()
        lane.rmdir()
