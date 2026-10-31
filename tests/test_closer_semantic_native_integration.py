"""Pure native/semantic source-union guards; no original process mechanism run."""

import ast
import copy
import hashlib
import json

import closer_semantic_native_integration_support as support
import pytest
from closer_semantic_audit_support import fingerprint, functions, source_path


def test_native_operational_and_latest_reader_sources_are_exact_reviewed0953():
    for filename, expected in support.integrated_native_source_reference().items():
        assert hashlib.sha256(source_path(filename).read_bytes()).hexdigest() == expected, filename


def test_private_git_successor_changes_only_reviewed_rebase_and_inert_added_module():
    reviewed = support.private_git_environment_reference()
    for filename, change in reviewed["files"].items():
        raw = source_path(filename).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == change["after_file_sha256"]
        tree = ast.parse(raw)
        assert fingerprint(tree) == change["after_module_ast_sha256"]
        if change.get("added_module"):
            assert filename == "closer_coding_git_environment.py"
            assert change["before_file_sha256"] is None
            continue
        assert filename == "closer_native_source_upgrade.py"
        assert set(change["function_delta"]) == {"_rebased_policy"}
        assert change["constant_delta"] == {}
        for name, delta in change["function_delta"].items():
            nodes = [node for node in tree.body if getattr(node, "name", None) == name]
            assert len(nodes) == 1 and fingerprint(nodes[0]) == delta["after"]
            assert change["before_top_level_ast"][name] == delta["before"]
            tree.body.remove(nodes[0])
        assert fingerprint(tree) == change["outside_reviewed_changes_ast_sha256"]


def test_initial_b_observability_successor_is_exact_and_has_no_unreviewed_ast_delta():
    reviewed = support.initial_b_observability_reference()
    for filename, change in reviewed["files"].items():
        raw = source_path(filename).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == change["after_file_sha256"]
        tree = ast.parse(raw)
        assert fingerprint(tree) == change["after_module_ast_sha256"]
        if change.get("added_module"):
            assert filename == "closer_public_qualification_input.py"
            assert change["before_file_sha256"] is None
            continue
        selected = []
        for name, delta in change["function_delta"].items():
            nodes = [node for node in tree.body if getattr(node, "name", None) == name]
            assert len(nodes) == 1 and fingerprint(nodes[0]) == delta["after"]
            selected.extend(nodes)
        tree.body = [node for node in tree.body if node not in selected]
        assert fingerprint(tree) == change["outside_reviewed_changes_ast_sha256"]


def test_native_command_evidence_successor_changes_only_the_reviewed_parser_and_projection():
    reviewed = support.native_command_evidence_reference()
    for filename, change in reviewed["files"].items():
        raw = source_path(filename).read_bytes()
        historical = (
            support.historical_clock_readback_reference()
            if filename == "closer_native_source_upgrade.py"
            else None
        )
        if historical is not None:
            # The historical reader is an append-only successor.  Retain this
            # prior review's exact selected-method pin while its full-module
            # successor boundary is checked by the dedicated test below.
            assert change["after_file_sha256"] == historical["before_file_sha256"]
            assert change["after_module_ast_sha256"] == historical["before_module_ast_sha256"]
            successor = support.ownership_budget_reference()["files"][filename]
            latest = support.private_git_environment_reference()["files"][filename]
            assert historical["after_file_sha256"] == successor["before_file_sha256"]
            assert successor["after_file_sha256"] == latest["before_file_sha256"]
            assert hashlib.sha256(raw).hexdigest() == latest["after_file_sha256"]
            assert (
                successor["before_top_level_ast"][change["changed_top_level"]]
                == change["after_changed_top_level_ast_sha256"]
            )
        else:
            assert hashlib.sha256(raw).hexdigest() == change["after_file_sha256"]
        tree = ast.parse(raw)
        selected = next(
            node for node in tree.body if getattr(node, "name", None) == change["changed_top_level"]
        )
        expected = (
            latest["function_delta"][change["changed_top_level"]]["after"]
            if historical is not None
            else change["after_changed_top_level_ast_sha256"]
        )
        assert fingerprint(selected) == expected
        if historical is None:
            assert fingerprint(tree) == change["after_module_ast_sha256"]
            tree.body.remove(selected)
            assert fingerprint(tree) == change["outside_changed_top_level_ast_sha256"]


def test_historical_clock_readback_successor_is_append_only_and_exactly_scoped():
    reviewed = support.historical_clock_readback_reference()
    successor = support.ownership_budget_reference()["files"]["closer_native_source_upgrade.py"]
    assert reviewed["after_file_sha256"] == successor["before_file_sha256"]
    assert reviewed["after_module_ast_sha256"] == successor["before_module_ast_sha256"]
    raw = source_path("closer_native_source_upgrade.py").read_bytes()
    latest = support.private_git_environment_reference()["files"]["closer_native_source_upgrade.py"]
    assert successor["after_file_sha256"] == latest["before_file_sha256"]
    assert hashlib.sha256(raw).hexdigest() == latest["after_file_sha256"]
    tree = ast.parse(raw)
    changed = reviewed["function_delta"]
    for name, expected in changed.items():
        assert successor["before_top_level_ast"][name] == expected["after"]
        nodes = [node for node in tree.body if getattr(node, "name", None) == name]
        assert len(nodes) == 1
        if name not in successor["function_delta"]:
            assert fingerprint(nodes[0]) == expected["after"]
    constants = reviewed["added_constant_ast"]
    selected = []
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        names = {target.id for target in node.targets if isinstance(target, ast.Name)}
        for name, expected in constants.items():
            if name in names:
                assert fingerprint(node) == expected
                selected.append(node)
    assert len(selected) == len(constants)


def test_ownership_budget_successor_preserves_every_unreviewed_ast_family():
    reviewed = support.ownership_budget_reference()
    expected_functions = {
        "closer_native_source_upgrade.py": {
            "_ownership_budget_no_act",
            "_diagnostic_no_act",
            "_reviewed_old_metadata",
            "_historical_ownership_budget_reader_program",
            "_old_diagnostic",
            "_ownership_budget_rebound_coding_policy",
            "_rebased_policy",
            "plan",
        },
        "closer_correction_ownership.py": {"_Reads", "_observation", "collect_ownership"},
    }
    for filename, change in reviewed["files"].items():
        raw = source_path(filename).read_bytes()
        latest = support.private_git_environment_reference()["files"].get(filename)
        if latest is not None:
            assert change["after_file_sha256"] == latest["before_file_sha256"]
        current = latest or change
        assert hashlib.sha256(raw).hexdigest() == current["after_file_sha256"]
        tree = ast.parse(raw)
        assert fingerprint(tree) == current["after_module_ast_sha256"]
        if change.get("added_module"):
            assert filename == "closer_correction_canary_ownership.py"
            assert change["before_file_sha256"] is None
            continue
        assert set(change["function_delta"]) == expected_functions[filename]
        removed = []
        for name, delta in change["function_delta"].items():
            nodes = [node for node in tree.body if getattr(node, "name", None) == name]
            expected = (latest or {}).get("function_delta", {}).get(name, delta)["after"]
            assert len(nodes) == 1 and fingerprint(nodes[0]) == expected
            assert change["before_top_level_ast"].get(name) == delta["before"]
            removed.extend(nodes)
        for name, delta in change["constant_delta"].items():
            nodes = [
                node
                for node in tree.body
                if isinstance(node, ast.Assign)
                and any(
                    isinstance(target, ast.Name) and target.id == name for target in node.targets
                )
            ]
            assert len(nodes) == 1 and fingerprint(nodes[0]) == delta["after"]
            removed.extend(nodes)
        if filename == "closer_correction_ownership.py":
            assert set(change["constant_delta"]) == {"PAGE_SIZE", "MAX_PAGES"}
        else:
            assert all(
                name.startswith("REVIEWED_OWNERSHIP_BUDGET_") for name in change["constant_delta"]
            )
        tree.body = [node for node in tree.body if node not in removed]
        assert fingerprint(tree) == change["outside_reviewed_changes_ast_sha256"]


def _top_level_ast(raw):
    tree = ast.parse(raw)
    return {
        node.name: fingerprint(node)
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.ClassDef))
    }, tree


def test_finite_no_start_v2_successor_changes_only_two_pinned_native_methods():
    reviewed = support.finite_no_start_v2_reference()
    for filename, change in reviewed["files"].items():
        after = source_path(filename).read_bytes()
        successor = support.controlled_initialization_reference()["files"].get(filename)
        later = support.initial_b_observability_reference()["files"].get(filename)
        if successor:
            assert change["after_file_sha256"] == successor["before_file_sha256"]
            if filename == "closer_native_observer.py":
                native = support.standing_publisher_v2_reference()["native_file_successors"][
                    filename
                ]
                assert successor["after_file_sha256"] == native["before_file_sha256"]
                later = support.initial_b_observability_reference()["files"][filename]
                assert native["after_file_sha256"] == later["before_file_sha256"]
                assert hashlib.sha256(after).hexdigest() == later["after_file_sha256"]
                continue
            if later is not None:
                assert change["after_file_sha256"] == later["before_file_sha256"]
                assert hashlib.sha256(after).hexdigest() == later["after_file_sha256"]
                continue
            assert hashlib.sha256(after).hexdigest() == successor["after_file_sha256"]
        else:
            if later is not None:
                assert change["after_file_sha256"] == later["before_file_sha256"]
                assert hashlib.sha256(after).hexdigest() == later["after_file_sha256"]
            else:
                assert hashlib.sha256(after).hexdigest() == change["after_file_sha256"]
        before_top = change["before_top_level_ast"]
        after_top, after_tree = _top_level_ast(after)
        if successor:
            assert after_top == successor["after_top_level_ast"]
            after_top = successor["before_top_level_ast"]
        elif later is not None:
            # The later successor's dedicated whole-module test pins this
            # changed top-level method; this historical fixture has no source
            # text from which to reconstruct its previous map.
            continue
        assert (
            hashlib.sha256(
                json.dumps(before_top, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            == change["before_top_level_ast_sha256"]
        )
        assert (
            hashlib.sha256(
                json.dumps(after_top, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            == change["after_top_level_ast_sha256"]
        )
        scope = change.get("class_method_scope")
        if scope:
            after_class = next(
                node
                for node in after_tree.body
                if isinstance(node, ast.ClassDef) and node.name == scope["class"]
            )
            assert (
                scope["before_remaining_class_ast_sha256"]
                == scope["after_remaining_class_ast_sha256"]
            )
            for node, expected in ((after_class, scope["before_remaining_class_ast_sha256"]),):
                remaining = copy.deepcopy(node)
                remaining.body = [
                    item
                    for item in remaining.body
                    if not (
                        isinstance(item, ast.FunctionDef)
                        and item.name in scope["only_changed_methods"]
                    )
                ]
                assert fingerprint(remaining) == expected
        for name, node_change in change["changed_nodes"].items():
            if "." in name:
                cls, method = name.split(".")
                after_node = next(
                    item
                    for item in next(
                        node
                        for node in after_tree.body
                        if isinstance(node, ast.ClassDef) and node.name == cls
                    ).body
                    if isinstance(item, ast.FunctionDef) and item.name == method
                )
            else:
                before_node, after_node = before_top[name], after_top[name]
                assert before_node == node_change["before_ast_sha256"]
                assert after_node == node_change["after_ast_sha256"]
                continue
            assert fingerprint(after_node) == node_change["after_ast_sha256"]
        changed_top = {name.split(".")[0] for name in change["changed_nodes"]}
        assert {name: value for name, value in after_top.items() if name not in changed_top} == {
            name: value for name, value in before_top.items() if name not in changed_top
        }


def test_current_native_top_level_entry_functions_are_not_backrevved():
    top = functions(source_path("closer_dispatcher.py").read_text())
    for name, expected in support.current_receiver_reference()["native_top_level_ast"].items():
        assert fingerprint(top[name]) == expected, name


def test_native_retained_packet_custody_guard_survives_semantic_interpret_composition():
    run = functions(source_path("closer_dispatcher.py").read_text())["_run_staged_pipeline"]
    interpret = next(
        node for node in run.body if isinstance(node, ast.FunctionDef) and node.name == "interpret"
    )
    block = next(
        node
        for node in ast.walk(interpret)
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Name)
        and node.test.id == "data_only"
        and any(
            isinstance(row, ast.Assign)
            and isinstance(row.value, ast.Call)
            and isinstance(row.value.func, ast.Name)
            and row.value.func.id == "data_only_evidence_context"
            for row in node.body
        )
    )
    assert (
        fingerprint(ast.Module(body=block.body[:2], type_ignores=[]))
        == support.native_reference()["native_custody_guard_ast"]
    )
    call = next(
        row.value
        for row in block.body
        if isinstance(row, ast.Assign)
        and isinstance(row.value, ast.Call)
        and isinstance(row.value.func, ast.Name)
        and row.value.func.id == "data_only_evidence_context"
    )
    selected = next(value.value for value in call.keywords if value.arg == "retained_dir")
    assert isinstance(selected, ast.Name)
    assert selected.id == support.native_reference()["retained_dir_name"] == "ci_retained_root"


def test_native_integration_manifest_is_pinned_not_a_mutable_allowlist(tmp_path, monkeypatch):
    path = tmp_path / "bad.json"
    path.write_bytes(support.MANIFEST.read_bytes().replace(b"0953", b"0000", 1))
    monkeypatch.setattr(support, "MANIFEST", path)
    with pytest.raises(AssertionError):
        support.native_reference()


def test_finite_no_start_v2_successor_fixture_is_pinned(tmp_path, monkeypatch):
    changed = tmp_path / "finite-no-start.json"
    changed.write_bytes(
        support.FINITE_NO_START_V2_MANIFEST.read_bytes().replace(
            b"pre_dispatch_no_start_proof", b"unreviewed_method", 1
        )
    )
    monkeypatch.setattr(support, "FINITE_NO_START_V2_MANIFEST", changed)
    with pytest.raises(AssertionError):
        support.finite_no_start_v2_reference()


def test_controlled_initialization_changes_only_atomic_begin_and_native_demand_guard():
    for filename, change in support.controlled_initialization_reference()["files"].items():
        if filename == "closer_campaign.py":
            successor = support.standing_publisher_v2_reference()
            assert (
                change["after_file_sha256"] == successor["historical_receiver_before_file_sha256"]
            )
            continue
        if filename == "closer_native_observer.py":
            successor = support.standing_publisher_v2_reference()["native_file_successors"][
                filename
            ]
            assert change["after_file_sha256"] == successor["before_file_sha256"]
            continue
        raw = source_path(filename).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == change["after_file_sha256"]
        top, tree = _top_level_ast(raw)
        assert top == change["after_top_level_ast"]
        selected = change["changed_node"].split(".")[0]
        changed_names = {selected, *change.get("additional_changed_top_level_ast", {})}
        assert {k: v for k, v in top.items() if k not in changed_names} == {
            k: v for k, v in change["before_top_level_ast"].items() if k not in changed_names
        }
        for name, pair in change.get("additional_changed_top_level_ast", {}).items():
            assert change["before_top_level_ast"][name] == pair["before"]
            assert top[name] == pair["after"]
        if filename == "closer_campaign.py":
            cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Campaign")
            node = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "begin")
            assert fingerprint(node) == change["after_node_ast_sha256"]
            cls.body = [
                n for n in cls.body if not (isinstance(n, ast.FunctionDef) and n.name == "begin")
            ]
            assert fingerprint(cls) == change["remaining_class_ast_sha256"]
        else:
            assert top[selected] == change["after_node_ast_sha256"]


def test_controlled_initialization_reference_is_pinned(tmp_path, monkeypatch):
    path = tmp_path / "changed.json"
    path.write_bytes(
        support.CONTROLLED_INITIALIZATION_MANIFEST.read_bytes().replace(
            b"Campaign.begin", b"Campaign.other", 1
        )
    )
    monkeypatch.setattr(support, "CONTROLLED_INITIALIZATION_MANIFEST", path)
    with pytest.raises(AssertionError):
        support.controlled_initialization_reference()


def test_standing_publisher_v2_successor_is_exact_and_preserves_other_campaign_ast():
    support.assert_standing_publisher_v2_successor()


def test_standing_publisher_v2_successor_fixture_is_pinned(tmp_path, monkeypatch):
    path = tmp_path / "standing-publisher-v2.json"
    path.write_bytes(
        support.STANDING_PUBLISHER_V2_MANIFEST.read_bytes().replace(
            b'"load_manifest"', b'"unreviewed_function"', 1
        )
    )
    monkeypatch.setattr(support, "STANDING_PUBLISHER_V2_MANIFEST", path)
    with pytest.raises(AssertionError):
        support.standing_publisher_v2_reference()


def test_current_publication_admission_overlay_preserves_unreviewed_ast_families():
    import copy

    reviewed = support.publication_admission_reference()
    assert set(reviewed["dispatcher_top_level_delta"]) == {
        "GitHub",
        "_controlled_launch_inputs",
        "_controlled_launch_options",
        "_controlled_model_contract",
        "_validate_controlled_result",
        "requirements",
    }
    assert set(reviewed["protected_native_modules"]) == {
        "closer_campaign.py",
        "closer_coding_worker.py",
        "closer_correction_controller.py",
        "closer_queue.py",
    }
    for filename, change in reviewed["protected_native_modules"].items():
        if filename == "closer_campaign.py":
            successor = support.standing_publisher_v2_reference()
            assert (
                change["after_file_sha256"] == successor["historical_receiver_before_file_sha256"]
            )
            continue
        top = functions(source_path(filename).read_text())
        before = change["before_top_level_ast"]
        changed = change["changed_top_level_ast"]
        added = change["added_top_level_ast"]
        assert set(top) == set(before) | set(added), filename
        assert {
            name: fingerprint(node)
            for name, node in top.items()
            if name not in changed and name not in added
        } == {name: value for name, value in before.items() if name not in changed}, filename
        for name, pair in changed.items():
            assert before[name] == pair["before"]
            assert fingerprint(top[name]) == pair["after"]
        for name, value in added.items():
            assert fingerprint(top[name]) == value
        if "class_method_scope" in change:
            scope = change["class_method_scope"]
            node = copy.deepcopy(top[scope["class"]])
            node.body = [
                item
                for item in node.body
                if not (
                    isinstance(item, ast.FunctionDef) and item.name in scope["only_changed_methods"]
                )
            ]
            assert fingerprint(node) == scope["remaining_class_ast_sha256"]


def test_current_publication_admission_overlay_is_pinned_not_a_mutable_allowlist(
    tmp_path, monkeypatch
):
    path = tmp_path / "changed.json"
    path.write_bytes(
        support.PUBLICATION_ADMISSION_MANIFEST.read_bytes().replace(
            b'"requirements"', b'"unreviewed"', 1
        )
    )
    monkeypatch.setattr(support, "PUBLICATION_ADMISSION_MANIFEST", path)
    with pytest.raises(AssertionError):
        support.publication_admission_reference()
