import json
import random

import adapters
import dispatcher
import exp_abcd
import judge_reliability


def test_muse_is_on_the_evaluator_roster_but_never_ranked_without_a_record():
    assert "muse" in exp_abcd.EVALUATOR_TOPUP_ORDER and "muse" in exp_abcd.PROBATION_EVALUATORS
    verdicts = {
        "codex": {"scores": {"A": 5, "B": 5}},
        "muse": {"scores": {"A": 10, "B": 0}},
    }
    maps = {"codex": {"A": "m1", "B": "m2"}, "muse": {"A": "m1", "B": "m2"}}
    harvest = exp_abcd._winner_and_harvest(verdicts, maps, reliability={"judges": {}})
    assert harvest["judge_weights"]["muse"] == 0.0
    assert harvest["means"]["m1"] == harvest["means"]["m2"] == 5.0


def test_a_ready_muse_record_counts_at_its_earned_weight():
    verdicts = {"codex": {"scores": {"A": 5}}, "muse": {"scores": {"A": 9}}}
    maps = {"codex": {"A": "m1"}, "muse": {"A": "m1"}}
    reliability = {
        "judges": {"muse": {"ready": True, "weight": 0.5}, "codex": {"ready": True, "weight": 1.0}}
    }
    harvest = exp_abcd._winner_and_harvest(verdicts, maps, reliability=reliability)
    assert harvest["judge_weights"]["muse"] == 0.5


def test_a_default_panel_keeps_its_size_and_one_seat_goes_to_an_unproven_judge():
    for seed in range(20):
        panel = exp_abcd._with_probation(
            ["codex", "cursor", "claude", "vibe"],
            protected={"codex", "cursor"},
            rng=random.Random(seed),
            unproven=["gemini", "muse"],
        )
        assert len(panel) == 4 and panel[:3] == ["codex", "cursor", "claude"]
        assert panel[-1] in ("gemini", "muse")
    seen = {
        exp_abcd._with_probation(
            ["codex", "claude"],
            protected={"codex"},
            rng=random.Random(s),
            unproven=["gemini", "muse"],
        )[-1]
        for s in range(40)
    }
    assert seen == {"gemini", "muse"}


def test_probation_changes_nothing_when_present_unneeded_or_no_seat_is_free():
    assert exp_abcd._with_probation(["claude", "muse"], unproven=["gemini", "muse"]) == [
        "claude",
        "muse",
    ]
    assert exp_abcd._with_probation(["claude"], unproven=[]) == ["claude"]
    assert exp_abcd._with_probation(["codex"], protected={"codex"}, unproven=["muse"]) == ["codex"]


def test_the_default_panel_still_has_four_judges_and_includes_a_probationer(monkeypatch):
    monkeypatch.setattr(exp_abcd, "_unproven_judges", lambda: ["gemini", "muse"])
    specs = exp_abcd._normalize_evaluator_specs(
        requested=None, default_agents=["codex", "cursor"], implementer_agents=["codex", "cursor"]
    )
    agents = [s["agent"] for s in specs]
    assert (
        len(agents) == 4 and {"gemini", "muse"} & set(agents) and {"codex", "cursor"} <= set(agents)
    )


def test_probation_is_only_for_the_default_panel():
    explicit = exp_abcd._normalize_evaluator_specs(
        ["codex"], default_agents=["codex"], implementer_agents=["codex"], min_evaluators=1
    )
    assert [s["agent"] for s in explicit] == ["codex"]


def test_muse_evaluator_command_is_toolless_private_and_reduced_to_terminal_text(
    tmp_path, monkeypatch
):
    # Command construction installs the runtime auth alias; isolate from the owner's login.
    home = tmp_path / "home"
    login = home / ".config" / "muse" / "auth.json"
    login.parent.mkdir(parents=True)
    (login.parents[2] / "Library/Keychains").mkdir(parents=True)
    login.write_text(json.dumps({"providers": {"meta": {"mechanism": "oauth"}}}))
    monkeypatch.setattr(dispatcher, "REAL_HOME", home)
    monkeypatch.setattr(dispatcher, "AGENT_RUNTIME_DIR", tmp_path / "runtime")
    monkeypatch.setattr(adapters, "AGENT_RUNTIME", tmp_path / "runtime")
    prompt = tmp_path / "p.txt"
    prompt.write_text("judge these")
    command = exp_abcd._eval_command("muse", str(prompt), tmp_path / "ws")
    for needle in (
        "--preset empty",
        "--contributor-mode off",
        "--no-session-log",
        "muse_terminal_text",
        ".muse.txt",
    ):
        assert needle in command, needle
    assert "READ-ONLY EVALUATION" in (tmp_path / "p.muse.txt").read_text()
    assert "--yolo" not in command
    assert "unset META_API_KEY" in command  # subscription login only, never the API key


def test_probation_reads_the_ledger_and_treats_an_unreadable_one_as_all_unproven(monkeypatch):
    monkeypatch.setattr(
        judge_reliability, "summarize", lambda: (_ for _ in ()).throw(RuntimeError("x"))
    )
    assert exp_abcd._unproven_judges() == ["gemini", "muse"]
    monkeypatch.setattr(
        judge_reliability, "summarize", lambda: {"judges": {"gemini": {"ready": True}}}
    )
    assert exp_abcd._unproven_judges() == ["muse"]
    assert adapters  # module imported for the mirror layout
