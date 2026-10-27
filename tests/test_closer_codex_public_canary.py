"""Source-selected public trial controls; no real native/provider calls."""

import copy
import hashlib
import json
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

import closer_codex_metadata as metadata
import closer_codex_public_canary as canary
import closer_correction_controller as ctl
import closer_frontier_policy as finite
import closer_queue as queue
import execution_profiles


def make_manifest(tmp_path):
    task = (tmp_path / "task.txt").resolve()
    task.write_text("Public synthetic CSV task; return JSON source only.")
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir(mode=0o700)
    profile = "codex-6.1-sol-high"
    policy = {
        "schema": canary.SCHEMA,
        "target": "public/startup#2",
        "task_path": str(task),
        "task_sha256": hashlib.sha256(task.read_bytes()).hexdigest(),
        "workspace_path": str(workspace),
        "profile_id": profile,
        "profile_sha256": ctl.source_digest(execution_profiles.get_profile(profile)),
        "max_runtime_seconds": 60,
        "max_captured_output_bytes": 131072,
        "max_retained_final_bytes": 12000,
        "max_outer_submissions": 1,
    }
    return {
        "policy": {
            "codex_initial_consumer": {"target": "public/startup#1"},
            canary.POLICY: policy,
            metadata.POLICY: {
                "binary_path": "/synthetic/no-native-call",
                "home": str(tmp_path),
                "codex_home": str(tmp_path),
                "private_root": str(tmp_path),
            },
        }
    }


def test_selected_task_and_binding_have_distinct_noncorrective_purpose(tmp_path):
    m = make_manifest(tmp_path)
    value = canary.operation_observation(m, "public/startup#2", "f" * 64)
    assert value["action"] == "disposition"
    assert value["evidence"] == {"purpose": canary.SCHEMA, "corrective_authority": False}
    assert canary.POLICY + "_sha256" in value["operation_binding"]
    assert "codex_initial_consumer_sha256" not in value["operation_binding"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_outer_submissions", 2),
        ("max_runtime_seconds", 121),
        ("max_retained_final_bytes", 131073),
        ("profile_sha256", "a" * 64),
        ("target", "public/startup#1"),
    ],
)
def test_public_trial_cannot_expand_its_source_budget_or_scope(tmp_path, field, value):
    m = make_manifest(tmp_path)
    m["policy"][canary.POLICY][field] = value
    with pytest.raises(ValueError):
        canary.selected_policy(m)


def test_fixed_task_change_and_alias_are_refused(tmp_path):
    m = make_manifest(tmp_path)
    policy = canary.selected_policy(m)
    assert canary._task(policy).startswith(b"Public")
    Path(policy["task_path"]).write_text("Different task")
    with pytest.raises(ValueError):
        canary._task(policy)
    p = Path(policy["task_path"])
    other = p.with_name("other.txt")
    p.rename(other)
    p.symlink_to(other)
    with pytest.raises(ValueError):
        canary.selected_policy(m)


def test_copied_operation_cannot_launch_public_trial(tmp_path):
    m = make_manifest(tmp_path)
    with pytest.raises(ValueError, match="original_public_test_callback_required"):
        canary.execute_original(
            object(), m, "f" * 64, "public/startup#2", "disposition", {}, {"id": "copied"}
        )


def test_distinct_real_queue_and_finite_reservation_precede_mocked_native_child(
    tmp_path, monkeypatch
):
    import closer_codex_metadata_admission as admitted
    import closer_resources as resources
    import dispatcher

    m = make_manifest(tmp_path)
    policy = m["policy"][canary.POLICY]
    target = policy["target"]
    observation = canary.operation_observation(m, target, "f" * 64)
    frontier = finite.make_frontier_from_observations(
        {target: observation}, owner="test-owner", frontier_id="public-source-test"
    )
    calls = []
    records = {}
    account = "a" * 64
    reference = {"name": "current-result.json", "sha256": "b" * 64, "execution_authorized": False}
    custody = SimpleNamespace(
        path=tmp_path,
        create=lambda n, v: records.setdefault(n, copy.deepcopy(v)) == v,
        read=lambda n: {"account_sha256": account} if n == reference["name"] else records[n],
    )
    scope = SimpleNamespace(
        custody=custody,
        spec=m["policy"][metadata.POLICY],
        deadline=time.monotonic() + 60,
        live=lambda: None,
        check_native=lambda: calls.append("fresh-source-check"),
    )

    @contextmanager
    def factory(binding, operation, *, purpose):
        assert purpose == canary.POLICY and queue._currently_invoking(target, operation)
        calls.append("actual-distinct-callback")
        yield scope

    monkeypatch.setattr(admitted, "metadata_scope", factory)
    monkeypatch.setattr(admitted, "qualify_admitted_callback", lambda _: reference)
    monkeypatch.setattr(admitted, "read_admitted_account", lambda s, r: object())
    monkeypatch.setattr(canary, "_previous_account", lambda _: account)

    # There is no real Codex child: invoke the actual source starting callback
    # while its real durable finite pool reservation and queue are held.
    def transport(command, **kwargs):
        assert resources.finite_pool_has_active_reservations()
        kwargs["starting_callback"](kwargs["observation"])
        assert resources.finite_pool_current_binding() == {
            "frontier_id": frontier["frontier_id"],
            "target": target,
        }
        calls.append("mock-native-after-guard")
        final = Path(command[command.index("--output-last-message") + 1])
        final.write_text(
            json.dumps(
                {"file": "escape_cell.py", "content": "def escape_cell(text):\n    return text\n"}
            )
        )
        resources.finite_pool_no_start(kwargs["observation"])
        return SimpleNamespace(
            returncode=0,
            stdout="mock completion",
            stderr="",
            observation={"cleanup_terminal": True},
        )

    monkeypatch.setattr(dispatcher, "run_observed_offload", transport)

    def execute(name, action, snapshot, operation):
        return canary.execute_original(object(), m, "f" * 64, name, action, snapshot, operation)

    queue.run_round(
        tmp_path / "public.json",
        [target],
        lambda _: copy.deepcopy(observation),
        execute,
        owner="test-owner",
        deadline=None,
        receipt_reserve_seconds=0,
        max_workers=1,
        max_passes=1,
        max_operation_retries=0,
        finite_frontier=frontier,
        request_timeout_seconds=120,
    )
    assert calls == ["actual-distinct-callback", "fresh-source-check", "mock-native-after-guard"]
    actual = json.loads((tmp_path / "public.json").read_text())["frontiers"][
        frontier["frontier_id"]
    ]["items"][target]["operation"]
    assert actual["status"] == "completed" and actual["result"]["corrective_authority"] is False
    retained = next(v for n, v in records.items() if n.startswith("public-test-result-"))
    assert (
        retained["code_executed_by_host"] is False
        and retained["semantic_correctness_tested"] is False
    )
    assert retained["same_process_account_proved"] is False
