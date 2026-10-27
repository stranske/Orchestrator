"""Retained DATA only; not source custody or native-delivery positive."""

import copy

import pytest
from test_closer_coding_publication import custody as custody
from test_closer_publication_handoff_first import handoff_data

import closer_coding_publication as pub
import closer_coding_worker as worker


def sample(custody):
    handoff, reference = handoff_data(custody)
    operation = {"status": "submitted", "submitted_at": 7.25, "deadline": 9.75}
    actual = {"cost": None, "elapsed_seconds": 0.125}
    terminal = handoff["terminal_reference"]
    baseline = {
        "schema": "closer-publication-source-baseline/v1",
        "operation_sha256": pub.digest(operation),
        "stable_slot": handoff["stable_slot"],
        **{k: False for k in pub._AUTHORITY},
    }
    handoff["operation_sha256"] = pub.digest(operation)
    handoff["result_sha256"] = pub.digest(actual)
    handoff["baseline_reference"]["sha256"] = pub.digest(baseline)
    reference["sha256"] = pub.digest(handoff)
    reference["name"] = "publication-source-handoff-" + reference["sha256"] + ".json"
    result = {
        "coding_terminal_reference": terminal,
        "actual": actual,
        "publication_handoff_reference": reference,
        **{k: False for k in worker._NO_AUTHORITY},
    }
    return result, operation, terminal, handoff, baseline


def test_optional_returned_handoff_binds_actual_result_and_original_operation(custody):
    values = sample(custody)
    before = copy.deepcopy(values)
    assert pub.validate_handoff_result_data(*values) == values[0]["publication_handoff_reference"]
    assert values == before


@pytest.mark.parametrize(
    "part", ["actual", "operation", "terminal", "handoff", "baseline", "flag", "extra"]
)
def test_altered_returned_handoff_refuses(custody, part):
    result, operation, terminal, handoff, baseline = sample(custody)
    if part == "actual":
        result["actual"]["elapsed_seconds"] = 0.126
    elif part == "operation":
        operation["deadline"] += 1
    elif part == "terminal":
        terminal = {**terminal, "sha256": "e" * 64}
    elif part == "handoff":
        handoff["stable_slot"] = "f" * 64
    elif part == "baseline":
        baseline["stable_slot"] = "f" * 64
    elif part == "flag":
        result["act_authorized"] = 0
    else:
        result["arbitrary"] = True
    with pytest.raises(pub.PublicationHeld):
        pub.validate_handoff_result_data(result, operation, terminal, handoff, baseline)


def test_known_coding_can_hold_without_issuing_or_adopting_orphan_handoff(custody):
    result, operation, terminal, handoff, baseline = sample(custody)
    result.pop("publication_handoff_reference")
    result["publication_handoff_held"] = True
    assert pub.validate_handoff_result_data(result, operation, terminal, None, None) is None
    with pytest.raises(pub.PublicationHeld, match="orphan"):
        pub.validate_handoff_result_data(result, operation, terminal, handoff, baseline)


def test_detached_source_envelope_reader_cannot_authenticate_mapping():
    with pytest.raises(pub.PublicationHeld, match="actual native coding result reader"):
        pub.validate_native_coding_result({}, {}, {})


def _detached_original_worker_cleanup():
    """Actual cleanup body only; no worker/root/provider authority is invoked."""
    import ast
    from pathlib import Path

    tree = ast.parse(Path(worker.__file__).read_text())
    execute = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_execute")
    cleanup = next(
        n.finalbody
        for n in ast.walk(execute)
        if isinstance(n, ast.Try)
        and n.finalbody
        and any(
            isinstance(c, ast.Call)
            and isinstance(c.func, ast.Attribute)
            and c.func.attr == "close_baseline"
            for part in n.finalbody
            for c in ast.walk(part)
        )
    )
    module = ast.parse(
        "def cleanup(publication, publication_baseline_ref, publication_ref):\n publication_held = False\n"
    )
    function = module.body[0]
    function.body.extend(copy.deepcopy(cleanup))
    function.body.append(ast.parse("return publication_ref, publication_held").body[0])
    namespace = {}
    exec(
        compile(ast.fix_missing_locations(module), "<original-known-handoff-cleanup-data>", "exec"),
        namespace,
    )
    return namespace["cleanup"]


def test_original_cleanup_refusal_preserves_exact_known_handoff_reference():
    class Port:
        @staticmethod
        def close_baseline(_):
            raise OSError("fixed inert cleanup refusal")

    reference = {"schema": "known-handoff-data", "sha256": "a" * 64}
    result, held = _detached_original_worker_cleanup()(Port, object(), reference)
    assert result is reference and held is True


@pytest.mark.parametrize("fatal", [KeyboardInterrupt(), SystemExit(4), GeneratorExit()])
def test_original_cleanup_does_not_swallow_fatal_exceptions(fatal):
    class Port:
        @staticmethod
        def close_baseline(_):
            raise fatal

    reference = {"sha256": "a" * 64}
    with pytest.raises(type(fatal)) as caught:
        _detached_original_worker_cleanup()(Port, object(), reference)
    assert caught.value is fatal
    assert reference == {"sha256": "a" * 64}
