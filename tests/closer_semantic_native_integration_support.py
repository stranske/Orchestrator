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
    # Finite no-start v2 is an independently pinned successor.  Do not rewrite
    # 0953 or broaden the publication overlay to cover its two native methods.
    for name, change in finite_no_start_v2_reference()["files"].items():
        assert original[name] == change["before_file_sha256"]
        original[name] = change["after_file_sha256"]
    observer = controlled_initialization_reference()["files"]["closer_native_observer.py"]
    assert original["closer_native_observer.py"] == observer["before_file_sha256"]
    original["closer_native_observer.py"] = observer["after_file_sha256"]
    return original


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


def assert_reviewed_role_registry_successor(source=None):
    """Pin the reviewed record while preserving every other module AST statement."""
    change = publication_admission_reference()["reviewed_native_followups"]["role_registry"]
    raw = source_path("closer_role_qualification.py").read_bytes() if source is None else source
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
SCOPED_CAPACITY_SHA256 = "c8e7da06d060dbe19d2608fb1ff92485661342db3b6a68739790b728e74671c9"


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
        hashlib.sha256(source_path("closer_operational_capacity.py").read_bytes()).hexdigest()
        == value["operational"]["after_file_sha256"]
    )
    return value


def before_scoped_capacity_source(source):
    """Undo only exact reviewed capacity bytes before applying older audits."""
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
