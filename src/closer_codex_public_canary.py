"""One source-selected public coding trial; no corrective/index/ACT authority."""

from __future__ import annotations

import ast
import copy
import hashlib
import os
import stat
import time
from pathlib import Path

import closer_codex_metadata as metadata
import closer_correction_controller as ctl

POLICY = "codex_public_coding_test"
SCHEMA = "closer-public-coding-canary/v1"
_SOURCE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def need(ok, reason):
    if not ok:
        raise ValueError(reason)


def selected_policy(manifest):
    value = (manifest.get("policy") or {}).get(POLICY)
    if value is None:
        return None
    fields = {
        "schema",
        "target",
        "task_path",
        "task_sha256",
        "workspace_path",
        "profile_id",
        "profile_sha256",
        "max_runtime_seconds",
        "max_captured_output_bytes",
        "max_retained_final_bytes",
        "max_outer_submissions",
    }
    need(
        type(value) is dict and set(value) == fields and value["schema"] == SCHEMA,
        "public_test_policy_fields",
    )
    import execution_profiles

    profile = execution_profiles.get_profile(value["profile_id"])
    need(
        profile is not None
        and profile["agent"] == "codex"
        and profile["profile_id"] == "codex-6.1-sol-high"
        and value["profile_sha256"] == ctl.source_digest(profile),
        "public_test_profile",
    )
    import closer_resources

    need(
        closer_resources.canonical_target(value["target"]) == value["target"], "public_test_target"
    )
    need(
        value["target"] != manifest["policy"]["codex_initial_consumer"]["target"],
        "public_test_distinct_purpose_target",
    )
    need(
        type(value["max_runtime_seconds"]) is int
        and 1 <= value["max_runtime_seconds"] <= 60
        and type(value["max_captured_output_bytes"]) is int
        and 0 < value["max_captured_output_bytes"] <= 131072
        and type(value["max_retained_final_bytes"]) is int
        and 0 < value["max_retained_final_bytes"] <= 12000
        and type(value["max_outer_submissions"]) is int
        and value["max_outer_submissions"] == 1,
        "public_test_bounds",
    )
    for field in ("task_sha256", "profile_sha256"):
        need(
            type(value[field]) is str
            and len(value[field]) == 64
            and all(c in "0123456789abcdef" for c in value[field]),
            "public_test_digest",
        )
    for field in ("task_path", "workspace_path"):
        p = Path(value[field])
        need(
            p.is_absolute()
            and str(p.resolve()) == str(p)
            and not any(x.is_symlink() for x in (p, *p.parents)),
            "public_test_physical_path",
        )
    return copy.deepcopy(value)


def operation_observation(manifest, target, manifest_sha256):
    policy = selected_policy(manifest)
    need(policy is not None and target == policy["target"], "public_test_not_selected")
    spec = manifest["policy"][metadata.POLICY]
    return {
        "target": target,
        "action": "disposition",
        "eligible": True,
        "revision": "public-coding-test:" + ctl.digest(policy),
        "operation_binding": {
            "runtime_manifest_sha256": manifest_sha256,
            metadata.POLICY + "_sha256": ctl.digest(spec),
            POLICY + "_sha256": ctl.digest(policy),
        },
        "reason": "source-selected public test task, never corrective action",
        "next_trigger": None,
        "evidence": {"purpose": SCHEMA, "corrective_authority": False},
    }


def _task(policy):
    p = Path(policy["task_path"])
    fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        need(
            stat.S_ISREG(before.st_mode)
            and before.st_uid == os.getuid()
            and before.st_nlink == 1
            and 0 < before.st_size <= 65536
            and not before.st_mode & 0o022,
            "public_task_file",
        )
        raw = stream.read(65537)
        after = os.fstat(stream.fileno())
        need(
            (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            and len(raw) == before.st_size
            and hashlib.sha256(raw).hexdigest() == policy["task_sha256"],
            "public_task_changed",
        )
        raw.decode("utf-8")
        return raw


def _previous_account(scope):
    import closer_codex_metadata_admission as admitted
    import closer_codex_registration as registration

    saved, _ = admitted._file(str(scope.state_path) + ".codex-metadata-queue.json")
    target = scope.manifest["policy"]["codex_initial_consumer"]["target"]
    row = saved["frontiers"][scope.admission["finite_frontier"]["frontier_id"]]["items"][target][
        "operation"
    ]
    expected = registration.operation_observation(scope.manifest, target, scope.origin.sha256)
    need(
        row["action"] == "disposition"
        and row["status"] == "completed"
        and row["attempts"] == 1
        and row["binding"] == expected["operation_binding"],
        "prior_metadata_purpose_not_completed",
    )
    reference = row["result"]["metadata_reference"]
    key = ctl.digest(
        {
            "schema": metadata.PURPOSE,
            "operation_id": row["id"],
            "manifest_sha256": scope.origin.sha256,
            "selection_sha256": ctl.digest(scope.spec),
        }
    )
    need(reference["name"] == "result-" + key + ".json", "prior_metadata_reference_changed")
    result = scope.custody.read(reference["name"])
    intent = scope.custody.read("intent-" + key + ".json")
    need(
        ctl.digest(result) == reference["sha256"]
        and reference["execution_authorized"] is False
        and result["guard_source_sha256"] == admitted._SOURCE_SHA256
        and result["codec_source_sha256"] == metadata._SOURCE_SHA256
        and intent["operation_id"] == row["id"]
        and result["operation_id"] == row["id"]
        and result["manifest_sha256"] == scope.origin.sha256
        and result["selection_sha256"] == ctl.digest(scope.spec)
        and result["execution_authorized"] is False
        and result["registration_installed"] is False
        and type(result["thread_starts"]) is int
        and result["thread_starts"] == 0
        and type(result["turn_starts"]) is int
        and result["turn_starts"] == 0,
        "prior_metadata_scope_changed",
    )
    return result["account_sha256"]


def run_from_native_admission(binding, manifest, manifest_sha256, admission, campaign, document):
    """Distinct purpose and physical queue after the zero-turn metadata round."""
    import closer_frontier_policy as finite
    import closer_queue as queue

    policy = selected_policy(manifest)
    if policy is None:
        return None
    target = policy["target"]
    observed = operation_observation(manifest, target, manifest_sha256)
    frontier = admission["finite_frontier"]
    need(
        frontier["bindings"][target]
        == {
            "revision": observed["revision"],
            "action_id": finite.action_identity(
                target, observed["revision"], "disposition", observed["operation_binding"]
            ),
        },
        "public_test_frontier_changed",
    )
    completed = None

    def observe(name):
        need(name == target, "public_test_target_changed")
        if completed is not None:
            return {
                **copy.deepcopy(observed),
                "action": "complete",
                "eligible": False,
                "reason": "public test purpose completed without production authority",
            }
        return copy.deepcopy(observed)

    def execute(name, action, snapshot, operation):
        nonlocal completed
        completed = execute_original(
            binding, manifest, manifest_sha256, name, action, snapshot, operation
        )
        return completed

    receipt = queue.run_round(
        str(campaign.path) + ".codex-public-canary-queue.json",
        [target],
        observe,
        execute,
        owner=document["owner"],
        deadline=None,
        receipt_reserve_seconds=0,
        max_workers=1,
        max_passes=1,
        max_operation_retries=0,
        finite_frontier=frontier,
        frontier_target=target,
        request_timeout_seconds=min(120.0, admission["request_timeout_seconds"]),
    )
    return {"purpose": SCHEMA, "queue": receipt, "result": completed, "corrective_authority": False}


def execute_original(binding, manifest, manifest_sha256, target, action, observation, operation):
    import adapters
    import closer_codex_metadata_admission as admitted
    import closer_queue as queue
    import closer_resources as resources
    import dispatcher
    import execution_profiles

    need(
        action == "disposition" and queue._currently_invoking(target, operation),
        "original_public_test_callback_required",
    )
    policy = selected_policy(manifest)
    expected = operation_observation(manifest, target, manifest_sha256)
    need(
        operation["binding"] == expected["operation_binding"]
        and observation["revision"] == expected["revision"],
        "original_public_test_binding_changed",
    )
    with admitted.metadata_scope(binding, operation, purpose=POLICY) as scope:
        prior_account = _previous_account(scope)
        ref = admitted.qualify_admitted_callback(scope)
        current = scope.custody.read(ref["name"])
        need(current["account_sha256"] == prior_account, "public_test_account_changed")
        task = _task(policy)
        workspace = Path(policy["workspace_path"])
        meta = workspace.stat(follow_symlinks=False)
        need(
            stat.S_ISDIR(meta.st_mode)
            and meta.st_uid == os.getuid()
            and stat.S_IMODE(meta.st_mode) == 0o700,
            "public_workspace_not_private",
        )
        profile = execution_profiles.get_profile(policy["profile_id"])
        binary = scope.spec["binary_path"]
        final = workspace / "public-canary-final.txt"
        need(not final.exists() and not final.is_symlink(), "public_test_prior_output")
        command = adapters.build_command(
            "codex",
            "-",
            cwd=str(workspace),
            profile=policy["profile_id"],
            transport="offload",
            permission_mode="read-only",
            prompt_via_stdin=True,
            out_file=str(final),
        )
        command[0] = binary
        from closer_codex_native_confinement import _TOOL_OVERRIDES
        from closer_codex_owned_stdio import _FIXED_OVERRIDES

        global_args = []
        for override in _FIXED_OVERRIDES + _TOOL_OVERRIDES:
            global_args.extend(("-c", override))
        command = [binary, *global_args, *command[1:]]
        environment = metadata._environment(scope.spec)
        run_id = "public-csv-" + operation["id"]
        details = {
            "run_id": run_id,
            "agent": "codex",
            "profile_id": profile["profile_id"],
            "finite_frontier_id": operation["finite_frontier"]["frontier_id"],
            "finite_frontier_target": target,
        }
        intent = {
            "schema": SCHEMA,
            "operation_id": operation["id"],
            "task_sha256": policy["task_sha256"],
            "policy_sha256": ctl.digest(policy),
            "account_sha256": prior_account,
            "execution_authorized": False,
        }
        need(
            scope.custody.create("public-trial-intent-" + operation["id"] + ".json", intent),
            "public_trial_no_retry",
        )

        def starting(observed):
            scope.check_native()
            need(_task(policy) == task, "public_task_changed")
            fresh = admitted.read_admitted_account(scope, ref)
            # The native account is re-read above in this distinct original test callback.
            need(
                scope.custody.read(ref["name"])["account_sha256"] == prior_account,
                "public_test_account_changed",
            )
            need(
                fresh is not None
                and observed is details
                and queue._currently_invoking(target, operation),
                "public_test_original_starting_callback",
            )
            resources.finite_pool_prepare_request(observed)

        timeout = min(policy["max_runtime_seconds"], scope.deadline - time.monotonic() - 2)
        need(timeout > 0, "public_test_window_exhausted")
        reservation = {
            "reservation_id": run_id,
            "target": target,
            "frontier_id": details["finite_frontier_id"],
            "agent": "codex",
            "profile_id": profile["profile_id"],
        }
        with resources.pool_slot(
            scope.custody.path,
            "public-codex-test",
            1,
            deadline=operation["deadline"],
            finite_frontier=True,
            reservation=reservation,
        ), Path(policy["task_path"]).open("rb") as stdin:
            result = dispatcher.run_observed_offload(
                command,
                callback=resources.finite_pool_process_started,
                observation=details,
                cwd=str(workspace),
                timeout=timeout,
                stdin=stdin,
                finished_callback=resources.finite_pool_process_finished,
                starting_callback=starting,
                env=environment,
                output_limit=policy["max_captured_output_bytes"],
            )
        scope.live()
        fd = os.open(final, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            need(
                stat.S_ISREG(info.st_mode)
                and info.st_uid == os.getuid()
                and info.st_nlink == 1
                and 0 < info.st_size <= policy["max_retained_final_bytes"],
                "public_test_final_file_invalid",
            )
            raw = stream.read(policy["max_retained_final_bytes"] + 1)
        need(
            result.returncode == 0 and 0 < len(raw) <= policy["max_retained_final_bytes"],
            "public_test_terminal_incomplete",
        )
        from closer_model_contracts import strict_response_json

        answer = strict_response_json(raw.decode("utf-8"))
        need(
            type(answer) is dict
            and set(answer) == {"file", "content"}
            and answer["file"] == "escape_cell.py"
            and type(answer["content"]) is str,
            "public_test_output_contract",
        )
        parsed = ast.parse(answer["content"])
        need(
            any(isinstance(n, ast.FunctionDef) and n.name == "escape_cell" for n in parsed.body),
            "public_test_code_missing",
        )
        # Returned code is DATA; never import/evaluate untrusted model Python on this host.
        record = {
            **intent,
            "schema": "closer-public-coding-test-result/v1",
            "exit_code": result.returncode,
            "final_sha256": hashlib.sha256(raw).hexdigest(),
            "final_utf8": raw.decode(),
            "stdout_sha256": hashlib.sha256(result.stdout.encode()).hexdigest(),
            "stderr_sha256": hashlib.sha256(result.stderr.encode()).hexdigest(),
            "observation": result.observation,
            "code_returned": True,
            "code_executed_by_host": False,
            "semantic_correctness_tested": False,
            "corrective_owned_stdio_proved": False,
            "same_process_account_proved": False,
            "closure_authorized": False,
            "publication_authorized": False,
        }
        name = "public-test-result-" + operation["id"] + ".json"
        need(scope.custody.create(name, record), "public_test_result_exists")
        return {
            "status": "completed",
            "purpose": SCHEMA,
            "reference": {"name": name, "sha256": ctl.digest(record)},
            "code_returned": True,
            "target_complete": False,
            "corrective_authority": False,
        }
