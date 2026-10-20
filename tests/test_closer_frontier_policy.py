import pytest

from closer_frontier_policy import (
    POLICY,
    action_identity,
    can_start_new_request,
    make_frontier,
    make_frontier_from_observations,
    request_is_live,
    request_window,
    validate_frontier,
    validate_item_states,
)


def test_frontier_is_an_exact_finite_target_set():
    targets = ["acme/one#1", "acme/two#2"]
    bindings = {
        target: {
            "revision": "rev-1",
            "action_id": action_identity(
                target, "rev-1", "collect_evidence", {"pr_target": target, "source_targets": []}
            ),
        }
        for target in targets
    }
    frontier = make_frontier(targets, owner="owner-a", frontier_id="f1", bindings=bindings)

    assert frontier["schema"] == POLICY
    assert validate_frontier(frontier, targets=["acme/one#1", "acme/two#2"]) == frontier
    with pytest.raises(ValueError, match="target set changed"):
        validate_frontier(frontier, targets=["acme/one#1"])
    with pytest.raises(ValueError, match="cannot repeat"):
        make_frontier(
            ["acme/one#1", "acme/one#1"],
            owner="owner-a",
            frontier_id="f1",
            bindings={"acme/one#1": {"revision": "rev-1", "action_id": "bad"}},
        )
    with pytest.raises(ValueError, match="canonical target"):
        make_frontier(
            ["Acme/One#1", "acme/one#1"],
            owner="owner-a",
            frontier_id="f1",
            bindings={
                "Acme/One#1": {"revision": "rev-1", "action_id": "a" * 64},
                "acme/one#1": {"revision": "rev-1", "action_id": "b" * 64},
            },
        )


def test_frontier_state_cannot_add_targets_or_turn_unknown_into_pending():
    target = "acme/one#1"
    frontier = make_frontier(
        [target],
        owner="owner-a",
        frontier_id="f1",
        bindings={
            target: {
                "revision": "rev-1",
                "action_id": action_identity(
                    target, "rev-1", "merge", {"pr_target": target, "source_targets": []}
                ),
            }
        },
    )

    with pytest.raises(ValueError, match="exactly its immutable targets"):
        validate_item_states(frontier, {"acme/one#1": "pending", "acme/two#2": "pending"})
    with pytest.raises(ValueError, match="unsupported finite frontier state"):
        validate_item_states(frontier, {"acme/one#1": "unknown"})
    assert validate_item_states(frontier, {"acme/one#1": "held"}) == {"acme/one#1": "held"}


def test_frontier_revision_and_action_identity_are_immutable():
    target = "acme/one#1"
    frontier = make_frontier(
        [target],
        owner="owner-a",
        frontier_id="f1",
        bindings={target: {"revision": "rev-1", "action_id": "a" * 64}},
    )
    changed = {**frontier, "bindings": {target: {"revision": "rev-2", "action_id": "b" * 64}}}
    with pytest.raises(ValueError, match="binding digest differs"):
        validate_frontier(changed)


def test_frontier_identity_is_derived_from_existing_queue_observations():
    target = "acme/one#1"
    observed = {
        target: {
            "target": target,
            "revision": "rev-7",
            "action": "merge",
            "eligible": True,
            "operation_binding": {"pr_target": target, "source_targets": ["acme/two#2"]},
        }
    }
    frontier = make_frontier_from_observations(
        observed, owner="owner-a", frontier_id="from-observation"
    )
    assert frontier["targets"] == [target]
    assert frontier["bindings"][target] == {
        "revision": "rev-7",
        "action_id": action_identity(
            target,
            "rev-7",
            "merge",
            {"pr_target": target, "source_targets": ["acme/two#2"]},
        ),
    }
    with pytest.raises(ValueError, match="not actionable"):
        make_frontier_from_observations(
            {target: {**observed[target], "action": "wait", "eligible": False}},
            owner="owner-a",
            frontier_id="from-wait",
        )


def test_elapsed_time_does_not_extend_or_expire_individual_request_window():
    first = request_window(100.0, 25.0)
    second = request_window(1000.0, 25.0)

    assert first == {"started_at": 100.0, "deadline": 125.0}
    assert second == {"started_at": 1000.0, "deadline": 1025.0}
    assert request_is_live(first, 124.999)
    assert not request_is_live(first, 125.0)
    assert not request_is_live(first, 1000.0)


@pytest.mark.parametrize(
    ("status", "cancelled", "paused", "capacity_ok", "expected"),
    [
        ("pending", False, False, True, True),
        ("pending", True, False, True, False),
        ("pending", False, True, True, False),
        ("pending", False, False, False, False),
        ("completed", False, False, True, False),
        ("held", False, False, True, False),
        ("unknown", False, False, True, False),
    ],
)
def test_new_request_gates(status, cancelled, paused, capacity_ok, expected):
    assert (
        can_start_new_request(status, cancelled=cancelled, paused=paused, capacity_ok=capacity_ok)
        is expected
    )


def test_request_window_rejects_unbounded_or_invalid_values():
    for start, timeout in [
        (float("inf"), 1),
        (0, float("nan")),
        (0, 0),
        (True, 1),
        (1e308, 1e308),
        (0, 121),
    ]:
        with pytest.raises(ValueError):
            request_window(start, timeout)
