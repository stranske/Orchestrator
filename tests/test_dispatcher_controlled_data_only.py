"""Actual inert CLI + existing observer/pool/retention; never a provider call."""

import contextlib
import hashlib
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import adapters
import closer_data_only_execution as execution
import closer_resources as resources
import dispatcher
import storage_lifecycle


@pytest.fixture
def world(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    owned = root / "owned"
    owned.mkdir()
    capture = root / "captured.json"
    cli = root / "inert-cli"
    events = {"auth": 0, "auth_closed": 0, "ledger": [], "started": [], "finished": []}
    token = "synthetic-borrowed-existing-token"

    def configure(mode="success"):
        script = f"""#!{sys.executable}
import json,os,sys,time
from pathlib import Path
text=sys.stdin.read()
environment={{k:v for k,v in os.environ.items() if k != 'CLAUDE_CODE_OAUTH_TOKEN'}}
Path({str(capture)!r}).write_text(json.dumps({{'argv':sys.argv,'cwd':os.getcwd(),'env':environment,'oauth_present':bool(os.environ.get('CLAUDE_CODE_OAUTH_TOKEN')),'stdin':text}}))
time.sleep(.04)
mode={mode!r}
if mode=='timeout': time.sleep(10)
if mode=='overflow': print('x'*70000);raise SystemExit(0)
if mode=='partial': print('{{"type":"result"');raise SystemExit(0)
model=sys.argv[sys.argv.index('--model')+1]
if mode=='wrongmodel':model='claude-opus-5-5'
final=json.dumps({{'classification':'relevant','authority':False,'evidence':[]}})
if mode=='finaloverflow':final='x'*9000
if mode=='leak':final=os.environ['CLAUDE_CODE_OAUTH_TOKEN']
row={{'type':'result','subtype':'success','is_error':False,'num_turns':1,'terminal_reason':'completed','stop_reason':'end_turn','session_id':'synthetic-session','result':final,'total_cost_usd':.01,'usage':{{'input_tokens':10,'output_tokens':20,'cache_read_input_tokens':1,'cache_creation_input_tokens':1}},'modelUsage':{{model:{{'inputTokens':10,'outputTokens':20,'cacheReadInputTokens':1,'cacheCreationInputTokens':1,'costUSD':.01}}}}}}
if mode=='local':row['num_turns']=0
if mode=='uncertain':row['outcome_status']='uncertain'
if mode=='multiturn':row['num_turns']=2
if mode=='resumed':row['resumed']=True
print(json.dumps(row))
"""
        cli.write_text(script)
        cli.chmod(0o700)
        sha = hashlib.sha256(cli.read_bytes()).hexdigest()
        monkeypatch.setattr(adapters, "DATA_ONLY_CLAUDE_SHA256", sha)
        monkeypatch.setattr(execution, "CLAUDE_BINARY_SHA256", sha)

    configure()
    real_which = adapters.shutil.which
    monkeypatch.setattr(
        adapters.shutil, "which", lambda name: str(cli) if name == "claude" else real_which(name)
    )
    monkeypatch.setattr(storage_lifecycle, "owned_agent_tmpdir", lambda *_: owned)
    monkeypatch.setattr(dispatcher, "DISPATCH_LOG_DIR", root / "logs")
    monkeypatch.setattr(dispatcher, "_capability_heartbeat", lambda *a, **k: None)
    monkeypatch.setattr(adapters, "record_ledger", lambda *a, **k: events["ledger"].append(k))
    monkeypatch.setattr(adapters, "can_report_cli_identity", lambda *_: (True, None))
    for name in (
        "record_run",
        "record_execution_attempt",
        "complete_profile_attempt",
        "complete_profile_attempt_unresolved",
        "record_cost",
    ):
        monkeypatch.setattr(dispatcher.feedback, name, lambda *a, **k: None)

    def forbidden(*_a, **_k):
        pytest.fail("controlled route touched legacy workspace/auth/shell/model-store path")

    for name in (
        "_path_prefix",
        "_net_hygiene_prelude",
        "_agent_runtime_prelude",
        "_auth_prelude",
        "_offload_prompt",
        "_isolate_offload_cwd",
    ):
        monkeypatch.setattr(dispatcher, name, forbidden)
    monkeypatch.setattr(adapters, "agent_workspace", forbidden)
    monkeypatch.setattr(adapters, "cli_reported_model", forbidden)
    monkeypatch.setenv("ORCH_MODEL_PROBE", "0")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ambient-must-not-reach-child")
    monkeypatch.setenv("CLAUDE_CODE_MAX_RETRIES", "99")
    limits = {
        "schema": execution.LIMITS_SCHEMA,
        "role": "bounded_classification",
        "model_contract_sha256": "f" * 64,
        "max_prompt_bytes": 65536,
        "max_final_bytes": 8192,
        "max_native_result_bytes": 65536,
        "max_output_tokens": 2048,
        "max_turns": 1,
        "timeout_seconds": 45,
    }

    @contextlib.contextmanager
    def supplier():
        events["auth"] += 1
        try:
            yield token
        finally:
            events["auth_closed"] += 1

    def started(observation):
        events["started"].append(dict(observation))
        resources.finite_pool_process_started(observation)

    def finished(observation):
        events["finished"].append(dict(observation))
        resources.finite_pool_process_finished(observation)

    args = {
        "agent": "claude",
        "prompt": "Bounded complete input.\n",
        "cwd": str(root),
        "mode": "assess",
        "isolate": True,
        "profile_id": "claude-sonnet-5-5-low",
        "task_type": "closer_interpret",
        "subject_id": "offline/repo#1",
        "data_only": True,
        "data_only_limits": limits,
        "trusted_auth_supplier": supplier,
        "data_only_operation_id": "original-operation",
        "finite_frontier_id": "original-frontier",
        "finite_frontier_target": "offline/repo#1",
        "process_starting_callback": resources.finite_pool_prepare_request,
        "process_started_callback": started,
        "process_finished_callback": finished,
    }

    @contextlib.contextmanager
    def lease():
        reservation = {
            "reservation_id": "original-reservation",
            "target": "offline/repo#1",
            "frontier_id": "original-frontier",
            "agent": "claude",
            "profile_id": "claude-sonnet-5-5-low",
        }
        with resources.pool_slot(
            root / "pools",
            hashlib.sha256(b"claude-subscription").hexdigest(),
            1,
            deadline=time.time() + 2,
            finite_frontier=True,
            reservation=reservation,
        ):
            yield

    return SimpleNamespace(
        root=root,
        cli=cli,
        capture=capture,
        events=events,
        token=token,
        configure=configure,
        args=args,
        lease=lease,
    )


def run(world, **changes):
    with world.lease():
        return dispatcher.offload(**{**world.args, **changes})


def test_actual_direct_cli_isolated_controls_origin_and_existing_accounting(world):
    result = run(world)
    assert result["exit"] == 0, json.dumps(result, indent=2)
    final = json.loads(result["output"])
    assert final["classification"] == "relevant" and "type" not in final
    got = json.loads(world.capture.read_text())
    assert got["argv"][0] == str(world.cli)
    assert got["argv"][-4:] == ["--output-format", "json", "--max-turns", "1"]
    assert got["stdin"] == world.args["prompt"] and world.args["prompt"] not in got["argv"]
    assert got["oauth_present"] is True
    assert "ANTHROPIC_API_KEY" not in got["env"]
    assert got["env"]["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
    assert got["env"]["CLAUDE_CODE_MAX_RETRIES"] == "0"
    assert got["env"]["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "2048"
    assert world.events["auth"] == world.events["auth_closed"] == 1
    assert len(world.events["started"]) == len(world.events["finished"]) == 1
    assert [row["event"] for row in world.events["ledger"]] == ["start", "complete"]
    origin = result["data_only_origin"]
    assert origin["operation_id"] == "original-operation" and origin["authority"] is False
    assert origin["classification_validated"] is False
    assert origin["process_observation"]["cleanup_terminal"] is True
    assert origin["final"]["sha256"] == hashlib.sha256(result["output"].encode()).hexdigest()
    assert origin["normalized_native_result"]["observed"]["effort"] == "UNKNOWN"
    assert origin["normalized_native_result"]["usage"]["pool_charge"] == "UNKNOWN"
    receipts = list((world.root / "logs").glob("*.provider-result-*.json"))
    assert len(receipts) == 1
    retained = json.loads(receipts[0].read_text())
    assert retained["result"]["data_only_origin"] == origin
    assert world.token not in json.dumps(retained)
    assert not Path(got["cwd"]).exists()


@pytest.mark.parametrize(
    "field",
    [
        "data_only_limits",
        "trusted_auth_supplier",
        "data_only_operation_id",
        "profile_id",
        "process_started_callback",
        "process_starting_callback",
        "process_finished_callback",
    ],
)
def test_missing_controlled_admission_is_known_not_started(world, field):
    result = run(world, **{field: None})
    assert result["exit"] == 2 and result["run_id"] is None
    assert result["process_started"] is False and result["outcome_uncertain"] is False
    assert not world.events["ledger"] and not world.capture.exists() and world.events["auth"] == 0


def test_actual_original_pool_reservation_is_mandatory(world):
    result = dispatcher.offload(**world.args)
    assert result["exit"] == 2 and result["process_started"] is False
    assert world.events["auth"] == 0


@pytest.mark.parametrize(
    "mode",
    [
        "partial",
        "wrongmodel",
        "local",
        "uncertain",
        "multiturn",
        "resumed",
        "finaloverflow",
        "leak",
        "overflow",
    ],
)
def test_native_completion_identity_protocol_and_bounds_never_fall_back(world, mode):
    world.configure(mode)
    result = run(world)
    assert result["exit"] == 70 and result["outcome_uncertain"] is True, result
    assert result["output"] == "" and result["retry_safe"] is False
    assert result["attempts"] == 1
    assert world.events["auth"] == world.events["auth_closed"] == 1
    assert world.token not in json.dumps(result)


def test_timeout_keeps_original_observed_run_and_never_retries(world):
    world.configure("timeout")
    result = run(world, timeout=0.2)
    assert result["exit"] == 124 and result["outcome_uncertain"] is True
    assert result["attempts"] == 1 and result["process_started"] is True
    assert result["process_observation"]["run_id"] == result["run_id"]


def test_legacy_generic_route_is_not_silently_controlled(world, monkeypatch):
    original = dispatcher.offload.__wrapped__
    calls = []

    def legacy(*args, **kwargs):
        calls.append((args, kwargs))
        return {"legacy": True}

    # Exercise the real offload signature binding while retaining a harmless target.
    legacy.__signature__ = __import__("inspect").signature(original)
    wrapped = dispatcher._controlled_data_only_entry(legacy)
    assert wrapped("claude", "normal assess", mode="assess") == {"legacy": True}
    assert calls == [(("claude", "normal assess"), {"mode": "assess"})]


def test_wrong_shared_account_lane_cannot_borrow_or_spawn(world):
    with resources.pool_slot(
        world.root / "pools",
        hashlib.sha256(b"gemini-subscription").hexdigest(),
        1,
        deadline=time.time() + 2,
        finite_frontier=True,
        reservation={
            "reservation_id": "wrong-account",
            "target": "offline/repo#1",
            "frontier_id": "original-frontier",
            "agent": "claude",
            "profile_id": "claude-sonnet-5-5-low",
        },
    ):
        result = dispatcher.offload(**world.args)
    assert result["process_started"] is False and result["outcome_uncertain"] is False
    assert world.events["auth"] == 0 and not world.events["ledger"]
    assert not world.capture.exists()


def test_complete_prompt_over_explicit_budget_never_borrows_or_allocates(world):
    result = run(world, prompt="😀" * 16385)
    assert result["exit"] == 2 and result["run_id"] is None
    assert result["process_started"] is False and result["outcome_uncertain"] is False
    assert world.events["auth"] == 0 and not world.events["ledger"]
    assert not world.capture.exists()


@pytest.mark.parametrize("source", ["dispatcher.py", "transport_saved_result.py"])
def test_actual_observed_source_drift_refuses_completed_final(world, monkeypatch, source):
    original = dispatcher._controlled_module_sha
    reads = {}

    def changed(module):
        name = Path(module.__file__).name
        reads[name] = reads.get(name, 0) + 1
        digest = original(module)
        return "0" * 64 if name == source and reads[name] > 1 else digest

    monkeypatch.setattr(dispatcher, "_controlled_module_sha", changed)
    result = run(world)
    assert world.capture.exists() and result["process_started"] is True
    assert result["exit"] == 70 and result["output"] == ""
    assert result["outcome_uncertain"] is True and result["retry_safe"] is False
    assert result["attempts"] == 1


@pytest.mark.parametrize("mode", ["success", "overflow", "timeout"])
def test_controlled_cleanup_never_reopens_unbounded_communicate(world, monkeypatch, mode):
    world.configure(mode)
    original = dispatcher.subprocess.Popen

    def spawn(command, *args, **kwargs):
        child = original(command, *args, **kwargs)
        if command[0] == str(world.cli):

            def forbidden(*_args, **_kwargs):
                pytest.fail("controlled child entered unbounded communicate")

            child.communicate = forbidden
        return child

    monkeypatch.setattr(dispatcher.subprocess, "Popen", spawn)
    result = run(world, timeout=0.2 if mode == "timeout" else 2)
    assert result["exit"] == {"success": 0, "overflow": 70, "timeout": 124}[mode]
    assert result["attempts"] == 1


def test_disabled_never_allocates_runtime_or_borrows_auth(world, monkeypatch):
    monkeypatch.setenv("ORCH_OFFLOAD_DISABLED", "1")
    result = run(world)
    assert result["disabled"] is True and result["process_started"] is False
    assert world.events["auth"] == 0 and not world.events["ledger"]
    assert list((world.root / "owned").iterdir()) == []


def test_expired_original_deadline_never_allocates_runtime_or_borrows(world):
    result = run(world, request_deadline_mono=time.monotonic() - 1)
    assert result["process_started"] is False and result["outcome_uncertain"] is False
    assert world.events["auth"] == 0 and not world.events["ledger"]
    assert list((world.root / "owned").iterdir()) == []


def test_known_result_survives_private_cleanup_refusal_and_cannot_promote(world, monkeypatch):
    def denied(*_args, **_kwargs):
        raise PermissionError("synthetic owned cleanup refusal")

    monkeypatch.setattr(dispatcher.shutil, "rmtree", denied)
    result = run(world)
    assert result["exit"] == 0 and json.loads(result["output"])["authority"] is False
    assert result["semantic_promotion_blocked"] is True and result["retry_safe"] is False
    assert result["data_only_private_runtime"]["cleanup"] == "held_owned_cleanup_unverified"
    assert [row["event"] for row in world.events["ledger"]] == ["start", "complete"]
    reference = result["provider_result_ref"]
    raw = Path(reference["path"]).read_bytes()
    assert reference["sha256"] == hashlib.sha256(raw).hexdigest()
    assert reference["bytes"] == len(raw)
    retained = json.loads(raw)
    assert retained["result"]["output"] == result["output"]
    assert retained["result"]["data_only_origin"] == result["data_only_origin"]
    assert retained["result"]["semantic_promotion_blocked"] is True
    assert retained["result"]["data_only_private_runtime"] == result["data_only_private_runtime"]
    assert "provider_result_ref" not in retained["result"]


def test_borrowed_token_local_cleared_from_exceptional_decorator_frame(world):
    original = dispatcher.offload.__wrapped__

    def broken(*args, **kwargs):
        raise RuntimeError("synthetic pre-provider body failure")

    broken.__signature__ = __import__("inspect").signature(original)
    wrapped = dispatcher._controlled_data_only_entry(broken)
    with world.lease(), pytest.raises(RuntimeError) as caught:
        wrapped(**world.args)
    tb = caught.value.__traceback__
    seen = False
    while tb:
        if tb.tb_frame.f_code.co_name == "invoke":
            seen = True
            assert tb.tb_frame.f_locals["token"] == ""
            context = tb.tb_frame.f_locals["context"]
            assert context.token == "" and context.env == {}
        tb = tb.tb_next
    assert seen and world.events["auth_closed"] == 1


def test_misplaced_borrowed_token_rejected_before_any_prompt_hash(world, monkeypatch):
    original = dispatcher.hashlib.sha256
    seen = []

    def digest(data=b"", *args, **kwargs):
        assert data != world.token.encode(), "borrowed token was hashed"
        seen.append(data)
        return original(data, *args, **kwargs)

    monkeypatch.setattr(dispatcher.hashlib, "sha256", digest)
    result = run(world, prompt=world.token)
    assert result["process_started"] is False and result["outcome_uncertain"] is False
    assert world.events["auth"] == world.events["auth_closed"] == 1
    assert not world.events["ledger"] and not world.capture.exists()
