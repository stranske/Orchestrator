import copy
import json
import time

import pytest

import adapters
import capacity
import dispatcher
import env_prereq
import execution_profiles
import feedback
import ledger_reconcile
import research_subjects
import router


@pytest.fixture
def codex_profile_registry():
    """Deliberate-break fixture named by audit issue 5."""
    return copy.deepcopy(execution_profiles.PROFILE_REGISTRY)


def test_three_profiles_share_one_pool(codex_profile_registry):
    """Codex's three model profiles debit ONE real subscription, not three balances.

    Scoped to codex deliberately. The registry now holds a profile per agent, so a registry-wide
    "exactly one pool" assertion would only re-assert that codex is the only seat -- never the
    property. The property is that multiple profiles of the SAME agent share that agent's real
    account, and it is now asserted for every agent.
    """
    codex_pools = {
        pool_id
        for profile in codex_profile_registry.values()
        if profile["agent"] == "codex"
        for pool_id in profile["capacity_pool_ids"]
    }
    assert len(codex_pools) == 1, "shared subscription counted as 3 pools"
    by_agent: dict[str, set[str]] = {}
    for profile in codex_profile_registry.values():
        by_agent.setdefault(profile["agent"], set()).update(profile["capacity_pool_ids"])
    for agent, pools in by_agent.items():
        assert len(pools) == 1, f"{agent} profiles span {sorted(pools)}"
    # The burn-once property is about ONE agent's profiles sharing ONE real balance, so the events
    # and registry are scoped to codex. Feeding every seat's profile in would test arithmetic across
    # unrelated accounts, not the shared-subscription invariant.
    codex_only = {
        pid: profile for pid, profile in codex_profile_registry.items()
        if profile["agent"] == "codex"
    }
    assert len(codex_only) == 3, sorted(codex_only)
    events = [
        {"selected_profile_id": profile_id, "event": "start", "units": 1}
        for profile_id in codex_only
    ]
    usage = capacity.debit_profile_pools(events, codex_only)
    assert usage == {"codex-subscription": 3.0}
    snapshot = capacity.profile_capacity_snapshot(
        {"agents": {"codex": {"state": "ok"}}},
        pool_usage=usage,
        pool_limits={"codex-subscription": 3},
        registry=codex_only,
    )
    # `profile_capacity_snapshot` reports every real pool, so assert codex's is present and
    # exhausted rather than that it is the only account in existence.
    assert "codex-subscription" in snapshot["pools"], list(snapshot["pools"])
    assert {row["state"] for row in snapshot["profiles"].values()} == {"shed"}


def test_capacity_build_reads_shared_pool_burn_once(tmp_path, monkeypatch, codex_profile_registry):
    ledger = tmp_path / "capacity-ledger.ndjson"
    now = int(time.time())
    rows = []
    for profile_id in codex_profile_registry:
        rows.append({"ts": now, "agent": "codex", "event": "start", "count": 1,
                     "selected_profile_id": profile_id})
        rows.append({"ts": now + 1, "agent": "codex", "event": "complete", "count": 0,
                     "selected_profile_id": profile_id})
    ledger.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    monkeypatch.setattr(capacity, "LEDGER", ledger)
    monkeypatch.setattr(capacity, "SHED_DIR", tmp_path / "shed")
    built = capacity.build(ccusage_block=None)
    assert built["pools"]["codex-subscription"]["used"] == 3.0
    # Only codex burned this pool, and all three of its profiles map to it. Other agents now have
    # their own pools, so filter rather than assert the registry is codex-only.
    codex_rows = {
        pid: row for pid, row in built["profiles"].items()
        if codex_profile_registry.get(pid, {}).get("agent") == "codex"
    }
    assert codex_rows, built["profiles"]
    assert {row["capacity_pool_ids"][0] for row in codex_rows.values()} == {"codex-subscription"}


def test_exact_codex_profile_commands_preserve_permission_rails(monkeypatch):
    # `build_command` with an exact profile resolves the version-capable Codex binary and fails
    # closed if it is absent, rather than falling back to whatever `codex` is on PATH. Nothing
    # about the permission rails can be observed without a command to inspect.
    env_prereq.require(env_prereq.codex_profile_binary_absent())
    monkeypatch.setenv("ORCH_CODEX_BYPASS_INNER_SANDBOX", "0")
    models = set()
    for profile in execution_profiles.profiles_for_agent("codex"):
        command = adapters.build_command(
            "codex", "do work", mode="full", profile=profile, transport="local"
        )
        models.add(command[command.index("--model") + 1])
        assert command[command.index("--sandbox") + 1] == "workspace-write"
        assert command[command.index("-c") + 1] == 'model_reasoning_effort="high"'
        assess = adapters.build_command(
            "codex", "assess", mode="assess", profile=profile,
            transport="offload", permission_mode="read-only",
        )
        assert assess[assess.index("--sandbox") + 1] == "read-only"
        assert "--json" not in assess
    assert models == {"gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"}


def test_nested_sandbox_never_widens_read_only_profile(monkeypatch):
    env_prereq.require(env_prereq.codex_profile_binary_absent())
    monkeypatch.setenv("CODEX_SANDBOX", "seatbelt")
    monkeypatch.delenv("ORCH_CODEX_BYPASS_INNER_SANDBOX", raising=False)
    profile = execution_profiles.get_profile("codex-5.6-sol-high")
    command = adapters.build_command(
        "codex",
        "read only",
        mode="full",
        profile=profile,
        transport="local",
        permission_mode="read-only",
    )
    assert "--dangerously-bypass-approvals-and-sandbox" not in command
    assert command[command.index("--sandbox") + 1] == "read-only"


def test_router_profile_envelope_replays_exact_choice():
    cap = capacity.profile_capacity_snapshot(
        {
            "agents": {
                "codex": {"state": "ok"}, "claude": {"state": "shed"},
                "cursor": {"state": "shed"}, "gemini": {"state": "shed"},
                "vibe": {"state": "shed"}, "aider": {"state": "shed"},
            }
        }
    )
    cap["agents"] = {
        "codex": {"state": "ok"}, "claude": {"state": "shed"},
        "cursor": {"state": "shed"}, "gemini": {"state": "shed"},
        "vibe": {"state": "shed"}, "aider": {"state": "shed"},
    }
    assignment = router.plan(
        [{"target": "owner/repo#5", "task_type": "implement", "lane": "opener"}],
        cap,
        dry_run=True,
    )["assignments"][0]
    replayed = router.replay_profile_choice(assignment)
    assert replayed["selected_profile_id"] == assignment["selected_profile_id"]
    assert replayed["candidate_profile_ids"] == assignment["candidate_profile_ids"]
    assert replayed["rng_seed"] == assignment["profile_rng_seed"]
    assert replayed["policy_version"] == assignment["profile_policy_version"]
    assert replayed["assignment_probability"] == assignment["profile_assignment_probability"]


def test_all_profile_shed_returns_no_route():
    cap = {
        "agents": {"codex": {"state": "ok"}},
        "profiles": {
            profile_id: {"state": "shed"}
            for profile_id in execution_profiles.PROFILE_REGISTRY
        },
    }
    assert router.select_agent(
        "implement",
        cap,
        only={"codex"},
        exploration_rate=0.0,
        profile_seed=1,
        causal_context={"target": "owner/repo#5"},
    ) is None


def test_profile_schema_is_additive_and_immutable(tmp_path, monkeypatch):
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "brain.db")
    feedback.record_run("legacy", "o/r#1", "implement", "codex", mode="local")
    with feedback._conn() as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"execution_profiles", "capacity_pools", "routing_decisions_v2", "route_weights_v2"} <= tables
        assert conn.execute("SELECT COUNT(*) FROM runs WHERE run_id='legacy'").fetchone()[0] == 1
        execution_profiles.ensure_schema(conn)
        # Every registered profile must persist. Comparing to the registry rather than a literal
        # keeps this a real check when a seat is added, instead of a count to bump.
        assert conn.execute("SELECT COUNT(*) FROM execution_profiles").fetchone()[0] == len(
            execution_profiles.PROFILE_REGISTRY)


def _profile_run(run_id, task_type, profile, *, resolved_model, verdict, tmp_ts):
    feedback.record_run(run_id, f"o/r#{run_id}", task_type, "codex", ts=tmp_ts)
    feedback.record_execution_attempt(
        run_id,
        attempt_id=f"profile:{run_id}",
        operation_role="worker",
        profile_id=profile["profile_id"],
        requested_provider=profile["provider"],
        requested_model=profile["requested_model"],
        resolved_provider=profile["provider"],
        resolved_model=resolved_model,
        fallback_reason=("fallback" if resolved_model != profile["requested_model"] else None),
        status="success",
        completed_ts=tmp_ts,
    )
    feedback.record_outcome(
        run_id,
        adjudicated_verdict=verdict,
        merged=verdict == "PASS",
        durability="durable" if verdict == "PASS" else "reverted",
    )


def test_profile_learner_cold_start_and_resolved_coverage_gate(tmp_path, monkeypatch):
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "brain.db")
    feedback.record_run("legacy", "o/r#legacy", "profile-task", "codex")
    feedback.record_outcome("legacy", adjudicated_verdict="PASS", merged=True, durability="durable")
    v1 = feedback.relearn_profiles({"profile-task": {"codex": 0.6}})
    with feedback._conn() as conn:
        cold = execution_profiles.current_profile_weights(conn, "profile-task")
        assert v1 == 1 and all(row["n_obs"] == 0 for row in cold)
        assert all(row["learning_gate_passed"] == 0 for row in cold)
        assert conn.execute("SELECT COUNT(*) FROM execution_attempts WHERE run_id='legacy'").fetchone()[0] == 0
        assert all(abs(row["posterior"] - 0.6) <= execution_profiles.MAX_SUCCESSOR_TRANSFER for row in cold)

    profile = execution_profiles.get_profile("codex-5.6-terra-high")
    for index in range(3):
        _profile_run(
            f"terra-{index}", "profile-task", profile,
            resolved_model=(profile["requested_model"] if index < 2 else "gpt-fallback"),
            verdict="PASS", tmp_ts=2_000_000_000 + index,
        )
    feedback.relearn_profiles({"profile-task": {"codex": 0.6}})
    with feedback._conn() as conn:
        terra = {
            row["profile_id"]: row
            for row in execution_profiles.current_profile_weights(conn, "profile-task")
        }[profile["profile_id"]]
        assert terra["resolved_model_coverage"] == pytest.approx(2 / 3)
        assert terra["learning_gate_passed"] == 0
        assert terra["n_obs"] == 0, "router accepted exact-profile learning below coverage gate"

    _profile_run(
        "terra-3", "profile-task", profile, resolved_model=profile["requested_model"],
        verdict="FAIL", tmp_ts=2_000_000_004,
    )
    feedback.relearn_profiles({"profile-task": {"codex": 0.6}})
    with feedback._conn() as conn:
        terra = {
            row["profile_id"]: row
            for row in execution_profiles.current_profile_weights(conn, "profile-task")
        }[profile["profile_id"]]
        assert terra["resolved_model_coverage"] == pytest.approx(0.75)
        assert terra["learning_gate_passed"] == 0
        assert terra["n_obs"] == 0, "router accepted exact-profile learning below coverage gate"

    _profile_run(
        "terra-4", "profile-task", profile, resolved_model=profile["requested_model"],
        verdict="PASS", tmp_ts=2_000_000_005,
    )
    feedback.relearn_profiles({"profile-task": {"codex": 0.6}})
    with feedback._conn() as conn:
        terra = {
            row["profile_id"]: row
            for row in execution_profiles.current_profile_weights(conn, "profile-task")
        }[profile["profile_id"]]
        assert terra["resolved_model_coverage"] == pytest.approx(0.8)
        assert terra["learning_gate_passed"] == 1
        assert terra["n_obs"] == 4


def test_completion_updates_selected_attempt_only_from_reported_resolution(tmp_path, monkeypatch):
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "brain.db")
    profile = execution_profiles.get_profile("codex-5.6-luna-high")
    feedback.record_run("completion", "o/r#completion", "implement", "codex")
    feedback.record_execution_attempt(
        "completion", attempt_id="attempt:profile:completion", operation_role="worker",
        profile_id=profile["profile_id"], requested_provider=profile["provider"],
        requested_model=profile["requested_model"], status="started",
    )
    with pytest.raises(ValueError, match="actually reported"):
        feedback.complete_profile_attempt(
            "completion", selected_profile_id=profile["profile_id"],
            resolved_provider="openai", resolved_model="",
        )
    feedback.complete_profile_attempt(
        "completion", selected_profile_id=profile["profile_id"],
        resolved_provider="openai", resolved_model="gpt-5.6-luna",
    )
    with feedback._conn() as conn:
        rows = conn.execute(
            "SELECT attempt_id,requested_model,resolved_model,status FROM execution_attempts WHERE run_id='completion'"
        ).fetchall()
    assert rows == [("attempt:profile:completion", "gpt-5.6-luna", "gpt-5.6-luna", "complete")]


def test_wrapper_completion_closes_unreported_model_as_unresolved(tmp_path, monkeypatch):
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "brain.db")
    monkeypatch.setattr(adapters, "HANDOFF", tmp_path / "handoff")
    monkeypatch.setattr(adapters, "LEDGER", tmp_path / "capacity.ndjson")
    profile = execution_profiles.get_profile("codex-5.6-sol-high")
    feedback.record_run("unresolved", "o/r#unresolved", "implement", "codex")
    feedback.record_execution_attempt(
        "unresolved",
        attempt_id="attempt:profile:unresolved",
        operation_role="worker",
        profile_id=profile["profile_id"],
        requested_provider=profile["provider"],
        requested_model=profile["requested_model"],
        status="started",
    )
    ledger_reconcile.record_completion(
        "unresolved",
        "codex",
        selected_profile_id=profile["profile_id"],
        requested_model=profile["requested_model"],
    )
    with feedback._conn() as conn:
        row = conn.execute(
            "SELECT status,requested_model,resolved_provider,resolved_model,fallback_reason "
            "FROM execution_attempts WHERE run_id='unresolved'"
        ).fetchone()
    assert row == (
        "unresolved",
        profile["requested_model"],
        None,
        None,
        "resolved_model_not_reported_by_completion",
    )


def test_profile_dispatch_fails_before_process_when_run_row_cannot_persist(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(dispatcher, "DISPATCH_LOG_DIR", tmp_path / "logs")
    started = []
    ledger = []
    monkeypatch.setattr(
        dispatcher.feedback,
        "record_run",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("db unavailable")),
    )
    monkeypatch.setattr(
        dispatcher.adapters, "record_ledger", lambda *args, **kwargs: ledger.append(kwargs)
    )
    monkeypatch.setattr(
        dispatcher.subprocess,
        "Popen",
        lambda *args, **kwargs: started.append(True),
    )
    with pytest.raises(RuntimeError, match="db unavailable"):
        dispatcher._spawn(
            {
                "agent": "codex",
                "mode": "full",
                "target": "o/r#1",
                "lane": "opener",
                "task_type": "implement",
                "model": "gpt-5.6-sol",
                "cwd": str(tmp_path),
                "wrapped": "true",
                "selected_profile_id": "codex-5.6-sol-high",
                "requested_model": "gpt-5.6-sol",
                "profile_policy_version": execution_profiles.PROFILE_POLICY_VERSION,
                "profile_assignment_probability": 1.0,
                "routing_metadata": {},
            }
        )
    assert not started
    assert not ledger


def test_profile_learning_collapses_same_subject_retries(tmp_path, monkeypatch):
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "brain.db")
    profile = execution_profiles.get_profile("codex-5.6-terra-high")
    with feedback._conn() as conn:
        research_subjects.ensure_schema(conn)
        identity = research_subjects.subject_identity(
            "owner/repo#same",
            "profile-retry",
            "same normalized spec",
            "abc123",
            ["codex"],
            [profile["profile_id"]],
        )
        for index in range(80):
            exp_id = f"same-subject-{index}"
            run_id = f"same-subject-run-{index}"
            research_subjects.record_subject(
                identity,
                lifecycle="evaluated",
                exp_id=exp_id,
                conn=conn,
                now=100 + index,
            )
            conn.execute(
                "INSERT INTO runs "
                "(run_id,ts,target,task_type,agent,experiment_id,assignment) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    run_id,
                    100 + index,
                    "owner/repo#same",
                    "profile-retry",
                    "codex",
                    exp_id,
                    "experimental",
                ),
            )
            conn.execute(
                "INSERT INTO outcomes "
                "(run_id,verifier_verdict,adjudicated_verdict,merged,durability) "
                "VALUES (?,?,?,?,?)",
                (run_id, "PASS", "PASS", 1, "durable"),
            )
            conn.execute(
                "INSERT INTO execution_attempts "
                "(attempt_id,run_id,attempt_ordinal,operation_role,profile_id," 
                "requested_provider,requested_model,resolved_provider,resolved_model," 
                "status,recorded_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    f"attempt-{index}",
                    run_id,
                    1,
                    "worker",
                    profile["profile_id"],
                    profile["provider"],
                    profile["requested_model"],
                    profile["provider"],
                    profile["requested_model"],
                    "success",
                    100 + index,
                ),
            )
        version = execution_profiles.relearn_route_weights_v2(
            conn, {"profile-retry": {"codex": 0.6}}, now=1000
        )
        row = conn.execute(
            "SELECT n_obs,effective_n,learning_gate_passed,posterior,transferred_prior "
            "FROM route_weights_v2 "
            "WHERE version=? AND task_type='profile-retry' AND profile_id=?",
            (version, profile["profile_id"]),
        ).fetchone()
    assert row[0] == 80
    assert row[1] == pytest.approx(1.0), "repeated subject counted as independent"
    assert row[2] == 0
    assert row[3] == pytest.approx(row[4])


def test_profile_report_surfaces_cold_starts_propensity_and_shared_pool(tmp_path, monkeypatch):
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "brain.db")
    # Scoped to codex's three profiles: this test is about propensity across ONE agent's model
    # choices and its shared subscription. Passing the whole registry would make 1/N a statement
    # about how many seats exist, which is not the property being checked.
    codex_candidates = sorted(
        pid for pid, profile in execution_profiles.PROFILE_REGISTRY.items()
        if profile["agent"] == "codex"
    )
    assert len(codex_candidates) == 3, codex_candidates
    envelope = execution_profiles.select_profile(
        "implement", "o/r#report", codex_candidates,
        rng_seed=7, exploration=True, exploration_policy="fixture",
    )
    feedback.record_profile_decision(envelope)
    summary = feedback.profile_routing_summary()
    assert summary["cold_starts"] == 3
    assert summary["routing_decisions"] == 1
    assert summary["mean_assignment_probability"] == pytest.approx(1 / 3)
    # This field reports every REAL account, not "the pools in this decision", so it must equal the
    # pool registry. Asserting a single literal only held while codex was the sole seat.
    assert summary["shared_capacity_pools"] == sorted(execution_profiles.CAPACITY_POOLS)
    assert "codex-subscription" in summary["shared_capacity_pools"]
    # The decision itself was scoped to codex, so only codex's models may appear in it.
    assert {row["requested_model"] for row in summary["profiles"]} == {
        "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"
    }


def test_capacity_pools_match_capacity_agents():
    """The pools are a COPY of capacity.AGENTS; this fails if either side drifts.

    execution_profiles cannot import capacity (capacity imports it), so the account/window facts are
    duplicated by necessity. A pool that misdescribes a real account would mis-report capacity to the
    dispatcher, so the duplication is guarded rather than trusted.
    """
    import capacity
    import execution_profiles
    for pool_id, pool in execution_profiles.CAPACITY_POOLS.items():
        agent = pool.get("agent")
        if not agent or agent not in capacity.AGENTS:
            continue
        truth = capacity.AGENTS[agent]
        assert pool["account"] == truth["account"], (pool_id, pool["account"], truth["account"])
    # And every agent capacity knows about must have a pool, or its seat can never be profiled.
    pooled = {p.get("agent") for p in execution_profiles.CAPACITY_POOLS.values()}
    assert set(capacity.AGENTS) <= pooled, sorted(set(capacity.AGENTS) - pooled)


def test_registry_models_match_adapters():
    """A registry that disagrees with the adapter would request a model the seat never runs.

    The two representations differ ON PURPOSE and this reconciles them rather than flattening one:
    `model_identity` is a DRIFT TAG that keeps the router (`agy:`, `cursor:`) because the seat that
    served the work is metered separately, while a profile's `requested_model` is the bare vendor id
    -- the thing a provider-resolved model is compared against. So the router prefix is stripped
    before comparing, and only the model part must agree.
    """
    import adapters
    import execution_profiles

    def bare(model: str) -> str:
        # Strip a leading `<seat>:` router prefix. Only these seats route through a wrapper; a
        # vendor id containing a slash (`mistral/codestral-latest`) is untouched.
        head, _, tail = str(model).partition(":")
        return tail if tail and head in {"agy", "cursor", "claude", "codex", "vibe", "aider"} else str(model)

    by_agent: dict[str, set[str]] = {}
    for profile in execution_profiles.PROFILE_REGISTRY.values():
        by_agent.setdefault(profile["agent"], set()).add(bare(profile["requested_model"]))
    for agent, models in by_agent.items():
        reported = {bare(adapters.model_identity(agent, mode)) for mode in ("mid", "full")}
        assert models & reported, (
            f"{agent}: registry {sorted(models)} matches none of the adapter identities "
            f"{sorted(reported)}")


def test_every_agent_can_record_a_worker_attempt():
    """One profile per agent -- without it, that seat's work can never carry provenance."""
    import capacity
    import execution_profiles
    for agent in capacity.AGENTS:
        assert execution_profiles.profiles_for_agent(agent, transport="offload"), (
            f"{agent} has no offload profile, so dispatcher.offload cannot record a worker attempt")


def test_vibe_model_matches_local_config():
    """vibe's named model must match the CLI's OWN config, or one of them has drifted.

    `mistral-medium-3.5` is a fact about this machine's vibe install, not a vendor constant, so the
    check reads the real config and SKIPS with the missing file named when vibe is not installed
    (a CI runner). It never guesses.
    """
    import env_prereq
    import adapters
    env_prereq.require(env_prereq.vibe_config_absent())
    configured = env_prereq.vibe_active_model()
    assert configured, "vibe config present but active_model unreadable"
    assert adapters.VIBE_MODEL == configured, (adapters.VIBE_MODEL, configured)
    assert adapters.model_identity("vibe") == configured


def test_named_models_are_not_adapter_tags():
    """A seat whose model is NAMED must not be reported as a routing tag, and vice versa.

    This is the line between "cannot be identified" and "identified but not yet confirmed". Getting
    it wrong in either direction misreports coverage: a tag counted as a model claims provenance
    that cannot exist, and a real model counted as a tag hides a seat that only needs tracing.
    """
    import feedback
    import execution_profiles
    tag_seats, named_seats = set(), set()
    for profile in execution_profiles.PROFILE_REGISTRY.values():
        target = tag_seats if feedback.SYNTHETIC_ADAPTER_MODEL_RE.match(
            str(profile["requested_model"])) else named_seats
        target.add(profile["agent"])
    # NO seat may request a routing tag. Each model is named from that seat's own authority --
    # codex/claude from tier research, gemini from agy's advertised-models probe, cursor from its
    # known default, vibe from its CLI config, aider from its floating alias. A tag here would mean
    # a seat had been quietly reclassified as unidentifiable when it is merely unconfirmed.
    # No PROFILE may request a routing tag -- that is the field compared against a resolved model.
    # `model_identity` legitimately still carries the router prefix; these are different fields.
    assert tag_seats == set(), sorted(tag_seats)
    assert {"codex", "claude", "aider", "vibe", "gemini", "cursor"} <= named_seats, sorted(named_seats)


def test_gemini_model_is_one_the_seat_advertises():
    """gemini's requested model must appear in agy's OWN advertised-models probe.

    Skips with the missing file named when the probe cache is absent (a runner). The point is that
    the id is not guessed: agy publishes what it serves, and drifting off that list would request a
    model the seat cannot route.
    """
    import json
    import adapters
    import env_prereq
    import execution_profiles
    cache = adapters.GEMINI_MODEL_CACHE
    env_prereq.require(None if cache.is_file() else f"agy advertised-models cache absent: {cache}")
    advertised = set((json.loads(cache.read_text()) or {}).get("models") or [])
    assert advertised, f"cache present but lists no models: {cache}"
    for profile in execution_profiles.PROFILE_REGISTRY.values():
        if profile["agent"] == "gemini":
            assert profile["requested_model"] in advertised, (
                profile["requested_model"], sorted(advertised))
