"""Tests for independent provider routing in closer_dispatcher.

Verifies that exception/repair work can be routed to any eligible provider
(Codex, Claude, Gemini, Muse, Cursor, Vibe) excluding Aider.
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest
import test_closer_execution_contract_routes as contract_routes
from test_dispatcher_controlled_data_only import world as _controlled_world

import closer_dispatcher as adapter

controlled_world = _controlled_world

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
        import closer_role_selection as selection
        import execution_profiles

        monkeypatch.setattr(
            selection,
            "candidates_for_agent",
            lambda agent, role, transport, category=None, **kwargs: execution_profiles.profiles_for_agent(
                agent, transport=transport
            ),
        )
        monkeypatch.setattr(adapter, "_profile_exception_qualified", lambda *a, **kw: True)

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
                lambda agent, transport, include_explicit=False: (
                    [mock_profile] if agent != "aider" else []
                ),
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
            lambda agent, transport, include_explicit=False: (
                [mock_profile] if agent != "aider" else []
            ),
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
            lambda agent, transport, include_explicit=False: (
                [mock_profile] if agent == "gemini" else []
            ),
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
        monkeypatch.setattr(
            router,
            "select_agent",
            lambda *args, **kwargs: {"agent": "cursor", "selected_profile_id": "cursor-profile-1"},
        )

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
            lambda agent, transport, include_explicit=False: (
                [mock_profile] if agent != "aider" else []
            ),
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

    @pytest.mark.usefixtures("callback_inert_transport")
    def test_non_codex_exception_callback_identity(self, tmp_path, monkeypatch):
        # The wrapper below runs this assertion inside actual CLI registration.
        from closer_inert_dispatcher_support import SECOND
        from test_closer_disposition import TARGET
        from test_closer_execution_contract_routes import named, prepared, scoped_answer

        delivery, callbacks, context = prepared(tmp_path, monkeypatch)
        row = named(context["interpret"])
        route = {
            "agent": "claude",
            "profile_id": SECOND,
            "pools": ["claude-subscription"],
            "mode": "assess",
        }
        calls = []

        def exception_route(**kwargs):
            load = kwargs.get("load") or {}
            load["claude"] = load.get("claude", 0) + 1
            load["pool:claude-subscription"] = load.get("pool:claude-subscription", 0) + 1
            return route, "available"

        monkeypatch.setattr(adapter, "_exception_route", exception_route)

        def strong(envelope, _prompt, options):
            request = envelope["adjudication_request"]
            calls.append((request, options))
            return {"exit": 0, "output": json.dumps(scoped_answer(request, row))}

        delivery.strong_response = strong
        result = callbacks["exception"](TARGET, context)
        assert result["validated"]["scoped_adjudication"]["outcome"] == "satisfied"
        assert result["requested_agent"] == "claude"
        assert result["requested_profile"] == SECOND
        assert len(calls) == 1 and calls[0][1]["profile_id"] == SECOND
        assert calls[0][0]["basis"]["source"] == row["source"]
        actual = delivery.inert_caller.results[-1]
        assert actual["data_only_origin"]["profile_id"] == SECOND
        assert actual["data_only_origin"]["process_observation"]["agent"] == "claude"
        assert actual["provider_result_ref"]["path"]


@pytest.fixture
def callback_inert_transport(controlled_world, monkeypatch):
    return contract_routes.inert_transport.__wrapped__(controlled_world, monkeypatch)


TestExceptionCallbackIdentity.test_non_codex_exception_callback_identity = (
    contract_routes.native_callback_case(
        TestExceptionCallbackIdentity.test_non_codex_exception_callback_identity
    )
)


class TestInterpretationCapacityDeferral:
    def test_route_reservation_exhaustion_is_a_capacity_deferral(
        self, tmp_path, monkeypatch, controlled_world
    ):
        from test_closer_dispatcher import (
            TARGET,
            FakeGitHub,
            snapshot,
            stub_action_readiness_for_transport_test,
        )

        stub_action_readiness_for_transport_test(monkeypatch)

        import closer_pipeline
        import closer_resources

        postverify = snapshot()
        postverify.update(state="MERGED", merge="d" * 40, base="b" * 40)
        postverify["verifier"] = {
            "schema": "verifier-corpus-decision/v1",
            "verdict": "PASS",
            "repo": TARGET.split("#")[0],
            "pr": int(TARGET.split("#")[1]),
            "head_sha": postverify["head"],
            "evaluated_sha": postverify["merge"],
            "run_id": 34,
            "run_attempt": 1,
            "provider_verdicts": ["PASS"],
            "ci_failed": False,
        }
        fake_gh = FakeGitHub(postverify)
        backend = adapter.Closer(fake_gh, state_path=tmp_path / "state.json")
        # This case begins after admission; never depend on the host's controller.
        monkeypatch.setattr(backend, "campaign_admission", lambda *_args: {"allowed": True})
        from closer_inert_dispatcher_support import InertCaller

        import closer_evidence

        monkeypatch.setenv("HANDOFF_DIR", str(tmp_path / "handoff"))
        monkeypatch.setenv("HOME", str(controlled_world.root / "private-home"))
        caller = InertCaller(controlled_world, monkeypatch)
        caller.activate()
        packet, _ref, _gold = caller.packet(postverify, tmp_path / "state.json.evidence")
        monkeypatch.setattr(closer_evidence, "collect_evidence", lambda *a, **kw: packet)
        monkeypatch.setattr(adapter, "GitHub", lambda **_kwargs: fake_gh)
        callbacks = {}

        def capture(_path, _targets, **kwargs):
            callbacks.update(kwargs)
            return {"targets": {}}

        monkeypatch.setattr(closer_pipeline, "run_pipeline", capture)
        route = {
            "agent": "gemini",
            "profile_id": "gemini-offload-low",
            "pools": ["gemini-pool"],
            "mode": "assess",
        }
        monkeypatch.setattr(adapter, "_pipeline_low_route", lambda **_kwargs: (route, "available"))
        args = SimpleNamespace(state=str(tmp_path / "state.json"), max_workers=1, shadow=False)
        adapter._run_staged_pipeline(args, [TARGET], backend, time.time() + 600)
        deadline = time.time() + 180
        prep = callbacks["prepare"](TARGET, {"deadline": deadline})
        context = {
            "prepare": prep,
            "deadline": deadline,
            "revision": prep["revision"],
            "identity": prep["identity"],
            "work_key": "capacity-deferral",
        }

        reservation_attempts = []

        def blocked(*_args, **kwargs):
            reservation_attempts.append(kwargs["lane"])
            raise closer_resources.ResourceBlocked("capacity changed during reservation")

        monkeypatch.setattr(adapter, "_execute_profile_route", blocked)
        result = callbacks["interpret"](TARGET, context)
        assert reservation_attempts == ["low"]
        assert result["status"] == "deferred"
        assert result["reason"] == "worker_capacity"
        assert result["classification"] == "capacity_deferred"
        assert result["authority"] is False
        assert closer_pipeline._valid_worker_capacity(result["resource_evidence"], time.time())


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
            pools_state={"pool-1": {"state": "ok"}},
        )
        monkeypatch.setattr(adapter, "_fresh_capacity_report", lambda: report)

        # Mock execution_profiles to have a profile for gemini
        mock_profile = {
            "profile_id": "gemini-profile-1",
            "agent": "gemini",
            "capacity_pool_ids": ["pool-1"],
            "reasoning_effort": "medium",
        }
        monkeypatch.setattr(
            execution_profiles,
            "profiles_for_agent",
            lambda agent, transport, include_explicit=False: (
                [mock_profile] if agent == "gemini" else []
            ),
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
        monkeypatch.setattr(
            execution_profiles,
            "profiles_for_agent",
            lambda agent, transport, include_explicit=False: [],
        )

        capacity = adapter._pipeline_capacity()
        assert capacity["exception"] == 0

    @pytest.mark.parametrize(
        ("schema", "profile_ids", "expected"),
        [
            ("closer-operational-capacity/v1", ["claude-explicit"], 1),
            ("closer-operational-capacity/v1", ["other-profile"], 0),
            (None, ["claude-explicit"], 0),
        ],
    )
    def test_explicit_profile_requires_matching_scoped_report(
        self, monkeypatch, schema, profile_ids, expected
    ):
        """A named native scope is the only capacity path that admits explicit profiles."""
        import closer_operational_capacity
        import execution_profiles

        profile = {
            "profile_id": "claude-explicit",
            "agent": "claude",
            "capacity_pool_ids": ["claude-pool"],
            "requires_explicit_selection": True,
        }
        report = _make_capacity_report(
            agents_state={agent: {"state": "shed"} for agent in ELIGIBLE_AGENTS},
            profiles_state={"claude-explicit": {"state": "ok"}},
            pools_state={"claude-pool": {"state": "ok"}},
        )
        report["agents"]["claude"] = {"state": "ok"}
        if schema is not None:
            report["schema"] = schema
            # This unit isolates the dispatcher seam; operational-capacity has
            # its own exact producer/freshness validation tests.
            monkeypatch.setattr(
                closer_operational_capacity, "validate_report", lambda *_a, **_kw: True
            )
        report["profile_ids"] = profile_ids
        monkeypatch.setattr(adapter, "_fresh_capacity_report", lambda: report)
        monkeypatch.setattr(
            execution_profiles,
            "profiles_for_agent",
            lambda agent, transport, include_explicit=False: (
                [profile] if agent == "claude" and include_explicit else []
            ),
        )
        monkeypatch.setattr(adapter, "_profile_exception_qualified", lambda *_a, **_kw: True)

        capacity = adapter._pipeline_capacity()
        assert capacity["exception"] == expected
        assert capacity["low"] == expected


@pytest.fixture(autouse=True)
def synthetic_role_boundary(monkeypatch):
    # Allocator/capacity tests use an explicit synthetic qualification oracle.
    from closer_role_support import install_synthetic_role_verdict

    install_synthetic_role_verdict(monkeypatch)
