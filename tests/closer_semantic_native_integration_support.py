"""Explicit native0953 + semantic-v2 test baseline; historical data stays intact."""

import ast
import copy
import hashlib
import json
from pathlib import Path

from closer_semantic_audit_support import fingerprint, reference, source_path

MANIFEST = Path(__file__).parent / "fixtures/semantic_api_reference/native_0953_guard_audits.json"
MANIFEST_SHA256 = "e112f9f7152621f13470e8da11950aa9b29e000ee4bcba846ce3c1701ca995b3"


def native_reference():
    raw = MANIFEST.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == MANIFEST_SHA256
    return json.loads(raw)


CURRENT_MANIFEST = (
    Path(__file__).parent / "fixtures/semantic_api_reference/current_core_role_receiver_v1.json"
)
CURRENT_MANIFEST_SHA256 = "32d0a361e2e942fc262ef5847583c53d954d74a2bbdfe6a665c3a819b770035a"


def current_receiver_reference():
    raw = CURRENT_MANIFEST.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == CURRENT_MANIFEST_SHA256
    current = json.loads(raw)
    reviewed = main982_admission_reference()
    for name, change in reviewed["function_delta"].items():
        assert current["top_level_ast"].get(name) == change["before"]
        current["top_level_ast"][name] = change["after"]
        # Preserve every original native entry and bind only this reviewed successor.
        if name in current["native_top_level_ast"]:
            assert current["native_top_level_ast"][name] == change["before"]
        current["native_top_level_ast"][name] = change["after"]
    initial = initial_b_observability_reference()["files"]["closer_dispatcher.py"]
    main = initial["function_delta"]["main"]
    assert current["top_level_ast"]["main"] == main["before"]
    assert current["native_top_level_ast"]["main"] == main["before"]
    current["top_level_ast"]["main"] = main["after"]
    current["native_top_level_ast"]["main"] = main["after"]
    stale_root = validate_stop_stale_scoped_root_reference()["files"]["closer_dispatcher.py"]
    stale_main = stale_root["function_delta"]["main"]
    assert current["top_level_ast"]["main"] == stale_main["before"]
    assert current["native_top_level_ast"]["main"] == stale_main["before"]
    current["top_level_ast"]["main"] = stale_main["after"]
    current["native_top_level_ast"]["main"] = stale_main["after"]
    return current


MAIN982_MANIFEST = (
    Path(__file__).parent / "fixtures/semantic_api_reference/current_main982_admission_v1.json"
)
MAIN982_SHA256 = "2a12c158b5ce3cf7e90153e2e2e2c0eff668e30a7ac39e558bc124cf69624580"


def main982_admission_reference():
    raw = MAIN982_MANIFEST.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == MAIN982_SHA256
    reviewed = json.loads(raw)
    assert set(reviewed["function_delta"]) == {
        "_deadline_with_pipeline_round",
        "_main_admitted",
        "pipeline_admission_pass_number",
    }
    assert set(reviewed["added_constant_ast"]) == {
        "PIPELINE_ADMISSION_PASS_CHECK_TS",
        "PIPELINE_ADMISSION_PASS_CHECK_LABEL",
    }
    tree = ast.parse(source_path("closer_dispatcher.py").read_bytes())
    for name, expected in reviewed["added_constant_ast"].items():
        assignments = [
            node
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == name for target in node.targets)
        ]
        assert len(assignments) == 1 and fingerprint(assignments[0]) == expected
    return reviewed


def integrated_reference(base):
    """Preserve historical data, then compose the explicitly reviewed current receiver."""
    original = copy.deepcopy(reference(base))
    current = current_receiver_reference()
    dispatcher = original["files"].get("closer_dispatcher.py")
    if dispatcher is not None:
        dispatcher["top_level_ast"] = current["top_level_ast"]
        dispatcher["unreserved_pipeline_ast_sha256"] = current["unreserved_pipeline_ast_sha256"]
        dispatcher["nested_pipeline_ast"] = copy.deepcopy(current["nested_pipeline_ast"])
        # A separate pinned prospective audit covers ONLY the now unsupported
        # optional-audit job/cache changes; original7488 data stays immutable.
        deferred = audit_deferral_reference()
        dispatcher["nested_pipeline_ast"]["prepare"] = deferred["after_prepare_ast_sha256"]
        dispatcher["unreserved_pipeline_ast_sha256"] = deferred[
            "after_unreserved_pipeline_ast_sha256"
        ]
        # Separate pinned prospective omission of an unsupported advisory plan;
        # retain every historical fixture and all other AST families unchanged.
        omission = advisory_plan_omission_reference()["dispatcher"]
        assert dispatcher["nested_pipeline_ast"]["prepare"] == omission["before_prepare_ast_sha256"]
        assert (
            dispatcher["unreserved_pipeline_ast_sha256"] == omission["before_unreserved_ast_sha256"]
        )
        dispatcher["nested_pipeline_ast"]["prepare"] = omission["after_prepare_ast_sha256"]
        dispatcher["unreserved_pipeline_ast_sha256"] = omission["after_unreserved_ast_sha256"]
        bounded = finite_prepare_reference()["dispatcher"]
        assert dispatcher["nested_pipeline_ast"]["prepare"] == bounded["before_prepare_ast_sha256"]
        assert (
            dispatcher["unreserved_pipeline_ast_sha256"] == bounded["before_unreserved_ast_sha256"]
        )
        dispatcher["nested_pipeline_ast"]["prepare"] = bounded["after_prepare_ast_sha256"]
        dispatcher["unreserved_pipeline_ast_sha256"] = bounded["after_unreserved_ast_sha256"]
        # Overlay only separately reviewed current-source AST families.
        # Historical source maps remain byte-identical and every other family
        # still compares against the prior receiver and optional-audit pins.
        for name, change in publication_admission_reference()["dispatcher_top_level_delta"].items():
            assert dispatcher["top_level_ast"].get(name) == change["before"]
            dispatcher["top_level_ast"][name] = change["after"]
        for name, change in source_admission_reference()["dispatcher_function_delta"].items():
            assert dispatcher["top_level_ast"].get(name) == change["before"]
            dispatcher["top_level_ast"][name] = change["after"]
        identity = repository_identity_reference()["dispatcher"]
        assert dispatcher["top_level_ast"]["Closer"] == identity["before_closer_ast_sha256"]
        dispatcher["top_level_ast"]["Closer"] = identity["after_closer_ast_sha256"]
        capacity = scoped_capacity_reference()["dispatcher"]
        for changed in (capacity["changed_function"], capacity["validation_function"]):
            assert dispatcher["top_level_ast"][changed["name"]] == changed["before_ast_sha256"]
            dispatcher["top_level_ast"][changed["name"]] = changed["after_ast_sha256"]
        added = capacity["added_function"]
        assert added["name"] not in dispatcher["top_level_ast"]
        dispatcher["top_level_ast"][added["name"]] = added["ast_sha256"]
    return original


AUDIT_DEFERRAL_MANIFEST = (
    Path(__file__).parent / "fixtures/semantic_api_reference/optional_audit_deferral_v1.json"
)
AUDIT_DEFERRAL_SHA256 = "6fa689b6f2677979ef29483bed8d4c7545f0fec97000ad1ce9d5fa1b69d6afc1"


def audit_deferral_reference():
    raw = AUDIT_DEFERRAL_MANIFEST.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == AUDIT_DEFERRAL_SHA256
    return json.loads(raw)


PUBLICATION_ADMISSION_MANIFEST = (
    Path(__file__).parent / "fixtures/semantic_api_reference/current_publication_admission_v1.json"
)
PUBLICATION_ADMISSION_SHA256 = "695eec7282d482e43ebbefc70f3439313cd98d328796901bb565736cf4978deb"


def publication_admission_reference():
    raw = PUBLICATION_ADMISSION_MANIFEST.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == PUBLICATION_ADMISSION_SHA256
    value = json.loads(raw)
    change = controlled_initialization_reference()["files"]["closer_campaign.py"]
    campaign = value["protected_native_modules"]["closer_campaign.py"]
    assert campaign["after_file_sha256"] == change["before_file_sha256"]
    campaign["after_file_sha256"] = change["after_file_sha256"]
    assert (
        campaign["before_top_level_ast"]["Campaign"] == change["before_top_level_ast"]["Campaign"]
    )
    campaign["changed_top_level_ast"]["Campaign"] = {
        "before": change["before_top_level_ast"]["Campaign"],
        "after": change["after_top_level_ast"]["Campaign"],
    }
    return value


def integrated_native_source_reference():
    original = copy.deepcopy(current_receiver_reference()["protected_native_source_sha256"])
    for name, change in publication_admission_reference()["protected_native_modules"].items():
        assert original[name] == change["before_file_sha256"]
        original[name] = change["after_file_sha256"]
    publisher = standing_publisher_v2_reference()
    assert original["closer_campaign.py"] == publisher["historical_receiver_before_file_sha256"]
    original["closer_campaign.py"] = publisher["after_file_sha256"]
    # Finite no-start v2 is an independently pinned successor.  Do not rewrite
    # 0953 or broaden the publication overlay to cover its two native methods.
    for name, change in finite_no_start_v2_reference()["files"].items():
        assert original[name] == change["before_file_sha256"]
        original[name] = change["after_file_sha256"]
    observer = controlled_initialization_reference()["files"]["closer_native_observer.py"]
    assert original["closer_native_observer.py"] == observer["before_file_sha256"]
    original["closer_native_observer.py"] = observer["after_file_sha256"]
    native = publisher["native_file_successors"]
    assert "closer_native_readmission.py" not in original
    original["closer_native_readmission.py"] = native["closer_native_readmission.py"][
        "after_file_sha256"
    ]
    observer_successor = native["closer_native_observer.py"]
    assert original["closer_native_observer.py"] == observer_successor["before_file_sha256"]
    original["closer_native_observer.py"] = observer_successor["after_file_sha256"]
    command_evidence = native_command_evidence_reference()
    for filename, change in command_evidence["files"].items():
        assert filename not in original
        original[filename] = change["after_file_sha256"]
    historical = historical_clock_readback_reference()
    assert original["closer_native_source_upgrade.py"] == historical["before_file_sha256"]
    original["closer_native_source_upgrade.py"] = historical["after_file_sha256"]
    for filename, change in ownership_budget_reference()["files"].items():
        if filename in original:
            assert original[filename] == change["before_file_sha256"]
        else:
            assert filename in {
                "closer_correction_ownership.py",
                "closer_correction_canary_ownership.py",
            }
        original[filename] = change["after_file_sha256"]
    for filename, change in private_git_environment_reference()["files"].items():
        if filename in original:
            assert original[filename] == change["before_file_sha256"]
        else:
            assert filename == "closer_coding_git_environment.py"
        original[filename] = change["after_file_sha256"]
    failed = failed_predecessor_upgrade_reference()["files"]
    upgrade = failed["closer_native_source_upgrade.py"]
    assert original["closer_native_source_upgrade.py"] == upgrade["before_file_sha256"]
    original["closer_native_source_upgrade.py"] = upgrade["after_file_sha256"]
    seed_assessment = seed_assessment_upgrade_reference()["files"][
        "closer_native_source_upgrade.py"
    ]
    assert original["closer_native_source_upgrade.py"] == seed_assessment["before_file_sha256"]
    original["closer_native_source_upgrade.py"] = seed_assessment["after_file_sha256"]
    for filename, change in pagination_successor_reference()["files"].items():
        assert original[filename] == change["before_file_sha256"]
        original[filename] = change["after_file_sha256"]
    for filename, change in pagination_held_successor_reference()["files"].items():
        assert original[filename] == change["before_file_sha256"]
        original[filename] = change["after_file_sha256"]
    for filename, change in pagination_historical_role_reference()["files"].items():
        assert original[filename] == change["before_file_sha256"]
        original[filename] = change["after_file_sha256"]
    assert "closer_failed_predecessor_readback.py" not in original
    original["closer_failed_predecessor_readback.py"] = failed[
        "closer_failed_predecessor_readback.py"
    ]["after_file_sha256"]
    for filename, change in initial_b_observability_reference()["files"].items():
        if change.get("added_module"):
            assert filename not in original
        else:
            # Dispatcher entry points are covered by the receiver AST map, not
            # the historical operational source-file inventory.
            if filename in original:
                assert original[filename] == change["before_file_sha256"]
            else:
                assert filename == "closer_dispatcher.py"
        original[filename] = change["after_file_sha256"]
    bootstrap = dispatcher_bytecode_bootstrap_reference()["files"]["closer_dispatcher.py"]
    assert original["closer_dispatcher.py"] == bootstrap["before_file_sha256"]
    original["closer_dispatcher.py"] = bootstrap["after_file_sha256"]
    for filename, change in capacity_pre_effect_recovery_reference()["files"].items():
        if filename == "closer_operational_capacity.py":
            assert (
                change["before_file_sha256"]
                == scoped_capacity_reference()["operational"]["after_file_sha256"]
            )
        else:
            assert original[filename] == change["before_file_sha256"]
        original[filename] = change["after_file_sha256"]
    for filename, change in validate_stop_stale_scoped_root_reference()["files"].items():
        assert filename == "closer_dispatcher.py"
        assert original[filename] == change["before_file_sha256"]
        original[filename] = change["after_file_sha256"]
    for filename, change in source_pre_effect_readmission_reference()["files"].items():
        assert original[filename] == change["before_file_sha256"]
        original[filename] = change["after_file_sha256"]
    for filename, change in completed_invitation_read_source_reference()["files"].items():
        if filename in original:
            assert original[filename] == change["before_file_sha256"]
        else:
            assert filename == "closer_correction_source_record.py"
        original[filename] = change["after_file_sha256"]
    return original


NATIVE_COMMAND_EVIDENCE_MANIFEST = (
    Path(__file__).parent
    / "fixtures/semantic_api_reference/current_native_command_evidence_v1.json"
)
NATIVE_COMMAND_EVIDENCE_SHA256 = "3f454212c351bff30d29f19c07e75381541084d453d539056f189daea92b2f92"


def native_command_evidence_reference():
    """One current parser/projection successor; historical references stay immutable."""
    raw = NATIVE_COMMAND_EVIDENCE_MANIFEST.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == NATIVE_COMMAND_EVIDENCE_SHA256
    value = json.loads(raw)
    assert value["schema"] == "native-command-evidence-source-successor/v1"
    assert value["base_commit"] == "a9b85b503597fa29ec1c468208a7f1e512fe3598"
    assert {
        filename: change["changed_top_level"] for filename, change in value["files"].items()
    } == {
        "closer_codex_appserver_bootstrap.py": "TurnResponseData",
        "closer_codex_owned_stdio.py": "finish_turn_from_run",
        "closer_native_source_upgrade.py": "_rebased_policy",
    }
    return value


HISTORICAL_CLOCK_READBACK_MANIFEST = (
    Path(__file__).parent
    / "fixtures/semantic_api_reference/current_historical_clock_readback_v1.json"
)
HISTORICAL_CLOCK_READBACK_SHA256 = (
    "d6067c6e9226edb50c32b67266646a1a03cbea7ee270ca1f10495174e84d3fad"
)


def historical_clock_readback_reference():
    """Append the literal no-ACT historical reader after the command-evidence pin."""
    raw = HISTORICAL_CLOCK_READBACK_MANIFEST.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == HISTORICAL_CLOCK_READBACK_SHA256
    value = json.loads(raw)
    assert value["schema"] == "historical-clock-readback-source-successor/v1"
    assert value["base_commit"] == "9891d33018789b8c24e583c096ac84fe14aed825"
    prior = native_command_evidence_reference()["files"]["closer_native_source_upgrade.py"]
    assert value["before_file_sha256"] == prior["after_file_sha256"]
    assert value["before_module_ast_sha256"] == prior["after_module_ast_sha256"]
    assert set(value["function_delta"]) == {
        "_historical_clock_reader_program",
        "_old_diagnostic",
    }
    assert value["function_delta"]["_historical_clock_reader_program"]["before"] is None
    assert set(value["added_constant_ast"]) == {
        "REVIEWED_CLOCK_WORKER_ID",
        "REVIEWED_CLOCK_RESULT_SHA256",
    }
    return value


OWNERSHIP_BUDGET_MANIFEST = (
    Path(__file__).parent / "fixtures/semantic_api_reference/current_ownership_budget_v1.json"
)
OWNERSHIP_BUDGET_SHA256 = "aaadcf4714bae0bc19ad1cbec6712b5ee11bbbf734fe061e6fdda9515053ca63"


def ownership_budget_reference():
    """Append the reviewed owner observation and literal recovery successor."""
    raw = OWNERSHIP_BUDGET_MANIFEST.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == OWNERSHIP_BUDGET_SHA256
    value = json.loads(raw)
    assert value["schema"] == "ownership-budget-source-successor/v1"
    assert value["base_commit"] == "8a4716f844fe002c6606b6bad4104c74be83d32c"
    assert set(value["files"]) == {
        "closer_native_source_upgrade.py",
        "closer_correction_ownership.py",
        "closer_correction_canary_ownership.py",
    }
    prior = historical_clock_readback_reference()
    successor = value["files"]["closer_native_source_upgrade.py"]
    assert prior["after_file_sha256"] == successor["before_file_sha256"]
    assert prior["after_module_ast_sha256"] == successor["before_module_ast_sha256"]
    return value


PRIVATE_GIT_ENVIRONMENT_MANIFEST = (
    Path(__file__).parent
    / "fixtures/semantic_api_reference/current_private_git_environment_v1.json"
)
PRIVATE_GIT_ENVIRONMENT_SHA256 = "0a5bc920464eb5b4e997689cf4d208685a013aac7791f3b39f8832f78f4acb62"


def private_git_environment_reference():
    """Append the closed Git-environment data producer; retain every prior review."""
    raw = PRIVATE_GIT_ENVIRONMENT_MANIFEST.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == PRIVATE_GIT_ENVIRONMENT_SHA256
    value = json.loads(raw)
    assert value["schema"] == "private-git-environment-source-successor/v1"
    assert value["base_commit"] == "c8c2c33e0e8516985604d384455d1a7030cd6d3b"
    assert set(value["files"]) == {
        "closer_native_source_upgrade.py",
        "closer_coding_git_environment.py",
    }
    before = ownership_budget_reference()["files"]["closer_native_source_upgrade.py"]
    current = value["files"]["closer_native_source_upgrade.py"]
    assert current["before_file_sha256"] == before["after_file_sha256"]
    assert current["before_module_ast_sha256"] == before["after_module_ast_sha256"]
    assert set(current["function_delta"]) == {"_rebased_policy"}
    assert current["constant_delta"] == {}
    return value


INITIAL_B_OBSERVABILITY_MANIFEST = (
    Path(__file__).parent
    / "fixtures/semantic_api_reference/current_initial_b_observability_v1.json"
)
INITIAL_B_OBSERVABILITY_SHA256 = "6e74eea096ade8630020a2dcda0a733367ffa05ef200b203def4f1dfb6e45436"


def initial_b_observability_reference():
    """Append the exact generic initial-B observation and public-input successor."""
    raw = INITIAL_B_OBSERVABILITY_MANIFEST.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == INITIAL_B_OBSERVABILITY_SHA256
    value = json.loads(raw)
    assert value["schema"] == "initial-b-observability-source-successor/v1"
    assert value["base_commit"] == "dd8d71119b92e15ff85836cc44ad40e20207ac6f"
    assert set(value["files"]) == {
        "closer_dispatcher.py",
        "closer_native_observer.py",
        "closer_runtime_binding.py",
        "closer_public_qualification_input.py",
    }
    assert set(value["files"]["closer_dispatcher.py"]["function_delta"]) == {
        "_initial_dispatch_held",
        "main",
    }
    assert set(value["files"]["closer_native_observer.py"]["function_delta"]) == {
        "_initial_dispatch_held_diagnostic",
        "initial_b_observation_summary",
        "record_initial_b_observation",
    }
    assert set(value["files"]["closer_runtime_binding.py"]["function_delta"]) == {
        "_observed_correction_b"
    }
    assert value["files"]["closer_public_qualification_input.py"]["added_module"] is True
    return value


DISPATCHER_BYTECODE_BOOTSTRAP_MANIFEST = (
    Path(__file__).parent
    / "fixtures/semantic_api_reference/current_dispatcher_bytecode_bootstrap_v1.json"
)
DISPATCHER_BYTECODE_BOOTSTRAP_SHA256 = (
    "27c5fee4f9e9848da06c3c79024b1e91eb2cb4ad77a1207e01afc37e8e1452f0"
)


def dispatcher_bytecode_bootstrap_reference():
    """Append physical-entry bytecode suppression after initial-B custody."""
    raw = DISPATCHER_BYTECODE_BOOTSTRAP_MANIFEST.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == DISPATCHER_BYTECODE_BOOTSTRAP_SHA256
    value = json.loads(raw)
    assert value["schema"] == "dispatcher-bytecode-bootstrap-source-successor/v1"
    change = value["files"]["closer_dispatcher.py"]
    prior = initial_b_observability_reference()["files"]["closer_dispatcher.py"]
    assert change["before_file_sha256"] == prior["after_file_sha256"]
    assert change["before_module_ast_sha256"] == prior["after_module_ast_sha256"]
    return value


FAILED_PREDECESSOR_UPGRADE_MANIFEST = (
    Path(__file__).parent
    / "fixtures/semantic_api_reference/current_failed_predecessor_upgrade_v1.json"
)
FAILED_PREDECESSOR_UPGRADE_SHA256 = (
    "c039b9ba7d0c5a84bf525b797885edddbcba31bb383c8fcabb0a52ab3bcc5b37"
)


def failed_predecessor_upgrade_reference():
    """Append literal failed-before-pipeline containment to the upgrade boundary."""
    raw = FAILED_PREDECESSOR_UPGRADE_MANIFEST.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == FAILED_PREDECESSOR_UPGRADE_SHA256
    value = json.loads(raw)
    assert value["schema"] == "failed-before-pipeline-source-upgrade-successor/v1"
    assert set(value["files"]) == {
        "closer_native_source_upgrade.py",
        "closer_failed_predecessor_readback.py",
    }
    prior = private_git_environment_reference()["files"]["closer_native_source_upgrade.py"]
    change = value["files"]["closer_native_source_upgrade.py"]
    assert change["before_file_sha256"] == prior["after_file_sha256"]
    assert change["before_module_ast_sha256"] == prior["after_module_ast_sha256"]
    return value


SEED_ASSESSMENT_UPGRADE_MANIFEST = (
    Path(__file__).parent
    / "fixtures/semantic_api_reference/current_seed_assessment_upgrade_v1.json"
)
SEED_ASSESSMENT_UPGRADE_SHA256 = "bf7b3a1bc299bee8bc7e4989deaae24ca1692ade0806ddc2ef940a534c34da19"


def seed_assessment_upgrade_reference():
    """Append the literal source701d seed-identity no-ACT successor."""
    raw = SEED_ASSESSMENT_UPGRADE_MANIFEST.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == SEED_ASSESSMENT_UPGRADE_SHA256
    value = json.loads(raw)
    assert value["schema"] == "seed-assessment-source-upgrade-successor/v1"
    assert value["base_commit"] == "701d55df99694c16cdcd5b4c3d657b3ab419bd53"
    change = value["files"]["closer_native_source_upgrade.py"]
    prior = failed_predecessor_upgrade_reference()["files"]["closer_native_source_upgrade.py"]
    assert change["before_file_sha256"] == prior["after_file_sha256"]
    assert change["before_module_ast_sha256"] == prior["after_module_ast_sha256"]
    assert set(change["function_delta"]) == {
        "_diagnostic_no_act",
        "_old_diagnostic",
        "_old_reference",
        "_reviewed_old_metadata",
        "_seed_assessment_no_act",
        "_seed_assessment_reference",
        "plan",
    }
    assert set(change["constant_delta"]) == {
        "REVIEWED_SEED_ASSESSMENT_CANDIDATE",
        "REVIEWED_SEED_ASSESSMENT_DESCRIPTOR_SHA256",
        "REVIEWED_SEED_ASSESSMENT_MODULES_SHA256",
        "REVIEWED_SEED_ASSESSMENT_RESULT_SHA256",
        "REVIEWED_SEED_ASSESSMENT_ROUND_ID",
        "REVIEWED_SEED_ASSESSMENT_ROUND_SHA256",
        "REVIEWED_SEED_ASSESSMENT_WORKER_ID",
    }
    return value


PAGINATION_SUCCESSOR_MANIFEST = (
    Path(__file__).parent / "fixtures/semantic_api_reference/current_pagination_successor_v1.json"
)
PAGINATION_SUCCESSOR_SHA256 = "821a36517651348577466399e2015ccfcac4f3a7c23237f8c1e9f67c44735de6"


def pagination_successor_reference():
    """Append the literal d4ca pagination repair after source701d's pin."""
    raw = PAGINATION_SUCCESSOR_MANIFEST.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == PAGINATION_SUCCESSOR_SHA256
    value = json.loads(raw)
    assert value["schema"] == "pagination-source-successor/v1"
    assert value["base_commit"] == "d4ca8c8cf144d51d7b8b67a5beb1935e4c66a694"
    upgrade = value["files"]["closer_native_source_upgrade.py"]
    prior = seed_assessment_upgrade_reference()["files"]["closer_native_source_upgrade.py"]
    assert upgrade["before_file_sha256"] == prior["after_file_sha256"]
    assert upgrade["before_module_ast_sha256"] == prior["after_module_ast_sha256"]
    assert set(value["files"]) == {
        "closer_correction_native_effects.py",
        "closer_native_source_upgrade.py",
    }
    return value


PAGINATION_HELD_SUCCESSOR_MANIFEST = (
    Path(__file__).parent
    / "fixtures/semantic_api_reference/current_pagination_held_successor_v1.json"
)
PAGINATION_HELD_SUCCESSOR_SHA256 = (
    "70239ef79a0f443879004d94c7c5dc31a671d87c16c7938ae6102a708040272d"
)


def pagination_held_successor_reference():
    """Append the literal prepare-held d4ca selector correction."""
    raw = PAGINATION_HELD_SUCCESSOR_MANIFEST.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == PAGINATION_HELD_SUCCESSOR_SHA256
    value = json.loads(raw)
    assert value["schema"] == "pagination-held-source-successor/v1"
    assert value["base_commit"] == "f01d376d77006d6923c3d6554a95be09d4cc0491"
    change = value["files"]["closer_native_source_upgrade.py"]
    prior = pagination_successor_reference()["files"]["closer_native_source_upgrade.py"]
    assert change["before_file_sha256"] == prior["after_file_sha256"]
    assert change["before_module_ast_sha256"] == prior["after_module_ast_sha256"]
    assert set(value["files"]) == {"closer_native_source_upgrade.py"}
    assert set(change["function_delta"]) == {"_sealed_pagination_reference", "prepare_held"}
    assert change["constant_delta"] == {}
    return value


def pagination_historical_role_reference():
    path = (
        Path(__file__).parent
        / "fixtures/semantic_api_reference/current_pagination_historical_role_v1.json"
    )
    raw = path.read_bytes()
    assert (
        hashlib.sha256(raw).hexdigest()
        == "b588825db7943272766fc46907239ba23b82758d3a7d7b8c25e942b10cceeb74"
    )
    value = json.loads(raw)
    assert value["schema"] == "pagination-historical-role-source-successor/v1"
    assert value["base_commit"] == "2a5aea7e257c7bae2f2dd729fd0d0245ad62540a"
    assert set(value["files"]) == {"closer_native_source_upgrade.py"}
    change = value["files"]["closer_native_source_upgrade.py"]
    prior = pagination_held_successor_reference()["files"]["closer_native_source_upgrade.py"]
    assert change["before_file_sha256"] == prior["after_file_sha256"]
    assert change["before_module_ast_sha256"] == prior["after_module_ast_sha256"]
    assert set(change["function_delta"]) == {
        "_old_reference",
        "_pagination_preinstruction_diagnostic",
        "plan",
    }
    assert change["constant_delta"] == {}
    return value


def capacity_pre_effect_recovery_reference():
    path = (
        Path(__file__).parent
        / "fixtures/semantic_api_reference/current_capacity_pre_effect_recovery_v1.json"
    )
    raw = path.read_bytes()
    assert (
        hashlib.sha256(raw).hexdigest()
        == "64411f22832c61b2438a76ec4e11f82917a4e65f666671a0d3c9b3f36cf8052c"
    )
    value = json.loads(raw)
    assert value["schema"] == "native-capacity-pre-effect-recovery-successor/v1"
    assert value["base_commit"] == "f9f14efe899f66daa0f10be191999bd75fbdc746"
    assert {name: set(change["function_delta"]) for name, change in value["files"].items()} == {
        "closer_dispatcher.py": {"_fresh_capacity_report"},
        "closer_native_observer.py": {
            "_verified_admitted_capacity_report",
            "admitted_capacity_report",
            "revalidate_admitted_capacity_report",
            "validate_admission",
        },
        "closer_native_source_upgrade.py": {
            "_generic_old_native_quiescent",
            "_generic_pre_effect_manifest",
            "_source_selection_pre_effect_reference",
            "plan",
            "prepare_held",
        },
        "closer_operational_capacity.py": {
            "_AdmittedCapacityReport",
            "_admitted_report",
            "_evaluate",
            "_report_snapshot_valid",
            "declared_profile_ids",
            "evaluate",
            "validate_admitted_report",
            "validate_report",
        },
    }
    assert all(change["constant_delta"] == {} for change in value["files"].values())
    return value


SOURCE_PRE_EFFECT_READMISSION_MANIFEST = (
    Path(__file__).parent
    / "fixtures/semantic_api_reference/current_source_pre_effect_readmission_v1.json"
)
SOURCE_PRE_EFFECT_READMISSION_SHA256 = (
    "b2a6eb009cd8d0cf12343d99554dac619528abbdfe287437579c919b49c5e3f3"
)


def source_pre_effect_readmission_reference():
    """Append the generic pre-effect same-candidate readmission successor."""
    raw = SOURCE_PRE_EFFECT_READMISSION_MANIFEST.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == SOURCE_PRE_EFFECT_READMISSION_SHA256
    value = json.loads(raw)
    assert value["schema"] == "source-pre-effect-readmission-successor/v1"
    assert value["base_commit"] == "87360c8a4b7f"
    change = value["files"]["closer_native_source_upgrade.py"]
    prior = capacity_pre_effect_recovery_reference()["files"]["closer_native_source_upgrade.py"]
    assert change["before_file_sha256"] == prior["after_file_sha256"]
    assert change["before_module_ast_sha256"] == prior["after_module_ast_sha256"]
    assert set(value["files"]) == {"closer_native_source_upgrade.py"}
    assert set(change["function_delta"]) == {"plan"}
    assert change["constant_delta"] == {}
    return value


def before_source_pre_effect_readmission_source(raw, filename):
    """Restore the exact capacity-recovery source for that historical successor."""
    successor = source_pre_effect_readmission_reference()["files"].get(filename)
    if successor is not None and hashlib.sha256(raw).hexdigest() == successor["after_file_sha256"]:
        for reversal in successor["historical_reversal"]:
            before, after = (
                reversal["before_fragment"].encode(),
                reversal["after_fragment"].encode(),
            )
            assert raw.count(after) == 1
            raw = raw.replace(after, before, 1)
        assert hashlib.sha256(raw).hexdigest() == successor["before_file_sha256"]
    return raw


COMPLETED_INVITATION_READ_SOURCE_MANIFEST = (
    Path(__file__).parent
    / "fixtures/semantic_api_reference/current_completed_invitation_read_source_v1.json"
)
COMPLETED_INVITATION_READ_SOURCE_SHA256 = (
    "8a12d6f12bcbde8cf869e1362fb3d8d1e3cceb7654efbd2aff5ea43fe369bbe1"
)


def completed_invitation_read_source_reference():
    raw = COMPLETED_INVITATION_READ_SOURCE_MANIFEST.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == COMPLETED_INVITATION_READ_SOURCE_SHA256
    value = json.loads(raw)
    assert value["schema"] == "completed-invitation-read-source-successor/v1"
    return value


def before_completed_invitation_read_source(raw, filename):
    change = completed_invitation_read_source_reference()["files"].get(filename)
    if change is None or hashlib.sha256(raw).hexdigest() == change["before_file_sha256"]:
        return raw
    assert hashlib.sha256(raw).hexdigest() == change["after_file_sha256"]
    raw = change["before_source_utf8"].encode()
    assert hashlib.sha256(raw).hexdigest() == change["before_file_sha256"]
    return raw


def validate_stop_stale_scoped_root_reference():
    path = (
        Path(__file__).parent
        / "fixtures/semantic_api_reference/current_validate_stop_stale_scoped_root_v1.json"
    )
    raw = path.read_bytes()
    assert (
        hashlib.sha256(raw).hexdigest()
        == "aa2167b641e13279b4d97be7a81367e4d70de1b9798c439d26e776530b0683bb"
    )
    value = json.loads(raw)
    assert value["schema"] == "native-validate-stop-stale-scoped-root-successor/v1"
    assert value["base_commit"] == "f9f14efe899f66daa0f10be191999bd75fbdc746"
    assert {name: set(change["function_delta"]) for name, change in value["files"].items()} == {
        "closer_dispatcher.py": {
            "_finite_timestamp",
            "_newest_pipeline_run",
            "_newest_scoped_round",
            "_normalized_assignment",
            "_scoped_root_is_stale",
            "_scoped_round_for_pipeline_run",
            "_scoped_round_summaries",
            "main",
        }
    }
    assert all(change["constant_delta"] == {} for change in value["files"].values())
    assert (
        value["files"]["closer_dispatcher.py"]["before_file_sha256"]
        == capacity_pre_effect_recovery_reference()["files"]["closer_dispatcher.py"][
            "after_file_sha256"
        ]
    )
    return value


def before_validate_stop_stale_scoped_root_source(raw, filename):
    """Undo only the exact live validate-stop tip; any other bytes pass through unchanged."""
    change = validate_stop_stale_scoped_root_reference()["files"].get(filename)
    if change is None or hashlib.sha256(raw).hexdigest() != change["after_file_sha256"]:
        return raw
    for reversal in change["historical_reversal"]:
        before, after = reversal["before_fragment"].encode(), reversal["after_fragment"].encode()
        assert raw.count(after) == 1
        raw = raw.replace(after, before, 1)
    assert hashlib.sha256(raw).hexdigest() == change["before_file_sha256"]
    return raw


def before_capacity_pre_effect_recovery_source(raw, filename):
    """Keep historical assertions exact beneath separately attested successors."""
    raw = before_validate_stop_stale_scoped_root_source(raw, filename)
    raw = before_completed_invitation_read_source(raw, filename)
    raw = before_source_pre_effect_readmission_source(raw, filename)
    change = capacity_pre_effect_recovery_reference()["files"].get(filename)
    if change is None or hashlib.sha256(raw).hexdigest() == change["before_file_sha256"]:
        return raw
    assert hashlib.sha256(raw).hexdigest() == change["after_file_sha256"]
    for reversal in change["historical_reversal"]:
        before, after = reversal["before_fragment"].encode(), reversal["after_fragment"].encode()
        assert raw.count(after) == 1
        raw = raw.replace(after, before, 1)
    assert hashlib.sha256(raw).hexdigest() == change["before_file_sha256"]
    return raw


def before_dispatcher_bytecode_bootstrap(source):
    """Undo only the exact sealed-runtime bootstrap successor for old audits."""
    source = before_validate_stop_stale_scoped_root_source(
        source.encode(), "closer_dispatcher.py"
    ).decode()
    capacity = capacity_pre_effect_recovery_reference()["files"]["closer_dispatcher.py"]
    if hashlib.sha256(source.encode()).hexdigest() == capacity["after_file_sha256"]:
        source = before_capacity_pre_effect_recovery_source(
            source.encode(), "closer_dispatcher.py"
        ).decode()
    change = dispatcher_bytecode_bootstrap_reference()["files"]["closer_dispatcher.py"]
    assert hashlib.sha256(source.encode()).hexdigest() == change["after_file_sha256"]
    reversal = change["historical_reversal"]
    assert source.count(reversal["after_fragment"]) == 1
    source = source.replace(reversal["after_fragment"], reversal["before_fragment"], 1)
    assert hashlib.sha256(source.encode()).hexdigest() == change["before_file_sha256"]
    return source


def before_initial_b_observability_dispatcher(source):
    """Reverse only the exact reviewed initial-B dispatcher successor for old audits."""
    source = before_validate_stop_stale_scoped_root_source(
        source.encode(), "closer_dispatcher.py"
    ).decode()
    capacity = capacity_pre_effect_recovery_reference()["files"]["closer_dispatcher.py"]
    if hashlib.sha256(source.encode()).hexdigest() == capacity["after_file_sha256"]:
        source = before_capacity_pre_effect_recovery_source(
            source.encode(), "closer_dispatcher.py"
        ).decode()
    bootstrap = dispatcher_bytecode_bootstrap_reference()["files"]["closer_dispatcher.py"]
    if hashlib.sha256(source.encode()).hexdigest() == bootstrap["after_file_sha256"]:
        source = before_dispatcher_bytecode_bootstrap(source)
    change = initial_b_observability_reference()["files"]["closer_dispatcher.py"]
    assert hashlib.sha256(source.encode()).hexdigest() == change["after_file_sha256"]
    tree = ast.parse(source)
    assert fingerprint(tree) == change["after_module_ast_sha256"]
    top = {
        node.name: node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.ClassDef))
    }
    for name, delta in change["function_delta"].items():
        assert fingerprint(top[name]) == delta["after"]
    helper = ast.get_source_segment(source, top["_initial_dispatch_held"])
    assert isinstance(helper, str) and helper
    reversal = change["historical_reversal"]
    assert source.count(helper) == 1
    source = source.replace(helper + "\n\n\n", "", 1)
    assert source.count(reversal["main_prefix_after"]) == 1
    source = source.replace(reversal["main_prefix_after"], reversal["main_prefix_before"], 1)
    assert source.count(reversal["main_suffix_after"]) == 1
    source = source.replace(reversal["main_suffix_after"], "", 1)
    assert hashlib.sha256(source.encode()).hexdigest() == change["before_file_sha256"]
    return source


CONTROLLED_INITIALIZATION_MANIFEST = (
    Path(__file__).parent
    / "fixtures/semantic_api_reference/current_controlled_initialization_v1.json"
)
CONTROLLED_INITIALIZATION_SHA256 = (
    "c070e59b4fd2c9ee2ee7c2ee7b2c187e87aa02be3fc0df47398bf2af205bec35"
)


def controlled_initialization_reference():
    raw = CONTROLLED_INITIALIZATION_MANIFEST.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == CONTROLLED_INITIALIZATION_SHA256
    value = json.loads(raw)
    assert value["base_commit"] == "0a9b0f93909c82532add2d022e26e768df785839"
    assert {name: change["changed_node"] for name, change in value["files"].items()} == {
        "closer_campaign.py": "Campaign.begin",
        "closer_native_observer.py": "publish_admission",
    }
    assert set(value["files"]["closer_native_observer.py"]["additional_changed_top_level_ast"]) == {
        "wrapper_block"
    }
    return value


STANDING_PUBLISHER_V2_MANIFEST = (
    Path(__file__).parent / "fixtures/semantic_api_reference/current_standing_publisher_v2.json"
)
STANDING_PUBLISHER_V2_SHA256 = "910da21b2b420841506bfebaa6c373eef7e2e869420f89a8ca0529dd51f4bf68"


def standing_publisher_v2_reference():
    """One immutable source successor; historical receiver references stay intact."""
    raw = STANDING_PUBLISHER_V2_MANIFEST.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == STANDING_PUBLISHER_V2_SHA256
    value = json.loads(raw)
    assert value["schema"] == "standing-publisher-v2-source-successor/v1"
    assert value["source_before_file_sha256"] == value["historical_receiver_before_file_sha256"]
    assert set(value["changed_functions"]) == {
        "_controller_demand_context",
        "controller_demand_before_discovery",
        "controller_entry",
        "load_manifest",
    }
    assert set(value["added_functions"]) == {
        "_controller_demand_pin_snapshot",
        "_controller_demand_selected_v2",
        "_controller_demand_v2_integrity_stop",
        "_validated_manifest",
    }
    assert set(value["native_file_successors"]) == {
        "closer_native_readmission.py",
        "closer_native_observer.py",
    }
    assert {
        name: change["changed_function"]["name"]
        for name, change in value["native_file_successors"].items()
    } == {
        "closer_native_readmission.py": "_initialization",
        "closer_native_observer.py": "_demand_assignment_bytes",
    }
    return value


def assert_standing_publisher_v2_successor(source=None):
    """Compare the whole campaign module outside the exact reviewed v2 delta."""
    reviewed = standing_publisher_v2_reference()
    raw = source_path("closer_campaign.py").read_bytes() if source is None else source
    assert hashlib.sha256(raw).hexdigest() == reviewed["after_file_sha256"]
    tree = ast.parse(raw)
    assert fingerprint(tree) == reviewed["after_module_ast_sha256"]
    top = {
        node.name: node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.ClassDef))
    }
    for name, change in reviewed["changed_functions"].items():
        assert fingerprint(top[name]) == change["after"]
    for name, expected in reviewed["added_functions"].items():
        assert fingerprint(top[name]) == expected
    pin = next(
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "_CONTROLLER_DEMAND_PIN_UNREAD"
            for target in node.targets
        )
    )
    assert fingerprint(pin) == reviewed["added_constant_ast_sha256"]
    selected = set(reviewed["changed_functions"]) | set(reviewed["added_functions"])
    tree.body = [
        node
        for node in tree.body
        if not (
            isinstance(node, (ast.FunctionDef, ast.ClassDef))
            and node.name in selected
            or node is pin
        )
    ]
    assert fingerprint(tree) == reviewed["outside_reviewed_changes_ast_sha256"]
    for filename, successor in reviewed["native_file_successors"].items():
        raw = before_capacity_pre_effect_recovery_source(
            source_path(filename).read_bytes(), filename
        )
        later = initial_b_observability_reference()["files"].get(filename)
        if later is not None:
            assert successor["after_file_sha256"] == later["before_file_sha256"]
            assert hashlib.sha256(raw).hexdigest() == later["after_file_sha256"]
        else:
            assert hashlib.sha256(raw).hexdigest() == successor["after_file_sha256"]
        tree = ast.parse(raw)
        changed = successor["changed_function"]
        node = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == changed["name"]
        )
        assert fingerprint(node) == changed["after"]
        if later is not None:
            selected = []
            for name, delta in later["function_delta"].items():
                nodes = [item for item in tree.body if getattr(item, "name", None) == name]
                assert len(nodes) == 1 and fingerprint(nodes[0]) == delta["after"]
                assert delta["before"] is None
                selected.extend(nodes)
            tree.body = [item for item in tree.body if item not in selected]
            assert fingerprint(tree) == successor["after_module_ast_sha256"]
        tree.body.remove(node)
        assert fingerprint(tree) == successor["outside_changed_function_ast_sha256"]


FINITE_NO_START_V2_MANIFEST = (
    Path(__file__).parent / "fixtures/semantic_api_reference/current_finite_no_start_v2.json"
)
FINITE_NO_START_V2_SHA256 = "c4b75cd83a6070d18609cda61077a4044b91123fecdb2dcad4d92de04e139586"


def finite_no_start_v2_reference():
    raw = FINITE_NO_START_V2_MANIFEST.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == FINITE_NO_START_V2_SHA256
    value = json.loads(raw)
    assert value["base_commit"] == "112e9263fd7545bd5a3ab50913a867126fbe862a"
    assert set(value["files"]) == {"closer_native_observer.py", "closer_runtime_binding.py"}
    observer = value["files"]["closer_native_observer.py"]
    assert set(observer["changed_nodes"]) == {"pre_dispatch_no_start_proof"}
    binding = value["files"]["closer_runtime_binding.py"]
    assert set(binding["changed_nodes"]) == {"PinnedNativeHooks.begin_terminal_receipt"}
    assert binding["class_method_scope"]["only_changed_methods"] == ["begin_terminal_receipt"]
    return value


STRONG_ROLE_ENROLLMENT_MANIFEST = (
    Path(__file__).parent / "fixtures/semantic_api_reference/current_strong_role_enrollment_v1.json"
)
STRONG_ROLE_ENROLLMENT_SHA256 = "8d5be1378f1056cebfac2c8b5241cd87af185e8135f0d0a91e13c2fec2fa94fa"


def strong_role_enrollment_reference():
    raw = STRONG_ROLE_ENROLLMENT_MANIFEST.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == STRONG_ROLE_ENROLLMENT_SHA256
    value = json.loads(raw)
    assert value["schema"] == "closer-strong-role-enrollment-overlay/v1"
    return value["role_module"]


STRONG_ROLE_RENEWAL_MANIFEST = (
    Path(__file__).parent / "fixtures/semantic_api_reference/current_strong_role_renewal_v1.json"
)
STRONG_ROLE_RENEWAL_SHA256 = "a6bd1e6913f6c725a395eef7faa4bd20f630cc55749abc6dffa99daffab6abbc"


def strong_role_renewal_reference():
    raw = STRONG_ROLE_RENEWAL_MANIFEST.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == STRONG_ROLE_RENEWAL_SHA256
    value = json.loads(raw)
    assert value["schema"] == "closer-strong-role-renewal-overlay/v1"
    assert set(value) == {"schema", "scope", "actual_report_ref", "quality_ref", "role_module"}
    return value


INITIAL_B_ROLE_QUALIFICATION_MANIFEST = (
    Path(__file__).parent
    / "fixtures/semantic_api_reference/current_initial_b_role_qualification_v1.json"
)
INITIAL_B_ROLE_QUALIFICATION_SHA256 = (
    "6d060652da6fc7b89016066ed911c12a2dceff71cc443dad341da31dbe90973d"
)


def initial_b_role_qualification_reference():
    raw = INITIAL_B_ROLE_QUALIFICATION_MANIFEST.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == INITIAL_B_ROLE_QUALIFICATION_SHA256
    value = json.loads(raw)
    assert value["schema"] == "closer-initial-b-role-qualification-overlay/v1"
    assert set(value) == {"schema", "scope", "actual_report_ref", "quality_ref", "role_module"}
    return value


DISPATCHER_BYTECODE_ROLE_QUALIFICATION_MANIFEST = (
    Path(__file__).parent
    / "fixtures/semantic_api_reference/current_dispatcher_bytecode_role_qualification_v1.json"
)
DISPATCHER_BYTECODE_ROLE_QUALIFICATION_SHA256 = (
    "c061057e09b67f8ed18f17239f7212056de079230e959cbcd1d0dce6dd53a8f9"
)


def dispatcher_bytecode_role_qualification_reference():
    """Append the fresh, exact dispatcher-bootstrap qualification row."""
    raw = DISPATCHER_BYTECODE_ROLE_QUALIFICATION_MANIFEST.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == DISPATCHER_BYTECODE_ROLE_QUALIFICATION_SHA256
    value = json.loads(raw)
    assert value["schema"] == "closer-dispatcher-bytecode-role-qualification-overlay/v1"
    assert set(value) == {"schema", "scope", "actual_report_ref", "quality_ref", "role_module"}
    return value


def current_role_renewal_reference():
    path = Path(__file__).parent / "fixtures/semantic_api_reference/current_role_renewal_v1.json"
    raw = path.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == (
        "2954761a7e87af45832985207ea1559937c81c92050c1f76ceff64649d4a2993"
    )
    value = json.loads(raw)
    assert value["schema"] == "closer-current-role-renewal-overlay/v1"
    prior = dispatcher_bytecode_role_qualification_reference()["role_module"]
    change = value["role_module"]
    assert change["before_file_sha256"] == prior["after_file_sha256"]
    assert change["before_module_ast_sha256"] == prior["after_module_ast_sha256"]
    return value


def capacity_bridge_role_reference():
    path = (
        Path(__file__).parent
        / "fixtures/semantic_api_reference/current_capacity_bridge_role_v1.json"
    )
    raw = path.read_bytes()
    assert (
        hashlib.sha256(raw).hexdigest()
        == "f9cbf11fe29067e630f0e769b247ad76c71f3d878e2cd3b77dccc8385266b4a8"
    )
    value = json.loads(raw)
    assert value["schema"] == "closer-capacity-bridge-role-successor/v1"
    assert (
        value["role_module"]["before_file_sha256"]
        == current_role_renewal_reference()["role_module"]["after_file_sha256"]
    )
    assert set(value["role_module"]["assignment_delta"]) == {
        "ROLE_QUALIFICATIONS",
        "ROLE_QUALIFICATION_SUPERSEDES",
    }
    return value


def assert_capacity_bridge_role_successor(raw):
    reference = capacity_bridge_role_reference()
    change = reference["role_module"]
    assert hashlib.sha256(raw).hexdigest() == change["after_file_sha256"]
    tree = ast.parse(raw)
    assert fingerprint(tree) == change["after_module_ast_sha256"]
    for symbol, delta in change["assignment_delta"].items():
        node = next(
            n
            for n in tree.body
            if (isinstance(n, ast.AnnAssign) and getattr(n.target, "id", None) == symbol)
            or (
                isinstance(n, ast.Assign)
                and any(getattr(t, "id", None) == symbol for t in n.targets)
            )
        )
        assert fingerprint(node) == delta["after_ast_sha256"]
        indices = [
            i
            for i, key in enumerate(node.value.keys)
            if isinstance(key, ast.Constant) and key.value == reference["new_qualification_id"]
        ]
        assert len(indices) == 1
        index = indices[0]
        assert fingerprint(node.value.values[index]) == delta["added_value_ast_sha256"]
        node.value.keys.pop(index)
        node.value.values.pop(index)
        assert fingerprint(node) == delta["before_ast_sha256"]
    assert fingerprint(tree) == change["before_module_ast_sha256"]


def validate_stop_role_reference():
    path = (
        Path(__file__).parent
        / "fixtures/semantic_api_reference/current_validate_stop_stale_scoped_root_role_v1.json"
    )
    raw = path.read_bytes()
    assert (
        hashlib.sha256(raw).hexdigest()
        == "2a4b3718e2e009caae9e831a76baa675b34e7108ec82f54091a40c9d90f6649f"
    )
    value = json.loads(raw)
    assert value["schema"] == "closer-validate-stop-stale-scoped-root-role-successor/v1"
    assert value["base_commit"] == "f9f14efe899f66daa0f10be191999bd75fbdc746"
    assert (
        value["role_module"]["before_file_sha256"]
        == capacity_bridge_role_reference()["role_module"]["after_file_sha256"]
    )
    assert (
        value["role_module"]["before_module_ast_sha256"]
        == capacity_bridge_role_reference()["role_module"]["after_module_ast_sha256"]
    )
    assert set(value["role_module"]["assignment_delta"]) == {
        "ROLE_QUALIFICATIONS",
        "ROLE_QUALIFICATION_SUPERSEDES",
    }
    return value


def assert_validate_stop_role_successor(raw):
    reference = validate_stop_role_reference()
    change = reference["role_module"]
    assert hashlib.sha256(raw).hexdigest() == change["after_file_sha256"]
    tree = ast.parse(raw)
    assert fingerprint(tree) == change["after_module_ast_sha256"]
    for symbol, delta in change["assignment_delta"].items():
        node = next(
            n
            for n in tree.body
            if (isinstance(n, ast.AnnAssign) and getattr(n.target, "id", None) == symbol)
            or (
                isinstance(n, ast.Assign)
                and any(getattr(t, "id", None) == symbol for t in n.targets)
            )
        )
        assert fingerprint(node) == delta["after_ast_sha256"]
        indices = [
            i
            for i, key in enumerate(node.value.keys)
            if isinstance(key, ast.Constant) and key.value == reference["new_qualification_id"]
        ]
        assert len(indices) == 1
        index = indices[0]
        assert fingerprint(node.value.values[index]) == delta["added_value_ast_sha256"]
        node.value.keys.pop(index)
        node.value.values.pop(index)
        assert fingerprint(node) == delta["before_ast_sha256"]
    assert fingerprint(tree) == change["before_module_ast_sha256"]


def assert_reviewed_role_registry_successor(source=None):
    """Pin the reviewed record while preserving every other module AST statement."""
    change = publication_admission_reference()["reviewed_native_followups"]["role_registry"]
    raw = source_path("closer_role_qualification.py").read_bytes() if source is None else source
    if (
        hashlib.sha256(raw).hexdigest()
        == validate_stop_role_reference()["role_module"]["after_file_sha256"]
    ):
        assert_validate_stop_role_successor(raw)
        return
    if (
        hashlib.sha256(raw).hexdigest()
        == capacity_bridge_role_reference()["role_module"]["after_file_sha256"]
    ):
        assert_capacity_bridge_role_successor(raw)
        return
    bootstrap = dispatcher_bytecode_role_qualification_reference()["role_module"]
    renewal = current_role_renewal_reference()["role_module"]
    if hashlib.sha256(raw).hexdigest() == renewal["after_file_sha256"]:
        tree = ast.parse(raw)
        assert fingerprint(tree) == renewal["after_module_ast_sha256"]
        registry = next(
            node
            for node in tree.body
            if isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "ROLE_QUALIFICATIONS"
        )
        assert fingerprint(registry) == renewal["after_registry_ast_sha256"]
        index = next(
            index
            for index, key in enumerate(registry.value.keys)
            if isinstance(key, ast.Constant) and key.value == renewal["new_qualification_id"]
        )
        assert fingerprint(registry.value.values[index]) == renewal["new_record_ast_sha256"]
        registry.value.keys.pop(index)
        registry.value.values.pop(index)
        assert fingerprint(registry) == renewal["before_registry_ast_sha256"]
        assert fingerprint(tree) == renewal["before_module_ast_sha256"]
        return
    if hashlib.sha256(raw).hexdigest() == bootstrap["after_file_sha256"]:
        tree = ast.parse(raw)
        assert fingerprint(tree) == bootstrap["after_module_ast_sha256"]
        registry = next(
            node
            for node in tree.body
            if isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "ROLE_QUALIFICATIONS"
        )
        assert fingerprint(registry) == bootstrap["after_registry_ast_sha256"]
        pairs = list(zip(registry.value.keys, registry.value.values))
        key, record = next(
            pair
            for pair in pairs
            if isinstance(pair[0], ast.Constant)
            and pair[0].value == bootstrap["new_qualification_id"]
        )
        assert fingerprint(record) == bootstrap["new_record_ast_sha256"]
        registry.value.keys.remove(key)
        registry.value.values.remove(record)
        assert fingerprint(registry) == bootstrap["before_registry_ast_sha256"]
        table = next(
            node
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "ROLE_QUALIFICATION_SUPERSEDES"
                for target in node.targets
            )
        )
        assert fingerprint(table) == bootstrap["supersession_table_ast_sha256"]
        selected = [table]
        for name, delta in bootstrap["function_delta"].items():
            node = next(node for node in tree.body if getattr(node, "name", None) == name)
            assert fingerprint(node) == delta["after"]
            selected.append(node)
        tree.body = [node for node in tree.body if node not in selected]
        assert fingerprint(tree) == bootstrap["outside_reviewed_changes_ast_sha256"]
        return
    initial = initial_b_role_qualification_reference()
    initial_role = initial["role_module"]
    if hashlib.sha256(raw).hexdigest() == initial_role["after_file_sha256"]:
        tree = ast.parse(raw)
        assert fingerprint(tree) == initial_role["after_module_ast_sha256"]
        registry = next(
            node
            for node in tree.body
            if isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "ROLE_QUALIFICATIONS"
        )
        assert fingerprint(registry) == initial_role["after_registry_ast_sha256"]
        pairs = list(zip(registry.value.keys, registry.value.values))
        key, record = next(
            pair
            for pair in pairs
            if isinstance(pair[0], ast.Constant)
            and pair[0].value == initial_role["new_qualification_id"]
        )
        assert fingerprint(record) == initial_role["new_record_ast_sha256"]
        registry.value.keys.remove(key)
        registry.value.values.remove(record)
        assert fingerprint(registry) == initial_role["before_registry_ast_sha256"]
        assert fingerprint(tree) == initial_role["before_module_ast_sha256"]
        return
    renewal = strong_role_renewal_reference()
    renewal_role = renewal["role_module"]
    if hashlib.sha256(raw).hexdigest() == renewal_role["after_file_sha256"]:
        tree = ast.parse(raw)
        assert fingerprint(tree) == renewal_role["after_module_ast_sha256"]
        registry = next(
            node
            for node in tree.body
            if isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "ROLE_QUALIFICATIONS"
        )
        assert fingerprint(registry) == renewal_role["after_registry_ast_sha256"]
        pairs = list(zip(registry.value.keys, registry.value.values))
        key, record = next(
            pair
            for pair in pairs
            if isinstance(pair[0], ast.Constant)
            and pair[0].value == renewal_role["new_qualification_id"]
        )
        assert fingerprint(record) == renewal_role["new_record_ast_sha256"]
        registry.value.keys.remove(key)
        registry.value.values.remove(record)
        assert fingerprint(registry) == renewal_role["before_registry_ast_sha256"]
        assert fingerprint(tree) == renewal_role["before_module_ast_sha256"]
        return
    enrollment = strong_role_enrollment_reference()
    if hashlib.sha256(raw).hexdigest() == enrollment["after_file_sha256"]:
        tree = ast.parse(raw)
        assert fingerprint(tree) == enrollment["after_module_ast_sha256"]
        registry = next(
            node
            for node in tree.body
            if isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "ROLE_QUALIFICATIONS"
        )
        assert fingerprint(registry) == enrollment["after_registry_ast_sha256"]
        pairs = list(zip(registry.value.keys, registry.value.values))
        key, record = next(
            pair
            for pair in pairs
            if isinstance(pair[0], ast.Constant)
            and pair[0].value == enrollment["new_qualification_id"]
        )
        assert fingerprint(record) == enrollment["new_record_ast_sha256"]
        registry.value.keys.remove(key)
        registry.value.values.remove(record)
        assert fingerprint(registry) == enrollment["before_registry_ast_sha256"]
        assert fingerprint(tree) == enrollment["before_module_ast_without_new_record_sha256"]
        assert enrollment["only_changed_definitions"] == [
            "_selected_record",
            "qualification_problems",
            "qualified_record",
        ]
        tree.body = [
            n
            for n in tree.body
            if not (
                isinstance(n, ast.FunctionDef) and n.name in enrollment["only_changed_definitions"]
            )
            and n is not registry
        ]
        assert fingerprint(tree) == enrollment["unchanged_module_ast_sha256"]
        return
    identity = repository_identity_reference()["role_source"]
    if hashlib.sha256(raw).hexdigest() == identity["after_file_sha256"]:
        raw = raw.replace(
            identity["after_execution_contract"].encode(),
            identity["before_execution_contract"].encode(),
            1,
        )
        assert hashlib.sha256(raw).hexdigest() == identity["before_file_sha256"]
    bounded = finite_prepare_reference()["role_source"]
    if hashlib.sha256(raw).hexdigest() == bounded["after_file_sha256"]:
        # Test-only comparison of one exact separately reviewed source relation.
        raw = raw.replace(
            bounded["after_execution_contract"].encode(),
            bounded["before_execution_contract"].encode(),
            1,
        )
        assert hashlib.sha256(raw).hexdigest() == bounded["before_file_sha256"]
    tree = ast.parse(raw)
    actual_sha = hashlib.sha256(raw).hexdigest()
    update = advisory_plan_omission_reference()["role_source"]
    if actual_sha == update["after_file_sha256"]:
        # TEST-ONLY historical comparison after exact whole-module pin. Restore
        # solely this reviewed data literal in the AST, never production data.
        node = next(
            n
            for n in tree.body
            if isinstance(n, ast.Assign)
            and any(
                isinstance(t, ast.Name) and t.id == "_CLOSED_SONNET_SOURCE_ADMISSION"
                for t in n.targets
            )
        )
        assert fingerprint(node) == update["after_constant_ast_sha256"]
        value = next(
            v
            for k, v in zip(node.value.keys, node.value.values)
            if isinstance(k, ast.Constant) and k.value == "execution_contract_sha256"
        )
        assert value.value == update["after_execution_contract"]
        value.value = update["before_execution_contract"]
        assert fingerprint(node) == update["before_constant_ast_sha256"]
        actual_sha = update["before_file_sha256"]
    if actual_sha != change["after_file_sha256"]:
        successor = source_admission_reference()["role_module"]
        assert successor["before_file_sha256"] == change["after_file_sha256"]
        assert actual_sha == successor["after_file_sha256"]
        added = successor["added_ast"]
        selected = []
        for node in tree.body:
            name = getattr(node, "name", None)
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                name = getattr(node.targets[0], "id", None)
            if name in added:
                assert fingerprint(node) == added[name]
                selected.append(node)
        assert len(selected) == len(added)
        tree.body = [node for node in tree.body if node not in selected]
        assert fingerprint(tree) == successor["before_complete_ast_sha256"]
    registry = [
        node
        for node in tree.body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "ROLE_QUALIFICATIONS"
    ]
    assert len(registry) == 1
    tree.body.remove(registry[0])
    assert fingerprint(tree) == change["outside_registry_ast_sha256"]
    assert fingerprint(registry[0]) == change["after_registry_ast_sha256"]


SOURCE_ADMISSION_MANIFEST = (
    Path(__file__).parent
    / "fixtures/semantic_api_reference/closed_current_source_admission_v1.json"
)
SOURCE_ADMISSION_SHA256 = "858bb6a931eb93a53c6fe9849fc579f7eed2bda4cb69fc6ece50a02e2ecfafae"


def source_admission_reference():
    raw = SOURCE_ADMISSION_MANIFEST.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == SOURCE_ADMISSION_SHA256
    value = json.loads(raw)
    assert set(value["dispatcher_function_delta"]) == {"_controlled_launch_inputs"}
    assert set(value["role_module"]["added_ast"]) == {
        "_CLOSED_SONNET_SOURCE_ADMISSION",
        "_source_digest",
        "execution_source_support",
        "execution_record_matches",
    }
    return value


ADVISORY_PLAN_OMISSION_MANIFEST = (
    Path(__file__).parent / "fixtures/semantic_api_reference/current_advisory_plan_omission_v1.json"
)
ADVISORY_PLAN_OMISSION_SHA256 = "45de03d3c3e99e7cce48a63896d4b41c7999ef8eafbf3244f29537e4dff9147d"


def advisory_plan_omission_reference():
    raw = ADVISORY_PLAN_OMISSION_MANIFEST.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == ADVISORY_PLAN_OMISSION_SHA256
    return json.loads(raw)


FINITE_PREPARE_MANIFEST = (
    Path(__file__).parent
    / "fixtures/semantic_api_reference/current_finite_prepare_reference_v1.json"
)
FINITE_PREPARE_SHA256 = "b99b0674af9cd27a1a2bd5ff74175b63e55c180d5215cbb6d8a1bcc6e3f34494"


def finite_prepare_reference():
    raw = FINITE_PREPARE_MANIFEST.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == FINITE_PREPARE_SHA256
    return json.loads(raw)


def before_finite_prepare_source(source):
    """Restore only an exact whole-file-pinned prospective delta for old audits."""
    source = before_scoped_capacity_source(source)
    identity = repository_identity_reference()["dispatcher"]
    assert hashlib.sha256(source.encode()).hexdigest() == identity["after_file_sha256"]
    assert source.count(identity["after_fragment"]) == 1
    source = source.replace(identity["after_fragment"], identity["before_fragment"], 1)
    assert hashlib.sha256(source.encode()).hexdigest() == identity["before_file_sha256"]
    pin = finite_prepare_reference()["dispatcher"]
    assert hashlib.sha256(source.encode()).hexdigest() == pin["after_file_sha256"]
    assert source.count(pin["after_fragment"]) == 1
    restored = source.replace(pin["after_fragment"], pin["before_fragment"], 1)
    assert hashlib.sha256(restored.encode()).hexdigest() == pin["before_file_sha256"]
    return restored


REPOSITORY_IDENTITY_MANIFEST = (
    Path(__file__).parent / "fixtures/semantic_api_reference/current_repository_identity_v1.json"
)
REPOSITORY_IDENTITY_SHA256 = "4ff696cea743352bd382e51082904a09306bcc436c99233bc31ebee96e607df5"


def repository_identity_reference():
    raw = REPOSITORY_IDENTITY_MANIFEST.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == REPOSITORY_IDENTITY_SHA256
    return json.loads(raw)


SCOPED_CAPACITY_MANIFEST = (
    Path(__file__).parent / "fixtures/semantic_api_reference/current_scoped_capacity_v1.json"
)
SCOPED_CAPACITY_SHA256 = "4f337dc499f74c198e6909758c651ca8761c47d3b53306c3f004c4c63775da25"


def scoped_capacity_reference():
    """One reviewed enumeration successor, never a mutable AST allowlist."""
    raw = SCOPED_CAPACITY_MANIFEST.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == SCOPED_CAPACITY_SHA256
    value = json.loads(raw)
    assert value["schema"] == "reviewed-scoped-capacity-source-delta/v1"
    change = value["dispatcher"]
    assert change["changed_function"]["name"] == "_pipeline_capacity"
    assert change["validation_function"]["name"] == "_fresh_capacity_report"
    assert change["added_function"]["name"] == "_report_profiles_for_agent"
    assert (
        hashlib.sha256(
            before_capacity_pre_effect_recovery_source(
                source_path("closer_operational_capacity.py").read_bytes(),
                "closer_operational_capacity.py",
            )
        ).hexdigest()
        == value["operational"]["after_file_sha256"]
    )
    return value


def before_scoped_capacity_source(source):
    """Undo only exact reviewed capacity bytes before applying older audits."""
    source = before_validate_stop_stale_scoped_root_source(
        source.encode(), "closer_dispatcher.py"
    ).decode()
    source = before_capacity_pre_effect_recovery_source(
        source.encode(), "closer_dispatcher.py"
    ).decode()
    bootstrap = dispatcher_bytecode_bootstrap_reference()["files"]["closer_dispatcher.py"]
    if hashlib.sha256(source.encode()).hexdigest() == bootstrap["after_file_sha256"]:
        source = before_dispatcher_bytecode_bootstrap(source)
    initial = initial_b_observability_reference()["files"]["closer_dispatcher.py"]
    if hashlib.sha256(source.encode()).hexdigest() == initial["after_file_sha256"]:
        source = before_initial_b_observability_dispatcher(source)
    change = scoped_capacity_reference()["dispatcher"]
    assert hashlib.sha256(source.encode()).hexdigest() == change["after_file_sha256"]
    assert source.count(change["after_fragment"]) == source.count(change["added_fragment"]) == 1
    restored = source.replace(change["after_fragment"], change["before_fragment"], 1)
    restored = restored.replace(change["added_fragment"], "", 1)
    validation = change["validation_function"]
    assert restored.count(validation["after_fragment"]) == 1
    restored = restored.replace(validation["after_fragment"], validation["before_fragment"], 1)
    assert hashlib.sha256(restored.encode()).hexdigest() == change["before_file_sha256"]
    return restored
