"""Independent fleet risk counts stay shadowed until weekly population evidence exists."""

import json

import adversarial as adv
import capabilities
import capability_advisor as advisor
import fleet_shapes
import switch_review


def facts(paths, *, additions=1, deletions=0, labels=()):
    return {
        "paths": paths,
        "files_total": len(paths),
        "changedFiles": len(paths),
        "additions": additions,
        "deletions": deletions,
        "labels": list(labels),
    }


def population(rows, *, now=1_790_000_000):
    prs = [
        {"ref": f"o/r#{i}", "repo": "o/r", "ts": now, "agent": "codex", "durability": "pending"}
        for i in range(len(rows))
    ]
    return fleet_shapes.aggregate(
        prs, {p["ref"]: r for p, r in zip(prs, rows)}, now=now, window_days=60
    )


def test_workflow_and_auth_path_changes_are_high_stakes_and_docs_only_is_not():
    for path in (
        ".github/workflows/gate.yml",
        ".github/scripts/guard.js",
        "src/auth/session.py",
        "src/auth_tokens.py",
        "src/Authentication/session.py",
        "src/authorization/policy.py",
        "src/security/check.py",
        "src/db/store.py",
        "src/data/load.py",
        "src/database/connect.py",
        "src/migrations/upgrade.py",
        "src/persistence/save.py",
        "src/storage/write.py",
        "src/schema/validate.py",
    ):
        assert adv.high_stakes_from_shape(facts([path])), path
    for path_class in ("workflows", "github-meta", "auth", "data"):
        assert adv.high_stakes_from_shape({"path_classes": [path_class]}), path_class
    assert (
        adv.high_stakes_from_shape(
            facts(["docs/auth.md", "docs/database.rst"], additions=1000, deletions=1000)
        )
        is None
    )
    for path in ("tests/test_auth.py", ".agents/data.json", "src/author.py", "src/metadata.py"):
        assert adv.high_stakes_from_shape(facts([path])) is None, path
    assert adv.high_stakes_from_shape(facts(["docs/auth.md", "src/auth/session.py"]))


def test_large_code_change_uses_one_boundary_and_label_route_survives():
    boundary = adv.HIGH_STAKES_MIN_CHANGED_LINES
    for additions, deletions in (
        (boundary, 0),
        (0, boundary),
        (boundary // 2, boundary - boundary // 2),
    ):
        assert adv.high_stakes_from_shape(
            facts(["src/util.py"], additions=additions, deletions=deletions)
        )
        if additions:
            additions -= 1
        else:
            deletions -= 1
        assert (
            adv.high_stakes_from_shape(
                facts(["src/util.py"], additions=additions, deletions=deletions)
            )
            is None
        )
    assert adv.high_stakes_label_reason(facts(["docs/readme.md"], labels=["risk:major"]))
    assert adv.high_stakes_label_reason(
        {**facts(["docs/readme.md"]), "source_labels": [{"name": " HIGH_STAKES "}]}
    )
    assert adv.high_stakes_reason({"lane": "closer", "labels": ["risk:major"]})


def test_weekly_line_counts_the_live_population_and_prints_zero_as_zero(tmp_path):
    now = 1_790_000_000
    data = population(
        [facts([".github/workflows/gate.yml"]), facts(["src/auth.py"]), facts(["docs/readme.md"])],
        now=now,
    )
    (tmp_path / "fleet-shapes.json").write_text(json.dumps(data))
    section = switch_review.adversarial_shape_population(state_dir=tmp_path, now=now)
    assert section["shape_candidates"] == 2 and section["population"] == 3
    assert switch_review.adversarial_shape_line(section) == (
        "adversarial-review: high-stakes candidates 2 of 3 merged PRs (shape rule), 0 by label"
    )
    zero = population([facts(["docs/readme.md"])], now=now)["adversarial_shape"]
    assert switch_review.adversarial_shape_line(zero) == (
        "adversarial-review: high-stakes candidates 0 of 1 merged PRs (shape rule), 0 by label"
    )


def test_full_paths_keep_risk_that_top_three_shape_classes_omit():
    paths = [f"docs/{i}.md" for i in range(5)] + [f"tests/test_{i}.py" for i in range(4)]
    paths += [f"scripts/{i}.py" for i in range(3)] + ["src/auth/session.py"]
    assert "code" not in fleet_shapes.shape_signature("fix", [], paths)["path_classes"]
    assert population([facts(paths)])["adversarial_shape"]["shape_candidates"] == 1


def test_missing_and_truncated_facts_are_partial_not_measured_zero():
    row = facts(["src/util.py"])
    row["files_total"] = 2
    row.pop("changedFiles")
    section = population([row, None])["adversarial_shape"]
    assert section["status"] == "partial" and section["unknown"] == 2
    assert "2 PRs unmeasured" in switch_review.adversarial_shape_line(section)


def test_old_missing_different_rule_and_future_artifacts_are_unknown(tmp_path):
    now = 1_790_000_000
    p = tmp_path / "fleet-shapes.json"
    assert (
        switch_review.adversarial_shape_population(state_dir=tmp_path, now=now)["status"]
        == "unknown"
    )
    for stamp in (now - 3 * 86400, now + 1):
        p.write_text(json.dumps(population([], now=stamp)))
        assert (
            switch_review.adversarial_shape_population(state_dir=tmp_path, now=now)["status"]
            == "unknown"
        )
    data = population([], now=now)
    data["adversarial_shape"]["rule"] = "old-rule"
    p.write_text(json.dumps(data))
    assert (
        switch_review.adversarial_shape_population(state_dir=tmp_path, now=now)["status"]
        == "unknown"
    )


def ledger(tmp_path):
    p = tmp_path / "caps.json"
    capabilities.save(
        {"adversarial-review": capabilities._blank_capability("adversarial-review")}, p
    )
    return p


def test_weekly_evidence_is_deduplicated_and_never_credits_invocation(tmp_path, monkeypatch):
    p = ledger(tmp_path)
    now = 1_790_000_000
    section = population([])["adversarial_shape"]
    monkeypatch.delenv("ORCH_CAPABILITY_HEARTBEATS", raising=False)
    assert not adv.record_shape_measurement(section, now=now, path=p)
    monkeypatch.setenv("ORCH_CAPABILITY_HEARTBEATS", "1")
    assert adv.record_shape_measurement(section, now=now, path=p)
    assert not adv.record_shape_measurement(section, now=now + 1, path=p)
    row = capabilities.load_declared(p)["adversarial-review"]
    assert row["last_invocation"] in (None, 0)
    assert [e["type"] for e in row["event_history"]] == ["match"]
    assert not adv.shape_shadow_readiness(path=p, now=now)[0]


def test_probe_is_unknown_until_two_weekly_counts_and_then_uses_shape_or_label(
    tmp_path, monkeypatch
):
    p = ledger(tmp_path)
    now = 1_790_000_000
    monkeypatch.setattr(capabilities, "REG", p)
    monkeypatch.setattr(adv.time, "time", lambda: now)
    monkeypatch.setenv("ORCH_CAPABILITY_HEARTBEATS", "1")
    wf = facts([".github/workflows/gate.yml"])
    assert advisor._probe_high_stakes_shape(wf)[0] is None
    for stamp in (now - adv.SHAPE_SHADOW_WEEK_SECONDS, now):
        adv.record_shape_measurement(population([])["adversarial_shape"], now=stamp, path=p)
    assert advisor._probe_high_stakes_shape(wf)[0] is True
    assert advisor._probe_high_stakes_shape(facts(["docs/readme.md"]))[0] is False
    assert (
        advisor._probe_high_stakes_shape(facts(["docs/readme.md"], labels=["risk:major"]))[0]
        is True
    )
    assert advisor._probe_high_stakes_shape({})[0] is None


def test_same_week_partial_future_stale_and_rule_change_do_not_mature(tmp_path, monkeypatch):
    p = ledger(tmp_path)
    now = 1_790_000_000
    section = population([])["adversarial_shape"]
    monkeypatch.setenv("ORCH_CAPABILITY_HEARTBEATS", "1")
    assert not adv.record_shape_measurement({**section, "status": "partial"}, now=now, path=p)
    adv.record_shape_measurement(section, now=now, path=p)
    adv.record_shape_measurement(section, now=now + adv.SHAPE_SHADOW_WEEK_SECONDS, path=p)
    assert not adv.shape_shadow_readiness(path=p, now=now)[0]
    assert adv.shape_shadow_readiness(path=p, now=now + adv.SHAPE_SHADOW_WEEK_SECONDS)[0]
    assert not adv.shape_shadow_readiness(path=p, now=now + 3 * adv.SHAPE_SHADOW_WEEK_SECONDS)[0]
    monkeypatch.setattr(adv, "HIGH_STAKES_MIN_CHANGED_LINES", adv.HIGH_STAKES_MIN_CHANGED_LINES + 1)
    assert not adv.shape_shadow_readiness(path=p, now=now + adv.SHAPE_SHADOW_WEEK_SECONDS)[0]


def test_rendered_weekly_report_consumes_the_population_line():
    rep = {
        "review_days": 7,
        "raise_count": 0,
        "held_off": [],
        "on_but_idle": [],
        "unconditioned": [],
        "adversarial_shape": population([])["adversarial_shape"],
    }
    assert (
        "high-stakes candidates 0 of 0 merged PRs (shape rule), 0 by label"
        in switch_review.format_report(rep)
    )


def test_weekly_cli_records_measurement_from_its_own_report(monkeypatch, capsys):
    monkeypatch.setenv("ORCH_VALUE_CHAIN_MONITOR", "0")
    section = population([])["adversarial_shape"]
    called = []
    monkeypatch.setattr(
        switch_review,
        "review",
        lambda **kw: {"generated_at": 1_790_000_000, "adversarial_shape": section},
    )
    monkeypatch.setattr(
        adv, "record_shape_measurement", lambda s, **kw: called.append((s, kw)) or True
    )
    assert switch_review.main(["--json", "--env", "process"]) == 0
    assert called == [(section, {"now": 1_790_000_000})]
    assert json.loads(capsys.readouterr().out)["adversarial_shape_measurement_recorded"]


def test_explicit_advisor_ledger_cannot_borrow_live_shadow_evidence(tmp_path, monkeypatch):
    mature = ledger(tmp_path / "mature")
    fresh = ledger(tmp_path / "fresh")
    now = 1_790_000_000
    monkeypatch.setenv("ORCH_CAPABILITY_HEARTBEATS", "1")
    monkeypatch.setattr(adv.time, "time", lambda: now)
    for stamp in (now - adv.SHAPE_SHADOW_WEEK_SECONDS, now):
        adv.record_shape_measurement(population([])["adversarial_shape"], now=stamp, path=mature)
    monkeypatch.setattr(capabilities, "REG", mature)
    verdict = advisor.evaluate_precondition(
        "adversarial-review",
        repository="stranske/Orchestrator",
        pr_facts=facts([".github/workflows/gate.yml"]),
        ledger_path=fresh,
    )
    assert verdict["pr_requirement_met"] is None
    assert "0/2" in verdict["pr_requirement_evidence"]


def test_truncated_positive_workflow_fact_cannot_record_a_complete_week():
    row = facts([".github/workflows/gate.yml"])
    row["files_total"] = row["changedFiles"] = 2
    section = population([row])["adversarial_shape"]
    assert section["status"] == "partial"
    assert section["unknown"] == 1
    assert section["shape_candidates"] == 0


def test_mature_probe_preserves_security_title_without_review_heartbeat(monkeypatch):
    monkeypatch.setattr(adv, "shape_shadow_readiness", lambda **kw: (True, "two weeks"))
    monkeypatch.setattr(
        adv,
        "_capability_heartbeat",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("review invocation")),
    )
    row = {**facts(["src/util.py"], additions=30), "title": "security fix"}
    verdict, reason = advisor._probe_high_stakes_shape(row)
    assert verdict is True and "title match" in reason


def test_shared_graphql_fact_preserves_source_risk_labels(monkeypatch):
    queries = []
    raw = {
        "number": 4,
        "title": "small utility fix",
        "labels": {"nodes": []},
        "files": {
            "totalCount": 1,
            "nodes": [{"path": "src/util.py", "additions": 3, "deletions": 0}],
        },
        "closingIssuesReferences": {
            "pageInfo": {"hasNextPage": False},
            "nodes": [
                {"labels": {"pageInfo": {"hasNextPage": False}, "nodes": [{"name": "risk:major"}]}}
            ],
        },
    }

    def fetch(args):
        queries.append(args[-1])
        return {"data": {"repository": {"p4": raw}}}

    original = fleet_shapes.fetch_facts
    monkeypatch.setattr(
        fleet_shapes, "fetch_facts", lambda repo, nums: original(repo, nums, gh_json=fetch)
    )
    row = advisor._fetch_pr_facts("o/r", 4)
    assert row["source_labels"] == ["risk:major"]
    assert row["source_labels_complete"] is True
    assert "closingIssuesReferences" in queries[0]
    assert population([row])["adversarial_shape"]["label_candidates"] == 1
    monkeypatch.setattr(adv, "shape_shadow_readiness", lambda **kw: (True, "two weeks"))
    assert advisor._probe_high_stakes_shape(row)[0] is True


def test_incomplete_source_labels_keep_negative_advisor_verdict_unknown(monkeypatch):
    monkeypatch.setattr(adv, "shape_shadow_readiness", lambda **kw: (True, "two weeks"))
    row = {**facts(["src/util.py"]), "source_labels_complete": False}
    assert advisor._probe_high_stakes_shape(row)[0] is None
    assert population([row])["adversarial_shape"]["status"] == "partial"
