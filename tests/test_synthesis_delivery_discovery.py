"""A verified synthesis candidate is delivered by a PR that names it, and its outcome scores the
capability.

THE DEFECT (2026-10-12). `synthesis_promotion.link_delivery` was the only way across
`candidate_ready`, and nothing called it: 0 of 257 promotions on the live mirror crossed it. And
the synthesis run, where `default_mirror_outcome` lands a winner's delivery, carried no
`abcd-experiment` edge, so even a delivered winner would have scored nothing.

THE RULE THIS PINS. `exp_abcd.followup` consults `default_delivery_discovery` for every
`candidate_ready` promotion. It links only when exactly one PR in the repo names the candidate id
in its body AND the keepalive ingester has recorded exactly one run for that PR, the row
`default_outcome_lookup` reads. Every other case links nothing and says why. A delivered winner's
merge and durability then reach the synthesis run, and `capability_effectiveness` scores it.

The candidate is built by the same fixture `test_synthesis_promotion` uses; runs, outcomes and the
synthesis edge are written by the production writers with exp_abcd's arguments."""

from __future__ import annotations

import functools
import sys
from pathlib import Path

import pytest

import capabilities
import capability_effectiveness as effectiveness
import feedback
import synthesis_promotion as promotion

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_synthesis_promotion import _verified_candidate  # noqa: E402

VERSIONS = {"abcd-experiment": "cv:abcd"}


@pytest.fixture
def brain(monkeypatch, tmp_path):
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "feedback.db")
    monkeypatch.setattr(feedback, "_capability_daily_heartbeat", lambda *a, **k: None)
    monkeypatch.setattr(
        feedback,
        "_resolve_capability_versions",
        lambda ids: [VERSIONS[i] for i in ids] if all(i in VERSIONS for i in ids) else [],
    )
    ledger = tmp_path / "capabilities.json"
    row = capabilities._blank_capability("abcd-experiment")
    row["status"] = "shadow"
    capabilities.save({"abcd-experiment": row}, ledger)
    return ledger


def _search(*prs: tuple[int, str]):
    """A fake `gh api search/issues` answering with these (number, body) hits."""

    def gh(argv):
        assert argv[:2] == ["gh", "api"] and "search/issues" in argv, argv
        return {"items": [{"number": n, "body": body} for n, body in prs]}

    return gh


def _discover(gh):
    return functools.partial(promotion.default_delivery_discovery, gh_fn=gh)


def _synth_run(exp: Path) -> str:
    """The root synthesis run, recorded with the capability as `exp_abcd.synthesize` records it."""
    run_id = f"{exp.name}:synth"
    feedback.record_run(
        run_id,
        f"owner/repo [exp {exp.name} synth]",
        "synthesize",
        "codex",
        experiment_id=exp.name,
        capability_ids=["abcd-experiment"],
        influenced_by_workflow_ids=["exp_abcd"],
        routing_metadata={
            "experiment_identity_version": 2,
            "experiment_member_id": "codex--member-01-codex",
            "synthesis_base_member_id": "codex--member-01-codex",
        },
    )
    return run_id


def _keepalive_run(pr: int, agent: str = "claude") -> str:
    """What the keepalive ingester records for a PR (`keepalive_outcomes._stable_run_id`)."""
    import keepalive_outcomes

    run_id = keepalive_outcomes._stable_run_id("owner/repo", pr, agent)
    feedback.record_run(run_id, f"owner/repo#{pr}", "implement", agent, pr_number=pr)
    return run_id


def test_a_delivered_winner_is_linked_merged_and_scores_the_capability(brain, tmp_path):
    exp, state = _verified_candidate(tmp_path)
    cid = state["candidate"]["candidate_id"]
    _synth_run(exp)
    gh = _search((77, f"Delivers `{cid}` from the experiment."), (78, "mentions synth-candidate"))

    first = promotion.reconcile(exp, delivery_fn=_discover(gh), now=300)
    assert first["state"]["delivery_phase"] == "candidate_ready"
    assert "delivery_discovery:record_pending" in first["actions"], first["actions"]

    delivery = _keepalive_run(77)
    linked = promotion.reconcile(exp, delivery_fn=_discover(gh), now=301)
    assert "delivery_linked:owner/repo#77" in linked["actions"], linked["actions"]
    assert linked["state"]["delivery"]["run_id"] == delivery
    assert linked["state"]["delivery_phase"] == "delegated_or_pr"

    feedback.record_outcome(
        delivery, verifier_verdict="PASS", adjudicated_verdict="PASS", merged=True
    )
    merged = promotion.reconcile(exp, now=302)["state"]
    assert merged["delivery_phase"] == "merged"
    feedback.record_outcome(
        delivery,
        verifier_verdict="PASS",
        adjudicated_verdict="PASS",
        merged=True,
        durability="durable",
    )
    assert promotion.reconcile(exp, now=303)["state"]["delivery_phase"] == "durable"

    rep = effectiveness.measure(path=brain)
    arm = next(r for r in rep["rows"] if r["capability_id"] == "abcd-experiment")["accepted"]
    assert (arm["terminal"], arm["durable"], arm["no_outcome_path"]) == (1, 1, 0), arm


@pytest.mark.parametrize(
    ("hits", "status"),
    (
        ((), "no_pr"),
        (((5, "names synth-candidate:other"),), "no_pr"),
        (((5, "{cid}"), (6, "also {cid}")), "ambiguous_pr"),
    ),
)
def test_no_unique_pr_links_nothing(brain, tmp_path, hits, status):
    exp, state = _verified_candidate(tmp_path)
    cid = state["candidate"]["candidate_id"]
    _keepalive_run(5)
    gh = _search(*((n, body.format(cid=cid)) for n, body in hits))
    out = promotion.reconcile(exp, delivery_fn=_discover(gh), now=300)
    assert out["state"]["delivery_phase"] == "candidate_ready"
    assert f"delivery_discovery:{status}" in out["actions"], out["actions"]
    assert not out["state"].get("delivery")


def test_two_ingested_runs_for_the_pr_link_nothing(brain, tmp_path):
    exp, state = _verified_candidate(tmp_path)
    _keepalive_run(9, "codex")
    _keepalive_run(9, "claude")
    gh = _search((9, state["candidate"]["candidate_id"]))
    out = promotion.reconcile(exp, delivery_fn=_discover(gh), now=300)
    assert "delivery_discovery:ambiguous_record" in out["actions"], out["actions"]
    assert out["state"]["delivery_phase"] == "candidate_ready"


def test_an_unreadable_github_is_reported_never_read_as_no_pr(brain, tmp_path):
    exp, _ = _verified_candidate(tmp_path)

    def down(argv):
        raise RuntimeError("HTTP 502")

    out = promotion.reconcile(exp, delivery_fn=_discover(down), now=300)
    assert "delivery_discovery:unavailable" in out["actions"], out["actions"]


def test_a_like_wildcard_in_the_repo_name_matches_only_itself(brain, tmp_path):
    exp, state = _verified_candidate(tmp_path)
    feedback.record_run("keepalive:ownerXrepo#7:codex", "ownerXrepo#7", "implement", "codex")
    state["repo"] = "owner_repo"
    found = promotion.default_delivery_discovery(
        state, gh_fn=_search((7, state["candidate"]["candidate_id"]))
    )
    assert found["status"] == "record_pending", found


def test_the_candidate_tells_its_deliverer_to_name_it(brain, tmp_path):
    _, state = _verified_candidate(tmp_path)
    body = Path(state["candidate"]["body_path"]).read_text()
    assert f"Name `{state['candidate']['candidate_id']}` in the delivering PR's body" in body


def test_followup_consults_discovery_by_default():
    import inspect

    import exp_abcd

    src = inspect.getsource(exp_abcd.followup)
    needle = "promotion_delivery_fn or " + "synthesis_promotion.default_delivery_discovery"
    assert src.count(needle) == 1, "followup must consult delivery discovery by default"
    assert src.count("delivery_fn=" + "delivery_fn") == 2, "both reconcile calls consult it"


def test_the_root_synthesis_run_carries_the_capability():
    import inspect

    import exp_abcd

    needle = 'capability_ids=["abcd-' + 'experiment"]'
    assert inspect.getsource(exp_abcd.synthesize).count(needle) == 1
    assert (
        inspect.getsource(exp_abcd._resume_synthesis_promotion).count(needle) == 0
    ), "the resume run is not the outcome anchor; an edge there would await forever"
