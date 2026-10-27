"""A feature without selected source/configuration cannot block ordinary ACT.

No successful test here issues publication/closure authority. Selected subjects
still require the original authenticated reader and installed source.
"""

import time

import pytest

import closer_coding_publication as publication
import closer_dispatcher as native
import closer_publication_verification as verification
import closer_queue as queue
import closer_runtime_binding as runtime


def backend(policy=None, *, binding_none=False):
    value = object.__new__(native.Closer)
    value.runtime_binding = None
    if not binding_none:
        value.runtime_binding = object.__new__(runtime.RuntimeBinding)
        value.runtime_binding.policy = {} if policy is None else policy
    return value


def attach(value, snapshot=None, context=None):
    operation = {"status": "submitted", "id": "inert-current", "deadline": time.time() + 5}
    with queue._callback_invocation("o/r#7", operation, time.time):
        return verification.attach_existing_root(
            value, "o/r#7", snapshot or {}, operation, context or {}
        )


@pytest.mark.parametrize(
    "value",
    [
        backend(binding_none=True),
        backend(),
        backend({"codex_metadata_qualification": {"schema": "metadata-only"}}),
        backend({"correction_controller_installation": {"config": {"consumers": {}}}}),
        backend({"correction_controller_installation": {"config": {"consumers": {"c": {}}}}}),
    ],
)
def test_explicit_unselected_feature_leaves_ordinary_act_alone(value):
    assert attach(value) is None


@pytest.mark.parametrize(
    "policy",
    [
        {"correction_controller_installation": None},
        {"correction_controller_installation": {}},
        {"correction_controller_installation": {"config": {"consumers": None}}},
        {"correction_controller_installation": {"config": {"consumers": {"c": None}}}},
        {
            "correction_controller_installation": {
                "config": {"consumers": {"c": {"publication_v1": {}}}}
            }
        },
    ],
)
def test_selected_or_malformed_configuration_requires_existing_reader(policy):
    value = backend(policy)
    assert verification._publication_configuration_selected(value.runtime_binding)
    with pytest.raises((publication.PublicationHeld, verification.ContinuationHeld)):
        attach(value)


@pytest.mark.parametrize("selection", ["backend", "prepare", "native_verifier"])
def test_selected_context_without_binding_cannot_become_an_ordinary_noop(selection):
    value, snapshot, context = backend(binding_none=True), {}, {}
    if selection == "backend":
        value._publication_verification_context = object()
    elif selection == "prepare":
        context = {"prepare": {"corrective_publication_hint": {}}}
    else:
        snapshot = {"verifier": {"corrective_verification_input": {}}}
    with pytest.raises((publication.PublicationHeld, verification.ContinuationHeld)):
        attach(value, snapshot, context)


def test_unselected_subject_does_not_bypass_original_queue_callback():
    with pytest.raises(verification.ContinuationHeld, match="fresh admitted queue callback"):
        verification.attach_existing_root(backend(binding_none=True), "o/r#7", {}, {}, {})
