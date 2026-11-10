import feature_goals
import keepalive_supervisor


def test_goal_drift_issues_are_held_from_closer_closure():
    """Only the goals ledger closes its drift issues (Orchestrator#720 flapped daily otherwise)."""
    assert feature_goals.GOAL_DRIFT_LABEL in keepalive_supervisor.CLOSURE_HOLD_LABELS
