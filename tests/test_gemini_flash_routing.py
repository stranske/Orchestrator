"""Gemini Flash must not receive automatic testgen routing (#1039)."""

import pytest

import router


def test_testgen_never_routes_to_gemini_even_when_draining_agy():
    assert "testgen" not in router.GEMINI_GOOD_FIT_TASKS
    testgen_agents = {entry["agent"] for entry in router.ROUTE_TABLE["testgen"]["agents"]}
    assert "gemini" not in testgen_agents

    cap_drain = {
        "agents": {
            "claude": {"state": "shed"},
            "codex": {"state": "ok"},
            "gemini": {"state": "ok", "policy": "drain"},
            "cursor": {"state": "ok"},
            "vibe": {"state": "ok"},
            "aider": {"state": "ok"},
        }
    }
    selected = router.select_agent("testgen", cap_drain)
    assert selected is not None
    assert selected["agent"] != "gemini"


def test_break_revert_testgen_in_good_fit_tasks():
    original = set(router.GEMINI_GOOD_FIT_TASKS)
    try:
        router.GEMINI_GOOD_FIT_TASKS.add("testgen")
        with pytest.raises(AssertionError):
            test_testgen_never_routes_to_gemini_even_when_draining_agy()
    finally:
        router.GEMINI_GOOD_FIT_TASKS.clear()
        router.GEMINI_GOOD_FIT_TASKS.update(original)
