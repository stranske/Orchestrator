"""Claude identity IO must not erase an already observed provider completion."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_dispatcher_controlled_data_only import world as _controlled_world

import adapters
import dispatcher
import execution_profiles
import feedback
import ledger_reconcile

controlled_world = _controlled_world

PROFILE = "claude-sonnet-5-5-low"
REASON = "claude_transcript_probe_oserror:ENAMETOOLONG"


@pytest.fixture(autouse=True)
def private_state(tmp_path, monkeypatch):
    for key in ("ORCH_STATE_DIR", "ORCH_LOCAL_RUNTIME"):
        monkeypatch.setenv(key, str(tmp_path / key))
    monkeypatch.setenv(dispatcher.credential_redaction.SECRET_FILES_ENV, "")
    monkeypatch.delenv("ORCH_OFFLOAD_DISABLED", raising=False)
    monkeypatch.delenv(dispatcher.credential_redaction.REDACTION_DISABLED_ENV, raising=False)
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "feedback.db")
    monkeypatch.setattr(adapters, "HANDOFF", tmp_path)
    monkeypatch.setattr(adapters, "LEDGER", tmp_path / "capacity.ndjson")
    projects = tmp_path / "empty-claude-projects"
    projects.mkdir()
    monkeypatch.setattr(adapters, "CLAUDE_PROJECTS", projects)
    monkeypatch.setattr(dispatcher, "DISPATCH_LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(dispatcher, "REAL_HOME", tmp_path / "home")
    monkeypatch.setattr(dispatcher, "AGENT_RUNTIME_DIR", tmp_path / "runtime")

    def forbidden(*args, **kwargs):
        pytest.fail("unexpected real process or network call")

    monkeypatch.setattr(dispatcher.subprocess, "run", forbidden)
    monkeypatch.setattr(dispatcher.subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)


@pytest.fixture
def long_workspace(tmp_path):
    """Every real component is valid; only Claude's combined encoded key is too long."""
    limit = os.pathconf(tmp_path, "PC_NAME_MAX")
    workspace = tmp_path / "workspace"
    while len(str(workspace.resolve()).encode()) <= limit + 20:
        workspace /= "nested-" + "x" * min(80, limit - 8)
    workspace.mkdir(parents=True)
    encoded = adapters._claude_project_dir(workspace)
    assert len(encoded.name.encode()) > limit
    with pytest.raises(OSError) as error:
        encoded.stat()
    assert error.value.errno == errno.ENAMETOOLONG
    return workspace.resolve()


def test_actual_over_name_max_cwd_is_named_unresolved(long_workspace):
    assert adapters.cli_reported_model("claude", long_workspace) == {
        "model": None,
        "cli_version": None,
        "source": None,
        "reason": REASON,
    }


def _offload(
    monkeypatch,
    workspace,
    callback=None,
    output="actual fake provider result",
    failure=None,
    agent="claude",
    profile_id=PROFILE,
    provider_argv=None,
    observed=False,
    retries=0,
    returncode=0,
    stderr="",
    provider_calls=None,
):
    profile = execution_profiles.get_profile(profile_id)
    calls = provider_calls if provider_calls is not None else []
    monkeypatch.setattr(adapters, "agent_workspace", lambda *a, **k: (workspace, None))
    monkeypatch.setattr(dispatcher, "_offload_prompt", lambda prompt, *a: prompt)
    monkeypatch.setattr(dispatcher, "_select_offload_profile", lambda *a: profile)
    monkeypatch.setattr(
        adapters, "build_command", lambda *a, **k: provider_argv or ["private-fake-provider"]
    )
    monkeypatch.setattr(adapters, "model_identity", lambda *a: profile["requested_model"])
    monkeypatch.setattr(dispatcher, "_agent_runtime_prelude", lambda *a: "")
    monkeypatch.setattr(dispatcher, "_auth_prelude", lambda *a: "")
    monkeypatch.setattr(dispatcher, "_capability_heartbeat", lambda *a, **k: None)
    monkeypatch.setattr(dispatcher, "_exercised_capability_ids", lambda *a: [])
    import rate_incidents

    monkeypatch.setattr(rate_incidents, "get_structured_evidence", lambda **k: None)
    monkeypatch.setattr(rate_incidents, "provider_reset_at", lambda *a: None)

    def provider(*args, **kwargs):
        calls.append((args, kwargs))
        if failure is not None:
            raise failure
        return SimpleNamespace(returncode=returncode, stdout=output, stderr=stderr)

    monkeypatch.setattr(dispatcher.subprocess, "run", provider)
    if observed:
        monkeypatch.setattr(dispatcher, "run_observed_offload", provider)
    result = dispatcher.offload(
        agent,
        "private fixture",
        cwd=str(workspace),
        profile_id=profile_id,
        max_network_retries=retries,
        attempt_receipt_callback=callback,
        **({"process_started_callback": lambda event: None} if observed else {}),
    )
    return result, calls


def _attempt(run_id):
    with feedback._conn() as conn:
        return conn.execute(
            "SELECT status,requested_model,resolved_provider,resolved_model,fallback_reason "
            "FROM execution_attempts WHERE run_id=?",
            (run_id,),
        ).fetchone()


def test_actual_offload_callback_then_unresolved_completion_and_return(
    long_workspace, monkeypatch, tmp_path
):
    completed = []
    real_complete = feedback.complete_profile_attempt_unresolved

    def callback(event):
        completed.append(dict(event))
        receipt = _receipt(event)
        assert receipt["result"]["run_id"] == event["run_id"]
        assert receipt["full_result_retained"] is True
        with (tmp_path / "provider-completed.json").open("w") as stream:
            json.dump(event, stream)
            stream.flush()
            os.fsync(stream.fileno())
        assert _attempt(event["run_id"])[0] == "started"

    def complete(run_id, **kwargs):
        assert len(completed) == 1
        assert json.loads((tmp_path / "provider-completed.json").read_text())["run_id"] == run_id
        return real_complete(run_id, **kwargs)

    monkeypatch.setattr(feedback, "complete_profile_attempt_unresolved", complete)
    result, calls = _offload(monkeypatch, long_workspace, callback)
    assert len(calls) == len(completed) == 1
    assert result["exit"] == completed[0]["exit"] == 0
    assert result["run_id"] == completed[0]["run_id"]
    assert result["output"] == "actual fake provider result"
    assert result["served_model"] is None and result["served_model_status"] == "unknown"
    assert _attempt(result["run_id"]) == (
        "unresolved",
        "claude-sonnet-5-5",
        None,
        None,
        "resolved_model_not_reported_by_offload:" + REASON,
    )
    ledger = [json.loads(line) for line in adapters.LEDGER.read_text().splitlines()]
    assert [r["event"] for r in ledger] == ["start", "complete"]


def _receipt(result):
    return json.loads(Path(result["log"]).with_suffix(".provider-result-1.json").read_text())


def test_unavailable_identity_is_named(tmp_path, monkeypatch):
    workspace = tmp_path / "work"
    directory = adapters._claude_project_dir(workspace)
    original = Path.stat

    def inaccessible(path, *args, **kwargs):
        if path == directory:
            raise PermissionError(errno.EACCES, "private fixture")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", inaccessible)
    probe = adapters.cli_reported_model("claude", workspace)
    assert probe["model"] is None
    assert probe["reason"] == "claude_transcript_probe_oserror:EACCES"


def test_valid_identity_and_disappearing_transcript(tmp_path, monkeypatch):
    workspace = tmp_path / "work"
    directory = adapters._claude_project_dir(workspace)
    directory.mkdir()
    transcript = directory / "own.jsonl"
    transcript.write_text(json.dumps({"message": {"model": "claude-actual"}}) + "\n")
    assert adapters.cli_reported_model("claude", workspace)["model"] == "claude-actual"
    original = Path.open

    def disappear(path, *args, **kwargs):
        if path == transcript:
            transcript.unlink()
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", disappear)
    probe = adapters.cli_reported_model("claude", workspace)
    assert probe["model"] is None
    assert probe["reason"] == "claude_transcript_probe_oserror:ENOENT"


def test_standard_offload_without_callback_retains_result(long_workspace, monkeypatch):
    result, calls = _offload(monkeypatch, long_workspace)
    assert len(calls) == 1 and result["exit"] == 0
    receipt = _receipt(result)
    assert receipt["result"]["output"] == result["output"]
    assert receipt["profile_id"] == PROFILE and receipt["cwd"] == str(long_workspace)
    assert receipt["result"]["served_model"] is None
    assert feedback.resolved_worker_model_for_run(result["run_id"]) is None
    with feedback._conn() as conn:
        assert execution_profiles.resolved_model_coverage(conn, PROFILE)["learning_ready"] is False


@pytest.mark.parametrize("boundary", ["callback", "identity"])
def test_exception_after_provider_keeps_durable_result(long_workspace, monkeypatch, boundary):
    observed = []

    def fail(event):
        observed.append(_receipt(event))
        raise RuntimeError("private enrichment failure")

    if boundary == "identity":

        def identity(*args, **kwargs):
            logs = list(dispatcher.DISPATCH_LOG_DIR.glob("*.provider-result-1.json"))
            observed.append(json.loads(logs[0].read_text()))
            raise RuntimeError("private enrichment failure")

        monkeypatch.setattr(adapters, "cli_reported_model", identity)
    with pytest.raises(RuntimeError, match="private enrichment failure"):
        _offload(monkeypatch, long_workspace, fail if boundary == "callback" else None)
    assert len(observed) == 1
    assert observed[0]["exit"] == 0
    assert observed[0]["result"]["output"] == "actual fake provider result"


def test_retention_failure_holds_without_callback_or_enrichment(long_workspace, monkeypatch):
    def unavailable(*args, **kwargs):
        raise OSError(errno.ENOSPC, "private fixture")

    monkeypatch.setattr(dispatcher, "_retain_provider_result", unavailable)

    def forbidden(*args, **kwargs):
        pytest.fail("retention failure must stop enrichment/callback")

    monkeypatch.setattr(adapters, "cli_reported_model", forbidden)
    result, calls = _offload(monkeypatch, long_workspace, forbidden)
    assert len(calls) == 1
    assert result["exit"] == 70 and result["raw_exit"] == 0
    assert result["provider_completed"] is True
    assert result["outcome_uncertain"] is True and result["retry_safe"] is False
    assert _attempt(result["run_id"])[0] == "started"


def test_sidecar_create_only_and_redaction(tmp_path):
    log = tmp_path / "own.log"
    token = "ghp_" + "A" * 36
    result = {
        "run_id": "own",
        "agent": "claude",
        "cwd": str(tmp_path),
        "process_cwd": str(tmp_path),
        "exit": 0,
        "output": token,
    }
    path = dispatcher._retain_provider_result(
        log,
        result,
        profile_id=PROFILE,
        attempt=1,
        started_at=1,
        completed_at=2,
        run_cwd=tmp_path,
        process_cwd=tmp_path,
    )
    before = path.read_bytes()
    assert token.encode() not in before
    with pytest.raises(FileExistsError):
        dispatcher._retain_provider_result(
            log,
            result,
            profile_id=PROFILE,
            attempt=1,
            started_at=1,
            completed_at=2,
            run_cwd=tmp_path,
            process_cwd=tmp_path,
        )
    assert path.read_bytes() == before


def test_redaction_opt_out_does_not_copy_free_text(long_workspace, monkeypatch):
    monkeypatch.setenv(dispatcher.credential_redaction.REDACTION_DISABLED_ENV, "1")
    token = "ghp_" + "A" * 36
    result, calls = _offload(monkeypatch, long_workspace, output=token)
    assert len(calls) == 1 and result["output"] == token and result["exit"] == 0
    receipt = _receipt(result)
    assert receipt["full_result_retained"] is False
    assert receipt["omitted_free_text"] is True and "result" not in receipt
    assert token not in json.dumps(receipt)


def test_all_ledger_identity_callers_preserve_unresolved(long_workspace, monkeypatch):
    result, _ = _offload(monkeypatch, long_workspace)
    probe = ledger_reconcile._cli_identity_for_run(
        [{"agent": "claude", "target": "offload:" + str(long_workspace)}]
    )
    assert probe["model"] is None and probe["reason"] == REASON
    for apply in (False, True):
        sweep = ledger_reconcile.resolve_unresolved_worker_attempts(apply=apply)
        assert not sweep["resolved_by_agent"]
        assert sweep["blocked_by_reason"] == {"claude:claude_transcript_probe_oserror": 1}
    ledger_reconcile.record_completion(
        result["run_id"],
        "claude",
        target="offload:" + str(long_workspace),
        selected_profile_id=PROFILE,
        exit_code=0,
    )
    assert _attempt(result["run_id"])[0] == "unresolved"
    assert feedback.resolved_worker_model_for_run(result["run_id"]) is None


def test_unreadable_transcript_keeps_model_unknown(tmp_path, monkeypatch):
    workspace = tmp_path / "work"
    directory = adapters._claude_project_dir(workspace)
    directory.mkdir()
    transcript = directory / "own.jsonl"
    transcript.write_text(json.dumps({"message": {"model": "claude-actual"}}))
    original = Path.open

    def unreadable(path, *args, **kwargs):
        if path == transcript:
            raise PermissionError(errno.EACCES, "private fixture")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", unreadable)
    probe = adapters.cli_reported_model("claude", workspace)
    assert probe["model"] is None
    assert probe["reason"] == "claude_transcript_probe_oserror:EACCES"


def test_missing_identity_and_programming_errors_are_distinct(tmp_path, monkeypatch):
    workspace = tmp_path / "work"
    assert adapters.cli_reported_model("claude", workspace)["reason"] == (
        "no_claude_transcript_dir_for_workspace"
    )

    def broken(*args):
        raise RuntimeError("private programming error")

    monkeypatch.setattr(adapters, "_claude_project_dir", broken)
    with pytest.raises(RuntimeError, match="programming error"):
        adapters.cli_reported_model("claude", workspace)


def test_committed_receipt_cleanup_denial_preserves_callback_and_return(
    long_workspace, monkeypatch
):
    original = os.unlink
    denied = []

    def deny_temp(path, *args, **kwargs):
        if Path(path).name.startswith(".provider-result-"):
            denied.append(str(path))
            raise PermissionError(errno.EACCES, "private injected cleanup denial")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", deny_temp)
    completed = []

    def callback(event):
        completed.append(_receipt(event))

    result, calls = _offload(monkeypatch, long_workspace, callback)
    assert len(calls) == len(completed) == len(denied) == 1
    assert result["exit"] == 0 and completed[0]["exit"] == 0
    assert Path(denied[0]).exists()


def test_retained_hash_binds_nested_redacted_representation(tmp_path):
    token = "ghp_" + "A" * 36
    result = {
        "run_id": "own",
        "agent": "claude",
        "cwd": str(tmp_path),
        "process_cwd": str(tmp_path),
        "exit": 0,
        "request_metadata": {"nested": [token]},
        "output": token,
    }
    path = dispatcher._retain_provider_result(
        tmp_path / "own.log",
        result,
        profile_id=PROFILE,
        attempt=1,
        started_at=1,
        completed_at=2,
        run_cwd=tmp_path,
        process_cwd=tmp_path,
    )
    receipt = json.loads(path.read_text())
    assert receipt["retention_kind"] == "redacted_provider_result"
    assert (
        receipt["provider_result_sha256"]
        == hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()
    )
    assert (
        receipt["retained_result_sha256"]
        == hashlib.sha256(json.dumps(receipt["result"], sort_keys=True).encode()).hexdigest()
    )
    assert receipt["provider_result_sha256"] != receipt["retained_result_sha256"]
    assert token not in path.read_text()


def test_cleanup_denial_cannot_shadow_original_precommit_error(tmp_path, monkeypatch):
    result = {
        "run_id": "own",
        "agent": "claude",
        "cwd": str(tmp_path),
        "process_cwd": str(tmp_path),
        "exit": 0,
    }

    def no_link(*args):
        raise FileExistsError(errno.EEXIST, "original precommit failure")

    def no_cleanup(*args):
        raise PermissionError(errno.EACCES, "secondary cleanup failure")

    monkeypatch.setattr(os, "link", no_link)
    monkeypatch.setattr(os, "unlink", no_cleanup)
    with pytest.raises(FileExistsError, match="original precommit failure"):
        dispatcher._retain_provider_result(
            tmp_path / "own.log",
            result,
            profile_id=PROFILE,
            attempt=1,
            started_at=1,
            completed_at=2,
            run_cwd=tmp_path,
            process_cwd=tmp_path,
        )


@pytest.mark.parametrize("failure", ["observation", "timeout", "start"])
def test_all_failure_return_shapes_retain_authoritative_workspace(
    long_workspace, monkeypatch, failure
):
    import subprocess

    observation = {
        "source": "trusted-popen-transport/v1",
        "pid": 999,
        "process_group": 999,
        "cleanup_terminal": False,
        "cleanup_veto": True,
        "os_started": "private process birth",
        "run_id": "owned-fixture",
        "finite_frontier_id": "private-frontier",
        "finite_frontier_target": "o/r#1",
    }
    if failure == "observation":
        error = dispatcher.ObservedOffloadFailure(
            "synthetic unknown", observation, -9, "partial-output", "partial-error"
        )
    elif failure == "timeout":
        error = subprocess.TimeoutExpired("private-fake-provider", 1)
    else:
        error = OSError(errno.ENOENT, "synthetic process start failure")
    completed = []
    result, calls = _offload(
        monkeypatch,
        long_workspace,
        completed.append,
        failure=error,
        observed=(failure == "observation"),
    )
    assert len(calls) == len(completed) == 1
    receipt = _receipt(result)
    assert receipt["cwd"] == receipt["process_cwd"] == str(long_workspace)
    assert receipt["result"]["exit"] == result["exit"]
    if failure == "observation":
        assert result["outcome_uncertain"] is True and result["retry_safe"] is False
        assert result["process_started"] is True and result["raw_exit"] == -9
        assert result["finite_pool_fence"] == receipt["result"]["finite_pool_fence"]
        assert result["output"] == "partial-output"
    elif failure == "timeout":
        assert result["exit"] == 124
    else:
        assert result["exit"] == 126


def test_keyboard_interrupt_retained_before_callback_and_enrichment(long_workspace, monkeypatch):
    completed = []

    def callback(event):
        assert _receipt(event)["result"]["exit"] == 130
        completed.append(event)

    with pytest.raises(KeyboardInterrupt):
        _offload(monkeypatch, long_workspace, callback, failure=KeyboardInterrupt())
    assert len(completed) == 1
    assert _attempt(completed[0]["run_id"])[0] == "unresolved"


def test_keyboard_interrupt_retention_failure_is_held(long_workspace, monkeypatch):
    attempts = []

    def unavailable(*args, **kwargs):
        attempts.append(1)
        raise OSError(errno.ENOSPC, "private fixture")

    monkeypatch.setattr(dispatcher, "_retain_provider_result", unavailable)

    def forbidden(*args):
        pytest.fail("failed retention must not call completion callback")

    interruption = KeyboardInterrupt()
    calls = []
    with pytest.raises(KeyboardInterrupt) as error:
        _offload(monkeypatch, long_workspace, forbidden, failure=interruption, provider_calls=calls)
    assert error.value is interruption
    result = error.value.held_result
    assert len(calls) == len(attempts) == 1 and result["raw_exit"] == 130 and result["exit"] == 70
    assert result["outcome_uncertain"] is True and result["retry_safe"] is False
    assert _attempt(result["run_id"])[0] == "started"


@pytest.mark.parametrize(
    "agent,profile_id", [("muse", "muse-wonder-1.0-low"), ("gemini", "gemini-3.8-flash-low")]
)
def test_retention_barrier_preserves_prompt_cleanup_and_log_scrub(
    tmp_path, monkeypatch, agent, profile_id
):
    prompt = tmp_path / "private-prompt"
    prompt.write_text("private prompt")
    agy = tmp_path / "private-agy.log"
    agy.write_text("private log")
    monkeypatch.setattr(adapters, "muse_prompt_file", lambda text: prompt)
    monkeypatch.setattr(adapters, "agy_log_for", lambda log: agy)
    scrubbed = []

    def scrub(path, **kwargs):
        scrubbed.append(path)
        return {"status": "ok", "kinds": {}}

    monkeypatch.setattr(dispatcher.credential_redaction, "scrub_file", scrub)

    def unavailable(*args, **kwargs):
        raise OSError(errno.ENOSPC, "private fixture")

    monkeypatch.setattr(dispatcher, "_retain_provider_result", unavailable)
    result, calls = _offload(
        monkeypatch,
        tmp_path,
        agent=agent,
        profile_id=profile_id,
        provider_argv=["private-provider", "--log-file", "placeholder"],
    )
    assert len(calls) == 1 and result["outcome_status"] == "uncertain"
    if agent == "muse":
        assert not prompt.exists()
    else:
        assert agy in scrubbed


def test_cleanup_fault_cannot_erase_retention_barrier(tmp_path, monkeypatch):
    prompt = tmp_path / "private-prompt"
    prompt.write_text("private prompt")
    monkeypatch.setattr(adapters, "muse_prompt_file", lambda text: prompt)
    original = Path.unlink

    def denied(path, *args, **kwargs):
        if path == prompt:
            raise PermissionError(errno.EACCES, "private injected cleanup fault")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", denied)

    def unavailable(*args, **kwargs):
        raise OSError(errno.ENOSPC, "private fixture")

    monkeypatch.setattr(dispatcher, "_retain_provider_result", unavailable)
    result, calls = _offload(monkeypatch, tmp_path, agent="muse", profile_id="muse-wonder-1.0-low")
    assert len(calls) == 1
    assert result["error"] == "provider_result_retention_failed:OSError"
    assert result["cleanup_errors"] == ["muse_prompt:PermissionError"]
    assert result["outcome_status"] == "uncertain" and result["retry_safe"] is False


def test_interrupt_retention_failure_is_not_retried(long_workspace, monkeypatch):
    attempts = []
    real = dispatcher._retain_provider_result

    def once_failed(*args, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise OSError(errno.ENOSPC, "one private failure")
        return real(*args, **kwargs)

    monkeypatch.setattr(dispatcher, "_retain_provider_result", once_failed)

    def forbidden(*args, **kwargs):
        pytest.fail("retention failure must not reach callback or enrichment")

    monkeypatch.setattr(adapters, "cli_reported_model", forbidden)
    interruption = KeyboardInterrupt()
    calls = []
    with pytest.raises(KeyboardInterrupt) as error:
        _offload(monkeypatch, long_workspace, forbidden, failure=interruption, provider_calls=calls)
    assert error.value is interruption
    result = error.value.held_result
    assert len(attempts) == len(calls) == 1
    assert result["outcome_status"] == "uncertain" and result["raw_exit"] == 130
    assert result["retry_safe"] is False


def test_real_retention_barrier_reaches_closer_interpret_uncertainty(
    tmp_path, monkeypatch, long_workspace, controlled_world
):
    import time

    from closer_inert_dispatcher_support import InertCaller
    from test_closer_dispatcher import FakeGitHub, adapter, closer, snapshot

    import closer_evidence
    import closer_pipeline
    import execution_profiles

    monkeypatch.setenv("HOME", str(controlled_world.root / "private-home"))
    monkeypatch.setenv("HANDOFF_DIR", str(controlled_world.root / "private-handoff"))
    caller = InertCaller(controlled_world, monkeypatch)
    target = "o/r#41"
    current = snapshot(
        target=target,
        base="c" * 40,
        state="MERGED",
        merge="b" * 40,
        verifier={"verdict": "PASS", "run_id": "42", "evaluated_sha": "b" * 40},
    )
    prepared_payload = {}

    class PreparedGitHub(FakeGitHub):
        def __init__(self, *, deadline):
            super().__init__(current)
            self.deadline = deadline

    monkeypatch.setattr(adapter, "GitHub", PreparedGitHub)

    def collect(snap, _api, retained_dir, **_kwargs):
        packet, _ref, proposal = caller.packet(snap, retained_dir)
        prepared_payload["proposal"] = proposal
        return packet

    monkeypatch.setattr(closer_evidence, "collect_evidence", collect)
    backend = closer(tmp_path, current)
    calls = []

    def unavailable(*_args, **_kwargs):
        raise OSError(errno.ENOSPC, "private fixture")

    monkeypatch.setattr(dispatcher, "_retain_provider_result", unavailable)

    def offload(agent, prompt, **kwargs):
        calls.append((agent, kwargs["profile_id"]))
        return caller.response(
            {"exit": 0, "output": json.dumps(prepared_payload["proposal"])}, agent, prompt, **kwargs
        )

    monkeypatch.setattr(dispatcher, "offload", offload)

    def pipeline_stub(_path, targets, **callbacks):
        name = targets[0]
        context = {"deadline": time.time() + 60}
        context["prepare"] = callbacks["prepare"](name, context)
        interpreted = callbacks["interpret"](name, context)
        assert interpreted["status"] == "uncertain", interpreted
        held = caller.results[0]
        assert interpreted["provider_outcome"]["actual"]["run_id"] == held["run_id"]
        assert held["process_started"] is True and held["outcome_uncertain"] is True
        assert held["error"] == "provider_result_retention_failed:OSError"
        assert held["provider_completed"] is True and held["raw_exit"] == 0
        assert held["retry_safe"] is False
        assert "substantive_needs" in interpreted
        return {"targets": {name: interpreted}}

    monkeypatch.setattr(closer_pipeline, "run_pipeline", pipeline_stub)
    args = SimpleNamespace(state=str(tmp_path / "pipeline-state.json"), max_workers=1, shadow=False)
    caller.staged(args, [target], backend, time.time() + 180)
    assert len(calls) == len(caller.results) == 1
    selected = execution_profiles.get_profile(calls[0][1])
    import closer_role_qualification as roles
    import closer_role_selection as selection

    assert calls[0][0] == selected["agent"] == "claude"
    assert roles.is_runtime_qualified(selected, roles.BOUNDED_CLASSIFICATION)
    assert selection.quality_scope(selected, roles.BOUNDED_CLASSIFICATION, "ordinary_static")
    observed = json.loads(controlled_world.capture.read_text())
    assert observed["argv"][observed["argv"].index("--model") + 1] == selected["requested_model"]
    assert observed["argv"][observed["argv"].index("--effort") + 1] == selected["reasoning_effort"]
    assert controlled_world.events["auth"] == controlled_world.events["auth_closed"] == 1
    assert not backend.gh.calls


def test_actual_inaccessible_claude_directory_is_named(tmp_path):
    workspace = tmp_path / "work"
    directory = adapters._claude_project_dir(workspace)
    directory.mkdir()
    directory.chmod(0)
    try:
        with pytest.raises(PermissionError):
            with os.scandir(directory):
                pass
        probe = adapters.cli_reported_model("claude", workspace)
        assert probe["model"] is None
        assert probe["reason"] == "claude_transcript_probe_oserror:EACCES"
    finally:
        directory.chmod(0o700)


def test_unreadable_newest_cannot_credit_older_model(tmp_path, monkeypatch):
    workspace = tmp_path / "work"
    directory = adapters._claude_project_dir(workspace)
    directory.mkdir()
    older = directory / "older.jsonl"
    newest = directory / "newest.jsonl"
    older.write_text(json.dumps({"model": "claude-older-real"}))
    newest.write_text(json.dumps({"model": "claude-new-real"}))
    os.utime(older, (100, 100))
    os.utime(newest, (200, 200))
    original = Path.open
    opened = []

    def unreadable(path, *args, **kwargs):
        opened.append(path)
        if path == newest:
            raise PermissionError(errno.EACCES, "post-sort private read denial")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", unreadable)
    probe = adapters.cli_reported_model("claude", workspace)
    assert probe["model"] is None
    assert probe["reason"] == "claude_transcript_probe_oserror:EACCES"
    assert newest in opened and older not in opened


def test_receipt_failure_blocks_actual_transient_retry(long_workspace, monkeypatch):
    error_text = "connection reset by peer"
    assert dispatcher._is_transient_network_failure("agent exited 1", "", error_text)
    writes = []

    def unavailable(*args, **kwargs):
        writes.append(1)
        raise OSError(errno.ENOSPC, "private fixture")

    monkeypatch.setattr(dispatcher, "_retain_provider_result", unavailable)

    def forbidden(*args, **kwargs):
        pytest.fail("no callback, completion enrichment or retry backoff after failed receipt")

    monkeypatch.setattr(adapters, "cli_reported_model", forbidden)
    monkeypatch.setattr(dispatcher.time, "sleep", forbidden)
    result, calls = _offload(
        monkeypatch, long_workspace, forbidden, retries=2, returncode=1, stderr=error_text
    )
    assert len(calls) == len(writes) == 1
    assert result["raw_exit"] == 1 and result["exit"] == 70
    assert result["outcome_status"] == "uncertain" and result["retry_safe"] is False
    assert _attempt(result["run_id"])[0] == "started"


@pytest.mark.parametrize("boundary", ["callback", "identity"])
def test_original_interrupt_survives_later_enrichment_error(long_workspace, monkeypatch, boundary):
    interruption = KeyboardInterrupt()
    callbacks = []

    def callback(event):
        callbacks.append(event)
        if boundary == "callback":
            raise RuntimeError("private callback error")

    if boundary == "identity":

        def broken(*args, **kwargs):
            raise RuntimeError("private identity error")

        monkeypatch.setattr(adapters, "cli_reported_model", broken)
    with pytest.raises(KeyboardInterrupt) as error:
        _offload(monkeypatch, long_workspace, callback, failure=interruption)
    assert error.value is interruption
    assert len(callbacks) == 1 and _receipt(callbacks[0])["exit"] == 130
    assert _attempt(callbacks[0]["run_id"])[0] == "started"


def test_cli_interrupt_preserves_attached_retention_identity(monkeypatch, capsys):
    interruption = KeyboardInterrupt()
    interruption.held_result = {
        "agent": "claude",
        "exit": 70,
        "raw_exit": 130,
        "output": "",
        "error": "provider_result_retention_failed:OSError",
        "run_id": "private-cancelled-run",
        "outcome_status": "uncertain",
        "outcome_uncertain": True,
        "retry_safe": False,
    }

    def cancelled(*args, **kwargs):
        raise interruption

    monkeypatch.setattr(dispatcher, "offload", cancelled)
    assert dispatcher.main(["offload", "--agent", "claude", "--prompt", "private"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["exit"] == 130
    assert result["run_id"] == "private-cancelled-run"
    assert result["held_result"] == interruption.held_result
    assert result["outcome_uncertain"] is True and result["retry_safe"] is False
