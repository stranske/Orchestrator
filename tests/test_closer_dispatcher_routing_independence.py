"""Tests for independent provider routing in closer_dispatcher.

Verifies that exception/repair work can be routed to any eligible provider
(Codex, Claude, Gemini, Muse, Cursor, Vibe) excluding Aider.
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

import closer_dispatcher as adapter

# Eligible agents for exception work (excluding Aider)
ELIGIBLE_AGENTS = ("codex", "claude", "gemini", "cursor", "vibe", "muse")


def _make_capacity_report(agents_state=None, profiles_state=None, pools_state=None):
    """Create a mock capacity report."""
    agents = agents_state or {}
    profiles = profiles_state or {}
    pools = pools_state or {}

    # Default all agents to ok state
    for agent in ELIGIBLE_AGENTS:
        if agent not in agents:
            agents[agent] = {"state": "ok"}

    return {
        "agents": agents,
        "profiles": profiles,
        "pools": pools,
        "generated_at": time.time(),
    }


class TestExceptionRoute:
    """Tests for _exception_route function - verifies multi-agent selection."""

    @pytest.fixture(autouse=True)
    def no_live_calls(self, monkeypatch, tmp_path):
        """Prevent live capacity calls."""
        monkeypatch.setenv("HANDOFF_DIR", str(tmp_path / "handoff"))

        def mock_fresh_report():
            return _make_capacity_report()

        monkeypatch.setattr(adapter, "_fresh_capacity_report", mock_fresh_report)

    def test_route_function_exists(self):
        """Verify that _exception_route function exists."""
        assert hasattr(adapter, "_exception_route")
        assert callable(adapter._exception_route)

    def test_uses_router_select_agent(self, monkeypatch):
        """Verify that _exception_route uses router.select_agent."""
        import router

        # Mock router to return a specific agent
        original_select = router.select_agent
        selected_agents = []

        def mock_select(*args, **kwargs):
            selected_agents.append(kwargs.get("only"))
            return {"agent": "claude"}

        monkeypatch.setattr(router, "select_agent", mock_select)

        try:
            # Mock execution_profiles
            import execution_profiles

            mock_profile = {
                "profile_id": "claude-profile-1",
                "agent": "claude",
                "capacity_pool_ids": ["pool-1"],
                "reasoning_effort": "high",
            }
            monkeypatch.setattr(
                execution_profiles,
                "profiles_for_agent",
                lambda agent, transport: [mock_profile] if agent != "aider" else [],
            )
            monkeypatch.setattr(execution_profiles, "get_profile", lambda pid: mock_profile)

            # Also mock capacity to have pools
            monkeypatch.setattr(
                adapter,
                "_fresh_capacity_report",
                lambda: {
                    **_make_capacity_report(),
                    "pools": {"pool-1": {"state": "ok"}},
                    "profiles": {"claude-profile-1": {"state": "ok"}},
                },
            )

            adapter._exception_route()

            # Verify router was called with eligible agents
            assert len(selected_agents) >= 1
            assert "claude" in selected_agents[0]

        finally:
            monkeypatch.setattr(router, "select_agent", original_select)

    def test_excludes_aider_from_eligibility(self, monkeypatch):
        """Verify that Aider is excluded from eligible agents."""
        import execution_profiles
        import router

        # Mock capacity report
        report = _make_capacity_report()
        report["agents"]["aider"] = {"state": "ok"}
        monkeypatch.setattr(adapter, "_fresh_capacity_report", lambda: report)

        # Mock router to track what agents it sees
        seen_only_sets = []

        def mock_select(*args, **kwargs):
            seen_only_sets.append(kwargs.get("only", set()))
            # Return the first agent in the only set
            only = kwargs.get("only")
            if only:
                return {"agent": next(iter(only))}
            return None

        monkeypatch.setattr(router, "select_agent", mock_select)

        # Mock execution_profiles
        mock_profile = {
            "profile_id": "codex-profile-1",
            "agent": "codex",
            "capacity_pool_ids": ["pool-1"],
            "reasoning_effort": "low",
        }
        monkeypatch.setattr(
            execution_profiles,
            "profiles_for_agent",
            lambda agent, transport: [mock_profile] if agent != "aider" else [],
        )
        monkeypatch.setattr(execution_profiles, "get_profile", lambda pid: mock_profile)

        # Add pool to capacity
        report["pools"] = {"pool-1": {"state": "ok"}}
        report["profiles"] = {"codex-profile-1": {"state": "ok"}}

        adapter._exception_route()

        # Verify Aider was not in any eligible set
        for only_set in seen_only_sets:
            assert "aider" not in only_set

    def test_returns_none_when_all_shed(self, monkeypatch):
        """Verify that _exception_route returns None when all agents are shed."""
        # All agents in shed state
        monkeypatch.setattr(
            adapter,
            "_fresh_capacity_report",
            lambda: _make_capacity_report(
                agents_state={agent: {"state": "shed"} for agent in ELIGIBLE_AGENTS}
            ),
        )

        route = adapter._exception_route()
        assert route is None

    def test_selects_mid_mode_for_repair(self, monkeypatch):
        """Verify that repair flag results in mid mode."""
        import execution_profiles
        import router

        mock_profile = {
            "profile_id": "gemini-profile-high",
            "agent": "gemini",
            "capacity_pool_ids": ["pool-1"],
            "reasoning_effort": "high",
        }
        monkeypatch.setattr(
            execution_profiles,
            "profiles_for_agent",
            lambda agent, transport: [mock_profile] if agent == "gemini" else [],
        )
        monkeypatch.setattr(
            execution_profiles,
            "get_profile",
            lambda pid: mock_profile,
        )
        monkeypatch.setattr(
            router,
            "select_agent",
            lambda *args, **kwargs: {
                "agent": "gemini",
                "selected_profile_id": "gemini-profile-high",
            },
        )

        report = _make_capacity_report()
        report["pools"] = {"pool-1": {"state": "ok"}}
        report["profiles"] = {"gemini-profile-high": {"state": "ok"}}
        monkeypatch.setattr(adapter, "_fresh_capacity_report", lambda: report)

        route = adapter._exception_route(repair=True)
        assert route is not None
        assert route["mode"] == "mid"

    def test_selects_assess_mode_for_review(self, monkeypatch):
        """Verify that review (non-repair) uses assess mode."""
        import execution_profiles
        import router

        # Mock router to return an agent
        monkeypatch.setattr(router, "select_agent", lambda *args, **kwargs: {"agent": "cursor", "selected_profile_id": "cursor-profile-1"})

        # Mock execution_profiles
        mock_profile = {
            "profile_id": "cursor-profile-1",
            "agent": "cursor",
            "capacity_pool_ids": ["pool-1"],
            "reasoning_effort": "high",
        }
        monkeypatch.setattr(
            execution_profiles,
            "profiles_for_agent",
            lambda agent, transport: [mock_profile] if agent != "aider" else [],
        )
        monkeypatch.setattr(execution_profiles, "get_profile", lambda pid: mock_profile)

        # Add pool to capacity
        report = _make_capacity_report()
        report["pools"] = {"pool-1": {"state": "ok"}}
        report["profiles"] = {"cursor-profile-1": {"state": "ok"}}
        monkeypatch.setattr(adapter, "_fresh_capacity_report", lambda: report)

        route = adapter._exception_route(repair=False)
        assert route is not None
        assert route["mode"] == "assess"


class TestExceptionCallbackIdentity:
    """Pipeline exception callback must preserve router-selected provider identity."""

    def test_non_codex_exception_callback_identity(self, tmp_path, monkeypatch):
        from test_closer_dispatcher import TARGET, FakeGitHub, snapshot

        import closer_pipeline

        current = snapshot()
        fake_gh = FakeGitHub(current)
        route = {
            "agent": "claude",
            "profile_id": "claude-offload-assess",
            "pools": ["claude-subscription"],
            "mode": "assess",
        }
        worker_calls = []

        def exception_route(**kwargs):
            load = kwargs.get("load") or {}
            load["claude"] = load.get("claude", 0) + 1
            load["pool:claude-subscription"] = load.get("pool:claude-subscription", 0) + 1
            return (route, "available")

        monkeypatch.setattr(adapter, "_exception_route", exception_route)
        monkeypatch.setattr(adapter, "GitHub", lambda **kw: fake_gh)
        backend = adapter.Closer(
            fake_gh,
            state_path=tmp_path / "state.json",
            apply=True,
            workers=True,
            workspaces={"o/r": str(tmp_path)},
        )

        def worker_stub(target, action, snap, op, **kwargs):
            worker_calls.append(kwargs)
            return {
                "status": "completed",
                "worker": {
                    "exit": 0,
                    "output": json.dumps(
                        {
                            "classification": "uncertain",
                            "substantive_needs": ["needs senior review"],
                        }
                    ),
                },
            }

        monkeypatch.setattr(backend, "worker", worker_stub)
        callbacks = {}

        def capture(_path, _targets, **kwargs):
            callbacks.update(kwargs)
            return {"targets": {}}

        monkeypatch.setattr(closer_pipeline, "run_pipeline", capture)
        args = SimpleNamespace(state=str(tmp_path / "state.json"), max_workers=1, shadow=False)
        adapter._run_staged_pipeline(args, [TARGET], backend, time.time() + 600)
        prep = callbacks["prepare"](TARGET, {"deadline": time.time() + 180})
        context = {
            "prepare": prep,
            "deadline": time.time() + 180,
            "revision": prep["revision"],
            "work_key": "non-codex-exception",
        }
        result = callbacks["exception"](
            TARGET,
            {**context, "interpret": {"proposal": {}, "substantive_needs": []}},
        )
        assert result["requested_agent"] == "claude"
        assert result["requested_profile"] == "claude-offload-assess"
        assert worker_calls[0]["profile_id"] == "claude-offload-assess"


class TestPipelineCapacity:
    """Tests for _pipeline_capacity checking all eligible agents."""

    @pytest.fixture(autouse=True)
    def no_live_calls(self, monkeypatch, tmp_path):
        """Prevent live capacity calls."""
        monkeypatch.setenv("HANDOFF_DIR", str(tmp_path / "handoff"))

    def test_checks_all_eligible_agents_for_exception(self, monkeypatch):
        """Verify that _pipeline_capacity checks all eligible agents for exception lane."""
        import execution_profiles

        # Mock capacity report with only gemini available
        report = _make_capacity_report(
            agents_state={
                "codex": {"state": "shed"},
                "claude": {"state": "shed"},
                "gemini": {"state": "ok"},
                "cursor": {"state": "shed"},
                "vibe": {"state": "shed"},
                "muse": {"state": "shed"},
            },
            profiles_state={"gemini-profile-1": {"state": "ok"}},
        )
        monkeypatch.setattr(adapter, "_fresh_capacity_report", lambda: report)

        # Mock execution_profiles to have a profile for gemini
        mock_profile = {
            "profile_id": "gemini-profile-1",
            "agent": "gemini",
            "capacity_pool_ids": ["pool-1"],
            "reasoning_effort": "low",
        }
        monkeypatch.setattr(
            execution_profiles,
            "profiles_for_agent",
            lambda agent, transport: [mock_profile] if agent == "gemini" else [],
        )

        capacity = adapter._pipeline_capacity()
        assert capacity["exception"] == 1  # Should find gemini

    def test_exception_zero_when_no_eligible_agents(self, monkeypatch):
        """Verify that exception is 0 when no eligible agents have capacity."""
        import execution_profiles

        # All eligible agents shed
        monkeypatch.setattr(
            adapter,
            "_fresh_capacity_report",
            lambda: _make_capacity_report(
                agents_state={agent: {"state": "shed"} for agent in ELIGIBLE_AGENTS}
            ),
        )

        # Mock execution_profiles
        monkeypatch.setattr(execution_profiles, "profiles_for_agent", lambda agent, transport: [])

        capacity = adapter._pipeline_capacity()
        assert capacity["exception"] == 0
