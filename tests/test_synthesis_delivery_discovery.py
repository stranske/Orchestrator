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

import fcntl
import functools
import sqlite3
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


def _pr(number, body, repo="owner/repo"):
    return {
        "number": number,
        "body": body,
        "url": f"https://api.github.com/repos/{repo}/pulls/{number}",
        "base": {"repo": {"full_name": repo}},
    }


def _page(prs, *, total=None, incomplete=False, repo="owner/repo"):
    return {
        "total_count": len(prs) if total is None else total,
        "incomplete_results": incomplete,
        "items": [
            {
                "number": n,
                "body": body,
                "pull_request": {"url": _pr(n, body, repo)["url"]},
            }
            for n, body in prs
        ],
    }


def _search(*prs: tuple[int, str], repo="owner/repo", current=None):
    """Realistic complete search pages and separately injectable current PR bodies."""

    def gh(argv):
        assert argv[:2] == ["gh", "api"], argv
        if "search/issues" in argv:
            page = int(next((a[5:] for a in argv if a.startswith("page=")), "1"))
            return _page(prs[(page - 1) * 100 : page * 100], total=len(prs), repo=repo)
        number = int(argv[-1].rsplit("/", 1)[1])
        assert argv[-1] == f"repos/{repo}/pulls/{number}", argv
        bodies = dict(prs) if current is None else current
        body = bodies[number]
        if isinstance(body, Exception):
            raise body
        return _pr(number, body, repo)

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
    root_run = _synth_run(exp)
    resume_run = f"{exp.name}:synth:resume"
    feedback.record_run(resume_run, "owner/repo [resume]", "synthesize", "codex")
    state["synthesis"].update({"current_run_id": resume_run, "run_id": resume_run})
    promotion._atomic_json(promotion.state_path(exp), state)
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

    with sqlite3.connect(feedback.DB_PATH) as conn:
        assert conn.execute(
            "SELECT run_id FROM outcomes WHERE durability='durable' ORDER BY run_id"
        ).fetchall() == sorted([(root_run,), (delivery,)])
        assert (
            conn.execute("SELECT count(*) FROM outcomes WHERE run_id=?", (resume_run,)).fetchone()[
                0
            ]
            == 0
        )

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


def _assert_unlinked(exp, gh, status="unavailable"):
    """Exercise the real controller: rejected discovery cannot bind or write to Brain."""
    before = feedback.DB_PATH.read_bytes()
    out = promotion.reconcile(exp, delivery_fn=_discover(gh), now=300)
    assert f"delivery_discovery:{status}" in out["actions"], out["actions"]
    assert out["state"]["delivery_phase"] == "candidate_ready"
    assert not out["state"].get("delivery")
    assert not promotion.load_state(exp).get("delivery")
    assert feedback.DB_PATH.read_bytes() == before
    with sqlite3.connect(feedback.DB_PATH) as conn:
        assert conn.execute("SELECT count(*) FROM outcomes").fetchone()[0] == 0


def test_incomplete_search_never_binds_the_visible_match(brain, tmp_path):
    exp, state = _verified_candidate(tmp_path)
    _synth_run(exp)
    _keepalive_run(77)
    page = _page([(77, state["candidate"]["candidate_id"])], total=2, incomplete=True)
    _assert_unlinked(exp, lambda argv: page)


def test_stale_search_marker_removed_from_current_body_never_binds(brain, tmp_path):
    exp, state = _verified_candidate(tmp_path)
    _synth_run(exp)
    _keepalive_run(77)
    gh = _search((77, state["candidate"]["candidate_id"]), current={77: "Marker removed"})
    _assert_unlinked(exp, gh, "no_pr")


@pytest.mark.parametrize(
    "change",
    [
        {"total_count": 2},  # A truncated page despite incomplete_results=false.
        {"total_count": 1001},  # GitHub only exposes the first 1,000 hits.
        {"total_count": None},
        {"total_count": True},
        {"total_count": -1},
        {"total_count": "1"},
        {"incomplete_results": None},
        {"incomplete_results": 0},
        {"items": None},
        {"items": [None]},
        {"items": [{"number": 77, "body": "marker", "pull_request": {}}]},
    ],
)
def test_unprovable_search_completeness_never_binds(brain, tmp_path, change):
    exp, state = _verified_candidate(tmp_path)
    _synth_run(exp)
    _keepalive_run(77)
    page = _page([(77, state["candidate"]["candidate_id"])])
    page.update(change)
    _assert_unlinked(exp, lambda argv: page)


@pytest.mark.parametrize("missing", ["total_count", "incomplete_results", "items"])
def test_missing_search_metadata_never_binds(brain, tmp_path, missing):
    exp, state = _verified_candidate(tmp_path)
    _synth_run(exp)
    _keepalive_run(77)
    page = _page([(77, state["candidate"]["candidate_id"])])
    del page[missing]
    _assert_unlinked(exp, lambda argv: page)


@pytest.mark.parametrize("response", [None, [], {"message": "Validation Failed"}])
@pytest.mark.parametrize("read", ["search", "current"])
def test_unreadable_api_payload_never_binds(brain, tmp_path, response, read):
    exp, state = _verified_candidate(tmp_path)
    _synth_run(exp)
    _keepalive_run(77)
    gh = _search((77, state["candidate"]["candidate_id"]))

    def unreadable(argv):
        is_search = "search/issues" in argv
        return response if is_search == (read == "search") else gh(argv)

    _assert_unlinked(exp, unreadable)


@pytest.mark.parametrize("second_match", [False, True])
@pytest.mark.parametrize("total", [101, 1000])
def test_discovery_reads_beyond_one_hundred_hits(brain, tmp_path, second_match, total):
    exp, state = _verified_candidate(tmp_path)
    _synth_run(exp)
    _keepalive_run(1)
    cid = state["candidate"]["candidate_id"]
    hits = [
        (n, cid if n == 1 or (n == total and second_match) else "other")
        for n in range(1, total + 1)
    ]
    gh = _search(*hits)
    calls = []

    def tracked(argv):
        calls.append(argv)
        return gh(argv)

    if second_match:
        _assert_unlinked(exp, tracked, "ambiguous_pr")
    else:
        out = promotion.reconcile(exp, delivery_fn=_discover(tracked), now=300)
        assert out["state"]["delivery"]["pr_number"] == 1
    assert len([a for a in calls if "search/issues" in a]) == (total + 99) // 100
    assert len([a for a in calls if a[-1].startswith("repos/")]) == total


@pytest.mark.parametrize("failure", ["error", "incomplete", "changed_total", "duplicate"])
def test_second_page_failure_cannot_turn_first_page_into_unique_delivery(brain, tmp_path, failure):
    exp, state = _verified_candidate(tmp_path)
    _synth_run(exp)
    _keepalive_run(1)
    cid = state["candidate"]["candidate_id"]
    hits = [(n, cid if n == 1 else "other") for n in range(1, 101)]
    first = _page(hits, total=101)
    second = _page([(101, "other")], total=101)
    if failure == "incomplete":
        second["incomplete_results"] = True
    elif failure == "changed_total":
        second["total_count"] = 102
    elif failure == "duplicate":
        second = _page([(1, cid)], total=101)

    def gh(argv):
        assert "search/issues" in argv, "no direct read before the complete set is established"
        if "page=2" not in argv:
            return first
        if failure == "error":
            raise RuntimeError("HTTP 502")
        return second

    _assert_unlinked(exp, gh)


def test_duplicate_search_entries_cannot_prove_completeness(brain, tmp_path):
    exp, state = _verified_candidate(tmp_path)
    _synth_run(exp)
    _keepalive_run(77)
    cid = state["candidate"]["candidate_id"]
    _assert_unlinked(exp, _search((77, cid), (77, cid)))


def test_current_body_is_checked_even_when_search_body_lacks_marker(brain, tmp_path):
    exp, state = _verified_candidate(tmp_path)
    _synth_run(exp)
    _keepalive_run(77)
    cid = state["candidate"]["candidate_id"]
    gh = _search((77, cid), (78, "stale index"), current={77: cid, 78: cid})
    _assert_unlinked(exp, gh, "ambiguous_pr")


@pytest.mark.parametrize("failed_pr", [77, 78])
def test_direct_read_failure_never_binds(brain, tmp_path, failed_pr):
    exp, state = _verified_candidate(tmp_path)
    _synth_run(exp)
    _keepalive_run(77)
    cid = state["candidate"]["candidate_id"]
    current = {77: cid, 78: "other"}
    current[failed_pr] = RuntimeError("HTTP 404")
    gh = _search((77, cid), (78, "other"), current=current)
    _assert_unlinked(exp, gh)


@pytest.mark.parametrize(
    "change", [{"number": 78}, {"number": True}, {"url": "wrong"}, {"base": {}}, {"body": {}}]
)
def test_current_pr_identity_or_unreadable_body_never_binds(brain, tmp_path, change):
    exp, state = _verified_candidate(tmp_path)
    _synth_run(exp)
    _keepalive_run(77)
    cid = state["candidate"]["candidate_id"]
    current = _pr(77, cid)
    current.update(change)
    gh = _search((77, cid))
    _assert_unlinked(exp, lambda argv: gh(argv) if "search/issues" in argv else current)


def test_wrong_current_repository_never_binds(brain, tmp_path):
    exp, state = _verified_candidate(tmp_path)
    _synth_run(exp)
    _keepalive_run(77)
    cid = state["candidate"]["candidate_id"]
    gh = _search((77, cid))
    current = _pr(77, cid)
    current["base"]["repo"]["full_name"] = "owner/other"
    _assert_unlinked(exp, lambda argv: gh(argv) if "search/issues" in argv else current)


@pytest.mark.parametrize(
    "body",
    [
        "{cid}extra",
        "extra{cid}",
        "{cid}-extra",
        "{cid}_extra",
        "{cid}:extra",
        "{cid}.extra",
        "{cid}/extra",
        "{cid}+extra",
        "é{cid}",
    ],
)
def test_extended_identifiers_do_not_name_the_candidate(brain, tmp_path, body):
    exp, state = _verified_candidate(tmp_path)
    _synth_run(exp)
    _keepalive_run(77)
    cid = state["candidate"]["candidate_id"]
    _assert_unlinked(exp, _search((77, body.format(cid=cid))), "no_pr")


@pytest.mark.parametrize(
    "body", ["{cid}", "`{cid}`", "Delivers ({cid})", "\n{cid}\n", "{cid}, delivered"]
)
def test_delimited_current_marker_binds(brain, tmp_path, body):
    exp, state = _verified_candidate(tmp_path)
    _keepalive_run(77)
    cid = state["candidate"]["candidate_id"]
    found = promotion.default_delivery_discovery(state, gh_fn=_search((77, body.format(cid=cid))))
    assert found["status"] == "linked"
    assert found["pr_number"] == 77


def test_discovery_opens_brain_in_read_only_mode(brain, tmp_path, monkeypatch):
    exp, state = _verified_candidate(tmp_path)
    _keepalive_run(77)
    before = feedback.DB_PATH.read_bytes()
    real_connect = sqlite3.connect
    calls = []

    def connect(database, **kwargs):
        calls.append((database, kwargs))
        return real_connect(database, **kwargs)

    monkeypatch.setattr(promotion.sqlite3, "connect", connect)
    found = promotion.default_delivery_discovery(
        state, gh_fn=_search((77, state["candidate"]["candidate_id"]))
    )
    assert found["status"] == "linked"
    assert calls == [(feedback.DB_PATH.resolve().as_uri() + "?mode=ro", {"uri": True})]
    assert feedback.DB_PATH.read_bytes() == before


def test_reconciliation_holds_experiment_lock_through_discovery(brain, tmp_path):
    exp, state = _verified_candidate(tmp_path)
    _keepalive_run(77)
    gh = _search((77, state["candidate"]["candidate_id"]))
    reads = []

    def checked_read(argv):
        with (exp / promotion.LOCK_NAME).open() as lock:
            with pytest.raises(BlockingIOError):
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        reads.append(argv)
        return gh(argv)

    out = promotion.reconcile(exp, delivery_fn=_discover(checked_read), now=300)
    assert out["state"]["delivery"]["pr_number"] == 77
    assert len(reads) == 2, "both search and current PR read must be made under the lock"


def test_a_like_wildcard_in_the_repo_name_matches_only_itself(brain, tmp_path):
    exp, state = _verified_candidate(tmp_path)
    feedback.record_run("keepalive:ownerXrepo#7:codex", "ownerXrepo#7", "implement", "codex")
    state["repo"] = "owner_repo"
    found = promotion.default_delivery_discovery(
        state, gh_fn=_search((7, state["candidate"]["candidate_id"]), repo="owner_repo")
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
