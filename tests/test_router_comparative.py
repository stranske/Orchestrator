"""Tests for local comparative-advantage routing in router.select_agent (#1433)."""

from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

import router


def _write_export(
    state_dir: Path,
    *,
    route_enabled: object = True,
    ranking: list[dict] | None = None,
    reserve: list[dict] | None = None,
    task_type: str = "review",
) -> Path:
    state_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": "orchestrator.route-weights/v1",
        "comparative": {"route_enabled": route_enabled},
        "task_types": {
            task_type: {
                "evidence_ok": True,
                "ranking": (
                    [{"agent": "codex", "comparative_rank": 1}] if ranking is None else ranking
                ),
            }
        },
        "reserve": {task_type: reserve} if reserve is not None else {},
    }
    target = state_dir / "route-weights-export.json"
    target.write_text(json.dumps(payload), encoding="utf-8")
    return target


def _ordered_picks(task_type: str, cap: dict, learned: dict) -> list[tuple[str, str]]:
    load: dict[str, int] = {}
    picks: list[tuple[str, str]] = []
    for _ in range(len(cap.get("agents", {}))):
        picked = router.select_agent(
            task_type,
            cap,
            learned=learned,
            load=load,
            per_agent_cap=1,
            exploration_rate=0.0,
            simulate=True,
        )
        assert picked is not None
        agent = picked["agent"]
        if load.get(agent, 0) >= 1:
            break
        picks.append((agent, picked["route_source"]))
        load[agent] = load.get(agent, 0) + 1
    return picks


def test_comparative_rank_reorders_within_a_capacity_tier(tmp_path, monkeypatch):
    _write_export(
        tmp_path,
        route_enabled=True,
        ranking=[
            {"agent": "codex", "comparative_rank": 1},
            {"agent": "cursor", "comparative_rank": None},
        ],
    )
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("ORCH_LOCAL_COMPARATIVE", raising=False)
    monkeypatch.setenv("ORCH_EXPLORATION_RATE", "0")

    cap = {"agents": {"cursor": {"state": "ok"}, "codex": {"state": "ok"}}}
    picked = router.select_agent(
        "review",
        cap,
        learned={"cursor": 0, "codex": 1},
        exploration_rate=0.0,
        simulate=True,
    )
    assert picked is not None
    assert picked["agent"] == "codex"
    assert picked["route_source"] == "route_weights_comparative"


def test_flag_off_or_disabled_export_keeps_the_learned_order(tmp_path, monkeypatch):
    cap = {
        "agents": {
            "cursor": {"state": "ok"},
            "vibe": {"state": "ok"},
            "codex": {"state": "ok"},
            "claude": {"state": "ok"},
        }
    }
    learned = {"cursor": 0, "vibe": 1, "codex": 2, "claude": 3}
    expected = [
        ("cursor", "route_weights"),
        ("vibe", "route_weights"),
        ("codex", "route_weights"),
        ("claude", "route_weights"),
    ]

    # 1. ORCH_LOCAL_COMPARATIVE=0 with an enabled comparative export on disk.
    enabled_dir = tmp_path / "enabled"
    _write_export(
        enabled_dir,
        route_enabled=True,
        ranking=[{"agent": "codex", "comparative_rank": 2}],
        reserve=[{"agent": "claude", "comparative_rank": 1}],
    )
    monkeypatch.setenv("ORCH_STATE_DIR", str(enabled_dir))
    monkeypatch.setenv("ORCH_LOCAL_COMPARATIVE", "0")
    assert _ordered_picks("review", cap, learned) == expected

    # 2. Flag default on, but export has route_enabled: false.
    disabled_dir = tmp_path / "disabled"
    _write_export(
        disabled_dir,
        route_enabled=False,
        ranking=[{"agent": "codex", "comparative_rank": 2}],
        reserve=[{"agent": "claude", "comparative_rank": 1}],
    )
    monkeypatch.setenv("ORCH_STATE_DIR", str(disabled_dir))
    monkeypatch.delenv("ORCH_LOCAL_COMPARATIVE", raising=False)
    assert _ordered_picks("review", cap, learned) == expected

    # 3. Flag default on, with no export file present.
    missing_dir = tmp_path / "missing"
    missing_dir.mkdir()
    monkeypatch.setenv("ORCH_STATE_DIR", str(missing_dir))
    monkeypatch.delenv("ORCH_LOCAL_COMPARATIVE", raising=False)
    assert _ordered_picks("review", cap, learned) == expected


def test_capacity_still_outranks_comparative(tmp_path, monkeypatch):
    _write_export(
        tmp_path,
        route_enabled=True,
        ranking=[{"agent": "codex", "comparative_rank": 1}],
    )
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("ORCH_LOCAL_COMPARATIVE", raising=False)

    cap = {"agents": {"cursor": {"state": "ok"}, "codex": {"state": "warn"}}}
    picked = router.select_agent(
        "review",
        cap,
        allow_warn=True,
        learned={"cursor": 1, "codex": 0},
        exploration_rate=0.0,
        simulate=True,
    )
    assert picked is not None
    assert picked["agent"] == "cursor"
    assert picked["route_source"] == "route_weights"

    # Also holds under measured warn-zero pacing on the ranked candidate.
    paced_cap = {
        "agents": {
            "cursor": {"state": "ok"},
            "codex": {
                "state": "warn",
                "pacing": {"state": "warn", "routing_weight": 0.0},
            },
        }
    }
    paced_pick = router.select_agent(
        "review",
        paced_cap,
        allow_warn=True,
        learned={"cursor": 1, "codex": 0},
        exploration_rate=0.0,
        simulate=True,
    )
    assert paced_pick is not None
    assert paced_pick["agent"] == "cursor"


def test_reserve_rank_learned_tiebreak_and_unchanged_winner_route_source(tmp_path, monkeypatch):
    _write_export(
        tmp_path,
        route_enabled=True,
        ranking=[
            {"agent": "codex", "comparative_rank": 2},
            {"agent": "vibe", "comparative_rank": 2},
        ],
        reserve=[{"agent": "claude", "comparative_rank": 1}],
    )
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("ORCH_LOCAL_COMPARATIVE", raising=False)

    cap = {
        "agents": {
            "cursor": {"state": "ok"},
            "vibe": {"state": "ok"},
            "codex": {"state": "ok"},
            "claude": {"state": "ok"},
        }
    }
    learned = {"cursor": 0, "vibe": 1, "codex": 2, "claude": 3}
    # Reserve rank 1 (claude) beats ranking rank 2; learned rank breaks the rank-2 tie
    # (vibe < codex); unranked cursor comes after ranked agents inside the tier.
    assert _ordered_picks("review", cap, learned) == [
        ("claude", "route_weights_comparative"),
        ("vibe", "route_weights_comparative"),
        ("codex", "route_weights_comparative"),
        ("cursor", "route_weights"),
    ]

    # When comparative rank agrees with the baseline winner, route_source stays "route_weights".
    unchanged = router.select_agent(
        "review",
        {"agents": {"claude": {"state": "ok"}, "codex": {"state": "ok"}}},
        learned={"claude": 0, "codex": 1},
        exploration_rate=0.0,
        simulate=True,
    )
    assert unchanged is not None
    assert unchanged["agent"] == "claude"
    assert unchanged["route_source"] == "route_weights"


def test_comparative_rank_reads_export_once_per_process_and_handles_bad_files(
    tmp_path, monkeypatch
):
    cached_dir = tmp_path / "cached"
    export_file = _write_export(
        cached_dir,
        route_enabled=True,
        ranking=[{"agent": "codex", "comparative_rank": 1}],
    )
    monkeypatch.setenv("ORCH_STATE_DIR", str(cached_dir))
    assert router._comparative_rank("review", "codex") == 1

    # Rewrite on disk at the same path: in-process cache must not re-read the file.
    export_file.write_text(json.dumps({"comparative": {"route_enabled": False}}), encoding="utf-8")
    assert router._comparative_rank("review", "codex") == 1

    corrupt_dir = tmp_path / "corrupt"
    corrupt_dir.mkdir()
    (corrupt_dir / "route-weights-export.json").write_text("{not-json", encoding="utf-8")
    monkeypatch.setenv("ORCH_STATE_DIR", str(corrupt_dir))
    assert router._comparative_rank("review", "codex") is None

    nonbool_dir = tmp_path / "nonbool"
    _write_export(
        nonbool_dir,
        route_enabled="true",
        ranking=[{"agent": "codex", "comparative_rank": True}],
    )
    monkeypatch.setenv("ORCH_STATE_DIR", str(nonbool_dir))
    assert router._comparative_rank("review", "codex") is None


@pytest.mark.parametrize("transport", ["local", "remote"])
def test_comparative_export_respects_transport(tmp_path, monkeypatch, transport):
    _write_export(tmp_path)
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ORCH_LOCAL_COMPARATIVE", "1")
    picked = router.select_agent(
        "review",
        {"agents": {"cursor": {"state": "ok"}, "codex": {"state": "ok"}}},
        learned={"cursor": 0, "codex": 1},
        profile_transport=transport,
        exploration_rate=0.0,
        simulate=True,
    )
    assert picked["agent"] == ("codex" if transport == "local" else "cursor")
    assert picked["route_source"] == (
        "route_weights_comparative" if transport == "local" else "route_weights"
    )


@pytest.mark.parametrize("export", ["missing", "disabled", "flag-off", "ineffective", "changed"])
def test_unmeasured_pacing_fallback_attribution(tmp_path, monkeypatch, export):
    if export != "missing":
        _write_export(
            tmp_path,
            route_enabled=export != "disabled",
            ranking=[
                {"agent": "cursor" if export == "changed" else "codex", "comparative_rank": 1}
            ],
        )
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ORCH_LOCAL_COMPARATIVE", "0" if export == "flag-off" else "1")
    # The learned winner is shed. Pacing deterministically falls back to the
    # unmeasured warn tier; comparative evidence may reorder only that tier.
    cap = {
        "agents": {
            "cursor": {"state": "warn"},
            "vibe": {"state": "warn"},
            "codex": {"state": "shed", "pacing": {"state": "shed", "routing_weight": 0.0}},
        }
    }

    class NoDraws:
        def choices(self, *args, **kwargs):
            pytest.fail("deterministic pacing fallback consumed a random draw")

        def random(self):
            pytest.fail("zero exploration consumed a random draw")

    monkeypatch.setattr(router, "_route_profiles", lambda *args: [])
    # Also cover the exact cursor-warn/codex-shed report with no third seat.
    for only in ({"cursor", "codex"}, {"cursor", "vibe", "codex"}):
        picked = router.select_agent(
            "review",
            cap,
            learned={"codex": 0, "vibe": 1, "cursor": 2},
            only=only,
            exploration_rate=0.0,
            rng=NoDraws(),
            simulate=True,
        )
        changed = export == "changed" and "vibe" in only
        assert picked["agent"] == ("cursor" if changed or "vibe" not in only else "vibe")
        assert picked["route_source"] == (
            "route_weights_comparative" if changed else "route_weights"
        )
        assert "pacing_decision" not in picked


@pytest.mark.parametrize("rank", [True, -1, 1.5, None])
def test_enabled_export_rejects_invalid_ranks(tmp_path, monkeypatch, rank):
    _write_export(tmp_path, ranking=[{"agent": "codex", "comparative_rank": rank}])
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    assert router._comparative_rank("review", "codex") is None


@pytest.mark.parametrize("source", ["ORCH_STATE_DIR", "HANDOFF_DIR", "feedback.DB_PATH"])
def test_ambient_export_isolated_but_explicit_override_effective(tmp_path, monkeypatch, source):
    from conftest import _no_live_comparative_export

    monkeypatch.delenv("ORCH_STATE_DIR", raising=False)
    monkeypatch.delenv("HANDOFF_DIR", raising=False)
    ambient = tmp_path / "ambient"
    explicit = tmp_path / "explicit"
    _write_export(ambient)
    _write_export(explicit)

    def set_source(patch, directory):
        if source == "feedback.DB_PATH":
            patch.setattr(router.feedback, "DB_PATH", directory / "feedback.db")
        else:
            patch.setenv(source, str(directory))

    set_source(monkeypatch, ambient)
    # Invoke the real fixture with an ambient redirect present at fixture setup.
    with pytest.MonkeyPatch.context() as isolation:
        _no_live_comparative_export.__wrapped__(isolation, tmp_path / "inner")
        assert router._comparative_rank("review", "codex") is None
        with pytest.MonkeyPatch.context() as override:
            set_source(override, explicit)
            assert router._comparative_rank("review", "codex") == 1
        assert router._comparative_rank("review", "codex") is None


def test_exploration_challenger_pool_uses_baseline_not_comparative(tmp_path, monkeypatch):
    _write_export(
        tmp_path,
        route_enabled=True,
        ranking=[
            {"agent": "codex", "comparative_rank": 1},
            {"agent": "cursor", "comparative_rank": 2},
        ],
    )
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("ORCH_LOCAL_COMPARATIVE", raising=False)

    cap = {"agents": {"cursor": {"state": "ok"}, "codex": {"state": "ok"}}}
    learned = {"cursor": 0, "codex": 1}

    class AlwaysExplore:
        def random(self):
            return 0.0

        def choice(self, seq):
            return seq[0]

        def choices(self, population, weights=None, k=1):
            return [population[0]]

    picked = router.select_agent(
        "review",
        cap,
        learned=learned,
        exploration_rate=1.0,
        rng=AlwaysExplore(),
        simulate=True,
    )
    assert picked is not None
    assert picked["exploration"] is True
    assert picked["agent"] == "codex"
    assert picked["route_source"] == "route_weights"


def test_measured_pacing_weighted_draw_ignores_comparative_order(tmp_path, monkeypatch):
    _write_export(
        tmp_path,
        route_enabled=True,
        task_type="implement",
        ranking=[
            {"agent": "claude", "comparative_rank": 1},
            {"agent": "codex", "comparative_rank": 2},
        ],
    )
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("ORCH_LOCAL_COMPARATIVE", raising=False)

    cap = {
        "agents": {
            "codex": {
                "state": "ok",
                "pacing": {"state": "ok", "routing_weight": 0.5},
            },
            "claude": {
                "state": "ok",
                "pacing": {"state": "ok", "routing_weight": 0.5},
            },
        }
    }
    learned = {
        "codex": {"posterior": 0.5, "score": 0.5, "rank": 0},
        "claude": {"posterior": 0.5, "score": 0.5, "rank": 1},
    }
    comparative_off = tmp_path / "off"
    comparative_off.mkdir()
    monkeypatch.setenv("ORCH_STATE_DIR", str(comparative_off))

    baseline = router.select_agent(
        "implement",
        cap,
        only={"codex", "claude"},
        learned=learned,
        exploration_rate=0.0,
        rng=random.Random(2),
        simulate=True,
    )
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    with_comparative = router.select_agent(
        "implement",
        cap,
        only={"codex", "claude"},
        learned=learned,
        exploration_rate=0.0,
        rng=random.Random(2),
        simulate=True,
    )
    assert baseline is not None and with_comparative is not None
    assert with_comparative["pacing_decision"] is not None
    assert with_comparative["agent"] == baseline["agent"]
    assert with_comparative["route_source"] == "route_weights"


@pytest.mark.parametrize("failure", ["missing", "malformed", "unreadable"])
def test_export_load_diagnostic_once_per_path(tmp_path, monkeypatch, capsys, failure):
    target = tmp_path / router.ROUTE_WEIGHTS_EXPORT_NAME
    if failure == "malformed":
        target.write_text("{not-json", encoding="utf-8")
    elif failure == "unreadable":
        # A directory is deterministically unreadable as an export on CI too.
        target.mkdir()
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    for _ in range(2):
        assert router._comparative_rank("review", "codex") is None
    diagnostic = capsys.readouterr().err
    if failure == "missing":
        assert diagnostic == ""
    else:
        assert diagnostic.count("cannot load comparative export") == 1
        assert str(target) in diagnostic
