"""Bounded behavioral coverage for issue #1215; evidence is recorded in PR #1224."""

from __future__ import annotations

import base64
import io
import json
import os
import socket
import subprocess
import sys
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import capabilities
import feedback
import rate_incidents
import switch_review


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Keep every mutable registry, incident ledger, and GitHub read inside tmp_path."""
    state = tmp_path / "state"
    runtime = tmp_path / "runtime"
    handoff = tmp_path / "handoff"
    mirror = tmp_path / "mirror"
    for directory in (state, runtime, handoff, mirror):
        directory.mkdir()
    monkeypatch.setenv("ORCH_STATE_DIR", str(state))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(runtime))
    monkeypatch.setenv("HANDOFF_DIR", str(handoff))
    monkeypatch.setenv("HANDOFF_PACING_LOG", str(handoff / "relay-pacing.log"))
    monkeypatch.setattr(feedback, "DB_PATH", runtime / "feedback.db")
    monkeypatch.setattr(feedback, "_capability_daily_heartbeat", lambda *a, **k: None)
    monkeypatch.setattr(rate_incidents, "HANDOFF", handoff)
    monkeypatch.setattr(rate_incidents, "INCIDENT_FILE", handoff / "rate-limit-incidents.ndjson")
    monkeypatch.setattr(rate_incidents, "LOCK_FILE", handoff / "rate-limit-incidents.ndjson.lock")
    monkeypatch.setattr(rate_incidents, "SHED_DIR", handoff / "capacity-shed")
    monkeypatch.setattr(capabilities, "REG", runtime / "capabilities.json")
    monkeypatch.setattr(capabilities, "production_heartbeat", lambda *a, **k: None)
    monkeypatch.setattr(switch_review, "MIRROR_DIR", mirror)

    attempted: list[tuple] = []

    def forbidden_subprocess_or_socket(*args, **kwargs):
        attempted.append((args, kwargs))
        raise AssertionError("issue1215 ranked coverage must not start subprocess or network work")

    def forbidden_gh(args, *, timeout_s: int = 30):
        attempted.append((("gh", *args), {"timeout_s": timeout_s}))
        raise AssertionError("issue1215 ranked coverage must not invoke unmocked _gh_call")

    for name in ("run", "Popen"):
        monkeypatch.setattr(subprocess, name, forbidden_subprocess_or_socket)
    for name in ("socket", "create_connection"):
        monkeypatch.setattr(socket, name, forbidden_subprocess_or_socket)
    monkeypatch.setattr(switch_review, "_GH_CALL_RUNNER", forbidden_gh)
    yield
    assert attempted == [], f"unexpected external entry attempts: {attempted}"


def test_rate_incidents_classify_and_reset_parsing() -> None:
    assert rate_incidents.classify_provider_failure("") == ("unknown", "non_authoritative", "none")
    assert rate_incidents.classify_provider_failure(None) == (  # type: ignore[arg-type]
        "unknown",
        "non_authoritative",
        "none",
    )
    assert rate_incidents.classify_provider_failure(
        "Provider failure: exceeded secondary rate limit on endpoint"
    ) == ("rate_limit", "rate_limit_exhausted", "high")

    expected = int(datetime(2099, 1, 5, 3, 11, tzinfo=timezone.utc).timestamp())
    utc_reset = rate_incidents.parse_relay_reset_at(
        "Try again at Jan 5th, 2099 3:11 AM UTC", now=1_700_000_000
    )
    offset_reset = rate_incidents.parse_relay_reset_at(
        "Try again at Jan 5th, 2099 5:11 AM +0200", now=1_700_000_000
    )
    assert utc_reset == expected
    assert offset_reset == expected
    assert (
        rate_incidents.parse_relay_reset_at(
            "Try again at Feb 30th, 2099 3:11 AM UTC", now=1_700_000_000
        )
        is None
    )
    assert rate_incidents.parse_resets_in_duration("no stated window", now=1_700_000_000) is None
    assert rate_incidents.json_event("{malformed json") is None


def test_rate_incidents_relay_pacing_field_tokens() -> None:
    for invalid in (None, True, "bad", float("nan"), float("inf"), float("-inf")):
        assert rate_incidents._relay_weight_token(invalid) == "unknown"
    assert rate_incidents._relay_weight_token(0.25) == "0.25"
    assert rate_incidents._relay_weight_token(2.0) == "2"

    fields = rate_incidents.relay_log_fields_from_decision(
        {
            "candidates": [
                "not-a-mapping",
                {"agent": "codex", "pacing": {"routing_weight": 0.5, "state": " constrained "}},
                {"agent": "claude", "pacing": {"state": "   "}},
            ],
            "policy": "weighted",
        },
        candidates="codex,claude,cursor",
    )
    assert fields == {
        "weights": "codex:0.5,claude:1,cursor:1",
        "states": "codex:constrained,claude:unknown,cursor:unknown",
        "policy": "weighted",
    }


def test_rate_incidents_record_lane_round_validation_and_telemetry_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out_file = tmp_path / "tail.log"
    out_file.write_text("HTTP status 429: too many requests\n", encoding="utf-8")

    with pytest.raises(ValueError, match="agent must be codex, claude, cursor or muse"):
        rate_incidents.record_lane_round(
            agent="gemini",
            lane="opener",
            receiver_reason=None,
            exit_status=1,
            output_file=out_file,
            ts=1_700_000_000,
        )
    with pytest.raises(ValueError, match="surface must be nonempty"):
        rate_incidents.record_lane_round(
            agent="codex",
            surface="   ",
            lane="opener",
            receiver_reason=None,
            exit_status=1,
            output_file=out_file,
            ts=1_700_000_000,
        )
    oversized = tmp_path / "oversized.log"
    oversized.write_bytes(b"x" * (128 * 1024 + 1))
    for bad_path in (tmp_path / "missing.log", oversized):
        with pytest.raises(ValueError, match="output-file must be a readable tail"):
            rate_incidents.record_lane_round(
                agent="codex",
                lane="opener",
                receiver_reason=None,
                exit_status=1,
                output_file=bad_path,
                ts=1_700_000_000,
            )

    monkeypatch.setattr(feedback, "record_lane_round", lambda *a, **k: "lane-round-1")

    def broken_ingest(*_a, **_k):
        raise OSError("pacing log locked")

    monkeypatch.setattr(feedback, "ingest_relay_pacing", broken_ingest)
    recorded = rate_incidents.record_lane_round(
        agent="codex",
        surface="handoff-relay",
        lane="closer",
        receiver_reason="pr_followup",
        exit_status=1,
        output_file=out_file,
        ts=1_700_000_000,
    )
    assert recorded["decision_id"] == "lane-round-1"
    assert recorded["error_class"] == "http_429"
    assert recorded["relay_pacing"] == {"status": "unavailable", "reason": "pacing log locked"}
    assert recorded["shed"] is True


def test_rate_incidents_dedup_cooldown_and_shed_guards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rate_incidents.INCIDENT_FILE.write_text(
        json.dumps({"idempotency_key": "r1|codex|quota", "incident_id": "inc-1"}) + "\n{not-json\n",
        encoding="utf-8",
    )
    assert rate_incidents._existing_idempotency_key("r1|codex|quota") == "inc-1"

    dir_incident = tmp_path / "incident_dir"
    dir_incident.mkdir()
    monkeypatch.setattr(rate_incidents, "INCIDENT_FILE", dir_incident)
    assert rate_incidents._existing_idempotency_key("r1|codex|quota") is None

    assert rate_incidents._cooldown_seconds("manual_note") is None
    monkeypatch.setenv("ORCH_PROVIDER_COOLDOWN_S", "not-an-int")
    assert rate_incidents._cooldown_seconds("quota") == rate_incidents.DEFAULT_COOLDOWN_S

    rate_incidents._ensure_paths()
    malformed_marker = rate_incidents.SHED_DIR / "codex"
    malformed_marker.write_text("{manual-marker", encoding="utf-8")
    assert (
        rate_incidents.ensure_shed(
            "codex", category="quota", incident_id="inc-2", expires_at=1_900_000_000
        )
        is True
    )
    assert malformed_marker.read_text(encoding="utf-8") == "{manual-marker"

    bool_expiry_marker = rate_incidents.SHED_DIR / "claude"
    bool_expiry_marker.write_text(json.dumps({"expires_at": True}), encoding="utf-8")
    assert (
        rate_incidents.ensure_shed(
            "claude", category="quota", incident_id="inc-3", expires_at=1_900_000_000
        )
        is True
    )
    assert json.loads(bool_expiry_marker.read_text(encoding="utf-8")) == {"expires_at": True}
    assert rate_incidents.check_shed("claude") is True
    assert rate_incidents.clear_shed("claude") is True
    assert rate_incidents.clear_shed("claude") is False
    assert rate_incidents.check_shed("claude") is False

    def fail_replace(_src, _dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", fail_replace)
    assert (
        rate_incidents.ensure_shed(
            "cursor", category="quota", incident_id="inc-4", expires_at=1_900_000_000
        )
        is False
    )
    assert list(rate_incidents.SHED_DIR.glob(".cursor.*")) == []

    with pytest.raises(ValueError, match="agent, surface, and category are required"):
        rate_incidents.record_incident(agent="", surface="cli", category="quota")


def test_rate_incidents_cli_subcommands_and_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert rate_incidents.main([]) == 0
    assert "usage:" in capsys.readouterr().out.lower()

    assert rate_incidents.main(["summary"]) == 0
    assert json.loads(capsys.readouterr().out) == {"by_agent": {}, "by_category": {}, "total": 0}

    assert rate_incidents.main(["classify", "--evidence", "HTTP status 429"]) == 0
    classified = json.loads(capsys.readouterr().out)
    assert classified["category"] == "rate_limit"
    assert classified["subcategory"] == "http_429"
    assert classified["should_shed"] is True

    assert (
        rate_incidents.main(
            ["record", "--agent", "codex", "--surface", "cli", "--evidence", "ordinary task log"]
        )
        == 2
    )
    assert json.loads(capsys.readouterr().out) == {
        "reason": "non_authoritative",
        "status": "ignored",
    }

    assert (
        rate_incidents.main(
            [
                "record",
                "--agent",
                "codex",
                "--surface",
                "cli",
                "--category",
                "quota",
                "--no-shed",
                "--evidence",
                "operator override",
            ]
        )
        == 0
    )
    recorded = json.loads(capsys.readouterr().out)
    assert recorded["status"] == "ok"
    assert recorded["shed"] is False

    with rate_incidents.INCIDENT_FILE.open("a", encoding="utf-8") as handle:
        handle.write("{corrupt-line\n[1, 2, 3]\n")
    assert rate_incidents.main(["summary"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "by_agent": {"codex": 1},
        "by_category": {"quota": 1},
        "total": 1,
    }

    monkeypatch.setattr(sys, "stdin", io.StringIO("[1, 2]"))
    with pytest.raises(SystemExit) as fmt_exc:
        rate_incidents.main(["format-relay-pacing-fields", "--candidates", "codex"])
    assert fmt_exc.value.code == 2
    assert "decision JSON must be an object" in capsys.readouterr().err

    with pytest.raises(SystemExit) as lane_exc:
        rate_incidents.main(
            [
                "record-lane-round",
                "--agent",
                "codex",
                "--surface",
                "   ",
                "--lane",
                "opener",
                "--exit",
                "1",
                "--output-file",
                str(tmp_path / "absent.log"),
            ]
        )
    assert lane_exc.value.code == 2
    assert "surface must be nonempty" in capsys.readouterr().err


def test_switch_review_heartbeat_and_time_parsers(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def broken_heartbeat(*_a, **_k):
        raise RuntimeError("ledger busy")

    monkeypatch.setattr(capabilities, "production_heartbeat", broken_heartbeat)
    switch_review._capability_heartbeat("invocation")
    assert "switch_review: capability heartbeat failed: ledger busy" in capsys.readouterr().err

    assert switch_review._etime_seconds("bad-01:02:03") is None
    assert switch_review._etime_seconds("01:xx") is None
    assert switch_review._etime_seconds("2-01:02:03") == 176523
    assert switch_review._parse_iso_ts(None) is None
    assert switch_review._parse_iso_ts("") is None
    assert switch_review._parse_iso_ts("not-iso") is None
    assert switch_review._parse_window_start(" 1700000123 ") == 1700000123
    assert switch_review._parse_window_start("2026-10-01T00:00:00") == int(
        datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc).timestamp()
    )


def test_switch_review_stale_runners_and_mirror_drift_edges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mirror = tmp_path / "mirror_check"
    checkout = tmp_path / "checkout_check"
    mirror.mkdir()
    checkout.mkdir()
    deployed_mod = mirror / "alpha.py"
    deployed_mod.write_text("x = 1\n", encoding="utf-8")
    os.utime(deployed_mod, (20_000.0, 20_000.0))

    ps_stdout = "\n".join(
        [
            "101",
            "102 01:00:00 /usr/bin/python3 /other/alpha.py",
            f"103 bad-01:00:00 /usr/bin/python3 {mirror}/alpha.py",
            f"104 02:00:00 /usr/bin/python3 {mirror}/alpha.py",
        ]
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_a, **_k: SimpleNamespace(returncode=0, stdout=ps_stdout, stderr=""),
    )
    stale = switch_review.stale_runners(now=23_600.0, mirror=mirror)
    assert [row["pid"] for row in stale] == ["104"]
    assert stale[0]["stale_by_hours"] == 1.0

    def fail_ps(*_a, **_k):
        raise OSError("ps unavailable")

    monkeypatch.setattr(subprocess, "run", fail_ps)
    assert switch_review.stale_runners(now=23_600.0, mirror=mirror) == []

    class BrokenEntry:
        def stat(self):
            raise OSError("broken symlink")

    broken_mirror = SimpleNamespace(glob=lambda _pat: [BrokenEntry()])
    monkeypatch.setattr(switch_review, "Path", lambda value: value)
    assert switch_review.stale_runners(now=23_600.0, mirror=broken_mirror) == []  # type: ignore[arg-type]
    monkeypatch.setattr(switch_review, "Path", Path)

    (checkout / "alpha.py").write_text("x = 1\n", encoding="utf-8")
    (checkout / "unreadable.py").write_text("y = 2\n", encoding="utf-8")
    (mirror / "unreadable.py").mkdir()  # directory at .py path forces OSError on read_bytes()

    def fake_git(cmd, **_kwargs):
        count = "3\n" if "HEAD..@{u}" in cmd else "0\n"
        return SimpleNamespace(returncode=0, stdout=count, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_git)
    drifted = switch_review.mirror_drift(mirror=mirror, checkout=checkout)
    assert drifted["status"] == "drifted"
    assert drifted["differing"] == ["unreadable.py"]
    assert drifted["checkout_behind"] == 3
    assert "3 commit(s) behind upstream" in drifted["reason"]

    monkeypatch.setattr(subprocess, "run", fail_ps)
    git_missing = switch_review.mirror_drift(mirror=mirror, checkout=checkout)
    assert "(git unavailable, so 'behind' is UNMEASURED, not zero)" in git_missing["reason"]


def test_switch_review_gh_call_subprocess_outcomes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(switch_review, "_GH_CALL_RUNNER", None)

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_a, **_k: SimpleNamespace(returncode=0, stdout='{"ok":true}', stderr=""),
    )
    assert switch_review._gh_call(["api", "user"]) == (True, '{"ok":true}', "")

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_a, **_k: SimpleNamespace(returncode=1, stdout="", stderr="HTTP 502\nbad gateway"),
    )
    assert switch_review._gh_call(["api", "user"]) == (
        False,
        "",
        "unmeasured: HTTP 502",
    )

    def raise_timeout(*_a, **_k):
        raise subprocess.TimeoutExpired(cmd=["gh", "api", "user"], timeout=12)

    monkeypatch.setattr(subprocess, "run", raise_timeout)
    assert switch_review._gh_call(["api", "user"], timeout_s=12) == (
        False,
        "",
        "unmeasured: gh timed out after 12s (api user)",
    )

    def raise_missing(*_a, **_k):
        raise FileNotFoundError("no gh binary")

    monkeypatch.setattr(subprocess, "run", raise_missing)
    assert switch_review._gh_call(["api", "user"]) == (
        False,
        "",
        "unmeasured: gh unavailable (no gh binary)",
    )


def test_switch_review_fleet_measures_validation_guards() -> None:
    with pytest.raises(switch_review._FleetReadError, match="would need a valid GitHub timestamp"):
        switch_review._fleet_stamp("2026-99-99T00:00:00Z")

    now = int(datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc).timestamp())
    merged_iso = "2026-10-10T10:00:00Z"

    def make_pr_gh(issue_payload: dict):
        def gh(args, **_kwargs):
            target = args[1]
            if "pulls?state=all" in target:
                return (
                    True,
                    json.dumps(
                        [
                            {
                                "number": 7,
                                "updated_at": merged_iso,
                                "created_at": "2026-10-10T09:00:00Z",
                                "merged_at": merged_iso,
                                "labels": [{"name": "agent:codex"}],
                                "body": "Closes #5",
                                "head": {"ref": "codex/issue-5"},
                            }
                        ]
                    ),
                    "",
                )
            if target == "graphql":
                return (
                    True,
                    json.dumps(
                        {
                            "data": {
                                "repository": {
                                    "pullRequest": {
                                        "closingIssuesReferences": {
                                            "nodes": [],
                                            "pageInfo": {"hasNextPage": False},
                                        }
                                    }
                                }
                            }
                        }
                    ),
                    "",
                )
            if target.endswith("/issues/5"):
                return True, json.dumps(issue_payload), ""
            raise AssertionError(f"unexpected gh call: {args}")

        return gh

    with pytest.raises(
        switch_review._FleetReadError, match="would need Closes references resolving to issues"
    ):
        switch_review._fleet_pr_measures(
            make_pr_gh({"pull_request": {}, "state": "closed"}),
            now,
            ["stranske/Example"],
            flow=False,
        )

    with pytest.raises(
        switch_review._FleetReadError, match="would need issue closure timestamps at or before now"
    ):
        switch_review._fleet_pr_measures(
            make_pr_gh({"state": "closed", "closed_at": "2026-10-10T13:00:00Z"}),
            now,
            ["stranske/Example"],
            flow=False,
        )

    with pytest.raises(switch_review._FleetReadError, match="would need issue state and closed_at"):
        switch_review._fleet_pr_measures(
            make_pr_gh({"state": "mystery"}),
            now,
            ["stranske/Example"],
            flow=False,
        )

    with pytest.raises(
        switch_review._FleetReadError,
        match="would need the configured three non-excluded canary repositories",
    ):
        switch_review._fleet_oldest_canary(
            lambda _args, **_k: (
                True,
                json.dumps({"canaries": [{"repo": "stranske/One"}, {"repo": "stranske/Two"}]}),
                "",
            ),
            now,
        )

    def future_canary_gh(args, **_k):
        if "consumer_sync_canaries.json" in args[1]:
            return (
                True,
                json.dumps(
                    {
                        "canaries": [
                            {"repo": "stranske/One"},
                            {"repo": "stranske/Two"},
                            {"repo": "stranske/Three"},
                        ]
                    }
                ),
                "",
            )
        return (
            True,
            json.dumps(
                [
                    [
                        {
                            "number": 11,
                            "head": {"ref": "sync/workflows-main"},
                            "created_at": "2026-10-10T14:00:00Z",
                        }
                    ]
                ]
            ),
            "",
        )

    with pytest.raises(
        switch_review._FleetReadError, match="would need canary creation timestamps before now"
    ):
        switch_review._fleet_oldest_canary(future_canary_gh, now)


def test_switch_review_registry_yaml_and_consumer_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    yaml_text = "\n".join(
        [
            "env:",
            "  REGISTERED_CONSUMER_REPOS: |",
            '    - "stranske/Alpha"',
            "    - 'stranske/Beta'",
            "jobs:",
            "  sync:",
            "    - stranske/Ignored",
        ]
    )
    assert switch_review._parse_maint_68_registry_yaml(yaml_text) == [
        "stranske/Alpha",
        "stranske/Beta",
    ]

    import consumer_sync_artifact_ingest

    monkeypatch.setattr(consumer_sync_artifact_ingest, "TEST_REGISTRY", ["stranske/Override"])
    assert switch_review._registered_consumer_repos() == (["stranske/Override"], "")

    monkeypatch.setattr(consumer_sync_artifact_ingest, "TEST_REGISTRY", [])
    encoded = base64.b64encode(yaml_text.encode("utf-8")).decode("ascii")
    assert switch_review._registered_consumer_repos(
        gh_fn=lambda _args, **_k: (True, json.dumps({"content": encoded}), "")
    ) == (["stranske/Alpha", "stranske/Beta"], "")

    monkeypatch.setitem(sys.modules, "consumer_sync_artifact_ingest", None)
    assert switch_review._registered_consumer_repos(
        gh_fn=lambda _args, **_k: (True, json.dumps({"content": encoded}), "")
    ) == (["stranske/Alpha", "stranske/Beta"], "")

    fallback_parse, reason_parse = switch_review._registered_consumer_repos(
        gh_fn=lambda _args, **_k: (True, "{bad-json", "")
    )
    assert fallback_parse == list(switch_review.MAINT_68_REGISTRY_FALLBACK)
    assert reason_parse.startswith("unmeasured: maint-68 registry parse failed")

    fallback_empty, reason_empty = switch_review._registered_consumer_repos(
        gh_fn=lambda _args, **_k: (False, "", "")
    )
    assert fallback_empty == list(switch_review.MAINT_68_REGISTRY_FALLBACK)
    assert reason_empty == "unmeasured: maint-68 registry fetch failed"


def test_switch_review_unresolved_review_threads_null_graphql_and_checks() -> None:
    assert switch_review._checks_all_green(None) is False
    assert (
        switch_review._checks_all_green(
            {"state": "PENDING", "contexts": [{"state": "SUCCESS"}, {"state": "success"}]}
        )
        is True
    )
    assert (
        switch_review._checks_all_green(
            {"state": "PENDING", "contexts": [{"state": "SUCCESS"}, {"state": "FAILURE"}]}
        )
        is False
    )

    assert switch_review._unresolved_review_threads(
        "invalid-slug", 12, gh_fn=lambda *_a, **_k: (True, "{}", "")
    ) == (None, "unmeasured: invalid repo slug")
    assert switch_review._unresolved_review_threads(
        "stranske/Repo", 12, gh_fn=lambda *_a, **_k: (True, "{not-json", "")
    ) == (None, "unmeasured: reviewThreads GraphQL JSON parse failed")

    for null_envelope in (
        "null",
        "[]",
        json.dumps({"data": None}),
        json.dumps({"data": {"repository": None}}),
        json.dumps({"data": {"repository": {"pullRequest": None}}}),
        json.dumps({"data": {"repository": {"pullRequest": {"reviewThreads": None}}}}),
        json.dumps({"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": None}}}}}),
    ):
        assert switch_review._unresolved_review_threads(
            "stranske/Repo", 12, gh_fn=lambda *_a, payload=null_envelope, **_k: (True, payload, "")
        ) == (None, "unmeasured: reviewThreads nodes missing")

    nodes_with_null = json.dumps(
        {
            "data": {
                "repository": {
                    "pullRequest": {"reviewThreads": {"nodes": [None, {"isResolved": True}]}}
                }
            }
        }
    )
    assert switch_review._unresolved_review_threads(
        "stranske/Repo", 12, gh_fn=lambda *_a, **_k: (True, nodes_with_null, "")
    ) == (1, "")


def test_switch_review_promote_canaries_continuation_and_fleet_gates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = int(datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc).timestamp())
    assert (
        switch_review._promote_age_days(now=now, gh_fn=lambda *_a, **_k: (True, "{bad", ""))[
            "measurement"
        ]
        == "unmeasured: maint-68 run list JSON parse failed"
    )
    skipped_runs = [
        "not-a-dict",
        {
            "displayTitle": "Sync canary",
            "conclusion": "success",
            "createdAt": "2026-10-10T10:00:00Z",
        },
        {
            "displayTitle": "Promote sync",
            "conclusion": "failure",
            "createdAt": "2026-10-10T10:00:00Z",
        },
        {"displayTitle": "Promote sync", "conclusion": "success", "createdAt": "invalid-ts"},
    ]
    assert switch_review._promote_age_days(
        now=now, gh_fn=lambda *_a, **_k: (True, json.dumps(skipped_runs), "")
    )["measurement"].startswith("unmeasured: no successful Maint 68 promote")

    assert (
        switch_review._open_canaries(
            now=now, gh_fn=lambda *_a, **_k: (True, "[]", ""), repos_fn=lambda: []
        )["measurement"]
        == "unmeasured: no registered consumer repos"
    )

    import consumer_sync_artifact_ingest

    monkeypatch.setattr(consumer_sync_artifact_ingest, "TEST_REGISTRY", [])
    canary_branch = next(iter(switch_review.CANARY_BRANCHES))

    def mixed_canary_gh(args, **_k):
        if args[0] == "api" and "maint-68-sync-consumer-repos.yml" in args[1]:
            return False, "", "unmeasured: offline"
        if args[:2] == ["pr", "list"]:
            repo = args[args.index("--repo") + 1]
            if repo == switch_review.MAINT_68_REGISTRY_FALLBACK[0]:
                return True, "{corrupt", ""
            if repo == switch_review.MAINT_68_REGISTRY_FALLBACK[1]:
                return (
                    True,
                    json.dumps(
                        [
                            "bad-row",
                            {"number": 1, "headRefName": "feature/other", "isDraft": False},
                            {"number": 2, "headRefName": canary_branch, "isDraft": True},
                            {
                                "number": 3,
                                "headRefName": canary_branch,
                                "isDraft": False,
                                "createdAt": "2026-10-09T12:00:00Z",
                                "statusCheckRollup": {"state": "SUCCESS"},
                            },
                        ]
                    ),
                    "",
                )
            return True, "[]", ""
        if args[:2] == ["api", "graphql"]:
            return (
                True,
                json.dumps(
                    {"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": []}}}}}
                ),
                "",
            )
        raise AssertionError(f"unexpected gh args: {args}")

    canaries = switch_review._open_canaries(now=now, gh_fn=mixed_canary_gh, repos_fn=None)
    assert canaries["open_count"] == 1
    assert canaries["drainable_count"] == 1
    assert (
        "measured with registry gap (unmeasured: offline); 1 repo(s) unreadable"
        == canaries["measurement"]
    )

    assert (
        switch_review._maint_82_continuation_hours(
            now=now, gh_fn=lambda *_a, **_k: (True, "{bad", "")
        )["measurement"]
        == "unmeasured: maint-82 run list JSON parse failed"
    )
    assert (
        switch_review._maint_82_continuation_hours(
            now=now, gh_fn=lambda *_a, **_k: (True, json.dumps([{"status": "in_progress"}]), "")
        )["measurement"]
        == "unmeasured: no completed maint-82 runs in lookback"
    )

    def deferred_log_gh(args, **_k):
        if args[:2] == ["run", "list"]:
            return (
                True,
                json.dumps(
                    [
                        {"status": "completed", "databaseId": None},
                        {
                            "status": "completed",
                            "databaseId": 55,
                            "createdAt": "2026-10-10T11:00:00Z",
                        },
                    ]
                ),
                "",
            )
        return False, "", "rate limited"

    assert "maint-82 log fetch deferred" in (
        switch_review._maint_82_continuation_hours(now=now, gh_fn=deferred_log_gh)["measurement"]
    )

    def marker_scan_gh(args, **_k):
        if args[:2] == ["run", "list"]:
            return (
                True,
                json.dumps(
                    [
                        {
                            "status": "completed",
                            "databaseId": 61,
                            "createdAt": "2026-10-10T11:00:00Z",
                        },
                        {"status": "completed", "databaseId": 62, "createdAt": "bad-ts"},
                    ]
                ),
                "",
            )
        if "61" in args:
            return True, "ordinary log without marker", ""
        return True, f"line with {switch_review.CONTINUATION_LOG_MARKER}", ""

    assert switch_review._maint_82_continuation_hours(now=now, gh_fn=marker_scan_gh)[
        "measurement"
    ].startswith(f"unmeasured: no '{switch_review.CONTINUATION_LOG_MARKER}'")

    def promote_ok_gh(args, **_k):
        if args[:2] == ["run", "list"] and switch_review.MAINT_68_WORKFLOW in args:
            return (
                True,
                json.dumps(
                    [
                        {
                            "databaseId": 99,
                            "displayTitle": "Promote sync",
                            "conclusion": "success",
                            "createdAt": "2026-10-10T00:00:00Z",
                        }
                    ]
                ),
                "",
            )
        return True, "[]", ""

    gates = switch_review.fleet_gates(now=now, gh_fn=promote_ok_gh, repos_fn=lambda: [])
    assert gates["status"] == "ok"
    assert gates["suspect"] is False
    assert gates["suspect_reason"] == "unmeasured: no registered consumer repos"


def test_switch_review_exploration_window_and_gate_expiry_edges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir = Path(os.environ["ORCH_STATE_DIR"])
    (state_dir / "exploration-window.json").write_text(
        json.dumps(
            {
                "start": "2026-10-01",
                "end": "20261015",
                "extended": "not-a-bool",
                "n_by_policy": {},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        switch_review,
        "_gh_call",
        lambda *_a, **_k: (True, json.dumps({"exploration": {"rate": 1.5}}), ""),
    )
    window = switch_review._exploration_window(now=1_760_000_000)
    assert any("invalid exploration-window state" in err for err in window["errors"])
    assert any("published rate: invalid exploration.rate" in err for err in window["errors"])

    import value_chain_monitor

    def fail_acted_on(*_a, **_k):
        raise RuntimeError("brain offline")

    monkeypatch.setattr(value_chain_monitor, "acted_on_index", fail_acted_on)
    assert switch_review._acting_index(1_760_000_000, ["cap-a"]) is None

    ledger_path = tmp_path / "caps.json"
    ledger_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        capabilities,
        "load_declared",
        lambda _p: {
            "bad-row": "not-a-dict",
            "live-cap": {
                "status": "gated",
                "gate_reason": "trial",
                "expiry": 1_760_000_000 + 30 * 86400,
                "event_history": [],
            },
        },
    )
    expiry = switch_review.gate_expiry(now=1_760_000_000, path=ledger_path)
    assert expiry["status"] == "ok"
    assert expiry["rows_with_expiry"] == 1
    assert expiry["next_expiry"]["capability_id"] == "live-cap"


def test_switch_review_shed_worktree_refusals_and_bootstrap_helpers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing_entry = tmp_path / "vanished-marker"
    shed = switch_review._shed_row(
        {
            "agent": "codex",
            "path": str(missing_entry),
            "state": "manual",
            "why": "empty file",
            "created_at": None,
            "expires_at": None,
            "incident_id": None,
            "category": None,
        },
        missing_entry,
        now=1_760_000_000.0,
    )
    assert shed["age_days"] is None
    assert shed["suspect"] is None

    explicit_gc = tmp_path / "custom-gc.log"
    monkeypatch.setenv("LANE_GC_LOG", str(explicit_gc))
    assert switch_review._gc_log_path() == explicit_gc

    now = 1_760_000_000
    refusal_log = tmp_path / "refusals.ndjson"
    refusal_log.write_text(
        "\n".join(
            [
                "",
                json.dumps({"ts": "not-int", "worktree": "wt-bad"}),
                json.dumps({"ts": now - 40, "worktree": "wt-1"}),
                json.dumps({"ts": now - 30, "worktree": "wt-2"}),
                json.dumps({"ts": now - 20, "worktree": "wt-3"}),
                json.dumps({"ts": now - 10, "worktree": "wt-4"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    gc_dir = tmp_path / "gc_as_dir"
    gc_dir.mkdir()
    unreadable_gc = switch_review.worktree_git_refusals(
        now=float(now), log_path=refusal_log, gc_log=gc_dir
    )
    assert unreadable_gc["refusals"]["malformed"] == 1
    assert unreadable_gc["refusals"]["count"] == 4
    assert unreadable_gc["refusals"]["latest"] == ["wt-4", "wt-3", "wt-2"]
    assert unreadable_gc["gc_skips"]["status"] == "unmeasured"

    stamp = datetime.fromtimestamp(now - 60, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    explicit_gc.write_text(
        f"not-a-date{switch_review.GC_SKIP_MARK}worktree=wt-bad reason=mismatch\n"
        f"{stamp}{switch_review.GC_SKIP_MARK}worktree=wt-ok reason=mismatch\n",
        encoding="utf-8",
    )
    parsed_gc = switch_review.worktree_git_refusals(
        now=float(now), log_path=refusal_log, gc_log=explicit_gc
    )
    assert parsed_gc["gc_skips"]["count"] == 1
    assert parsed_gc["gc_skips"]["latest"] == ["wt-ok"]

    import credential_redaction

    def fail_exposure(*_a, **_k):
        raise RuntimeError("rg boom")

    monkeypatch.setattr(credential_redaction, "exposure_report", fail_exposure)
    assert switch_review.credential_exposure(now=float(now)) == {
        "status": "unmeasured",
        "reason": "exposure scan failed: RuntimeError",
    }

    inputs = switch_review._redirect_bootstrap_inputs()
    assert set(inputs) == {"corpus_path", "plan_path", "report_dir"}
    assert all(isinstance(val, Path) for val in inputs.values())


def test_switch_review_firing_and_adversarial_shape_validation(tmp_path: Path) -> None:
    state_dir = tmp_path / "firing_state"
    state_dir.mkdir()
    report_path = state_dir / "capability-firing-monitor.json"

    report_path.write_text(json.dumps({"generated_at": "bad"}), encoding="utf-8")
    assert (
        switch_review.firing_regressions(now=1_760_000_000, state_dir=state_dir)["reason"]
        == "missing integer generated_at"
    )

    report_path.write_text(
        json.dumps(
            {
                "generated_at": 1_760_000_000,
                "regressed": [{"capability_id": ""}],
                "overdue": [],
            }
        ),
        encoding="utf-8",
    )
    assert (
        switch_review.firing_regressions(now=1_760_000_000, state_dir=state_dir)["reason"]
        == "missing capability_id"
    )

    rendered = "\n".join(
        switch_review._firing_lines(
            {
                "status": "ok",
                "generated_at": 1_760_000_000,
                "rows": [
                    {
                        "capability_id": "cap-future",
                        "findings": ["overdue"],
                        "last_heartbeat": 1_760_000_000,
                        "step_evidence": [
                            {
                                "step": "weekly",
                                "stamp_mtime": 1_760_000_100,
                                "stamp_age_seconds": -100,
                                "stale_after_seconds": 3600,
                                "artifact_mtime": None,
                                "artifact_path": None,
                            }
                        ],
                    },
                    {
                        "capability_id": "cap-uncarried",
                        "findings": ["regressed"],
                        "last_heartbeat": None,
                        "step_evidence": [],
                    },
                ],
            }
        )
    )
    assert "future stamp" in rendered
    assert "no declared cadence carrier" in rendered

    import adversarial

    rule = adversarial.shape_rule_id()
    shapes_path = state_dir / "fleet-shapes.json"

    def write_shapes(section_overrides: dict, *, prs: int = 10) -> dict:
        base = {
            "rule": rule,
            "shape_candidates": 2,
            "label_candidates": 1,
            "population": 10,
            "unknown": 0,
            "status": "ok",
            **section_overrides,
        }
        shapes_path.write_text(
            json.dumps(
                {
                    "generated_at": 1_760_000_000,
                    "counts": {"prs": prs},
                    "adversarial_shape": base,
                }
            ),
            encoding="utf-8",
        )
        return switch_review.adversarial_shape_population(state_dir=state_dir, now=1_760_000_100)

    assert write_shapes({"shape_candidates": -1})["reason"] == "invalid shape_candidates"
    assert write_shapes({}, prs=9)["reason"] == "population disagrees with fleet-shapes PR count"
    assert write_shapes({"shape_candidates": 11})["reason"] == "candidate count exceeds population"
    assert (
        write_shapes({"status": "ok", "unknown": 1})["reason"]
        == "measurement status disagrees with unknown count"
    )


def test_switch_review_questions_phrases_and_summary_lines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_question(*_a, **_k):
        raise RuntimeError("question db locked")

    monkeypatch.setattr(feedback, "record_owner_question", fail_question)
    outcome = switch_review.raise_questions(
        {
            "held_off": [
                {
                    "flag": "ORCH_TEST_SWITCH",
                    "capability": "cap-test",
                    "state": "off",
                    "value_source": "ambient",
                }
            ],
            "on_but_idle": [],
        },
        dry_run=False,
    )
    assert outcome["errors"] == ["ORCH_TEST_SWITCH: question db locked"]

    assert (
        switch_review.value_phrase({"value": "1", "value_source": "custom_origin"})
        == "'1' — source custom_origin"
    )

    state_dir = Path(os.environ["ORCH_STATE_DIR"])
    trial_dir = state_dir / "capability-program"
    trial_dir.mkdir()
    trial_path = trial_dir / "profile-trial.json"
    trial_path.write_text("[]", encoding="utf-8")
    assert switch_review.profile_trial_summary_line() is None

    trial_path.write_text("{}", encoding="utf-8")
    with monkeypatch.context() as scoped_mp:
        scoped_mp.setattr(
            json,
            "loads",
            lambda _s: {
                "profile_count": 1,
                "instance_count": 1,
                "cost_tokens_by_profile": {1: {"tokens_in": 5, "tokens_out": 7}},
                "quality_by_profile": {},
            },
        )
        assert switch_review.profile_trial_summary_line() is None

    import experiment_producer

    monkeypatch.setattr(
        experiment_producer,
        "producer_summary",
        lambda: {
            "launched": 2,
            "evaluated": 1,
            "winner_prs": 1,
            "winner_merged": 0,
            "cells_fresh": 1,
            "cells_total": 2,
            "skips": {"no_named_test": 3},
            "no_named_test_classes": {"file-only": 1, "ci-only": 0, "none": 0},
            "unknowns": [],
        },
    )
    monkeypatch.setattr(experiment_producer, "trial_window_end", lambda _s: 1_760_000_000)

    monkeypatch.setattr(experiment_producer, "trial_verdict", lambda **_k: "pending")
    monkeypatch.setattr(
        experiment_producer,
        "read_state",
        lambda: {"pending_since": "2026-10-01", "resolves_on": "2026-10-08"},
    )
    pending_line = switch_review.experiment_producer_line()
    assert "UNKNOWN historical_test_class=2" in pending_line
    assert "verdict pending since 2026-10-01, resolves fail on 2026-10-08" in pending_line

    monkeypatch.setattr(experiment_producer, "trial_verdict", lambda **_k: "fail")
    monkeypatch.setattr(
        experiment_producer, "read_state", lambda: {"trial_reason": "zero merged winners"}
    )
    reason_line = switch_review.experiment_producer_line()
    assert "verdict fail (zero merged winners)" in reason_line


def test_switch_review_assess_fallback_and_report_formatting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = 1_760_000_000
    clean_log = tmp_path / "assess-non-network.log"
    clean_log.write_text("syntax assertion failed locally\n", encoding="utf-8")
    ledger = tmp_path / "capacity-ledger.ndjson"
    ledger.write_text(
        "\n".join(
            [
                "",
                "[1, 2]",
                json.dumps(
                    {"ts": now - 90 * 86400, "event": "complete", "mode": "assess", "exit": 1}
                ),
                json.dumps(
                    {
                        "ts": now - 60,
                        "event": "complete",
                        "mode": "assess",
                        "exit": 1,
                        "error": "OFFLOAD_INCOMPLETE",
                        "log_file": str(clean_log),
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    section = switch_review.assess_sandbox_fallback_report(ledger_path=ledger, now=now)
    assert section["malformed_rows"] == 1
    assert section["network_caused_assess_incompletions"] == 0
    assert section["unclassifiable"] == 0
    assert section["status"] == "unverified"

    expiry_lines = "\n".join(
        switch_review.format_gate_expiry(
            {
                "status": "ok",
                "window_days": 14,
                "ttl_days": 30,
                "renewable": 0,
                "expiring": [],
                "lapsed": [
                    {
                        "capability_id": "cap-lapsed",
                        "retired_on": "2026-10-09",
                        "status_before": "gated",
                        "recent_invocations": 0,
                        "recent_window_days": 30,
                        "last_invocation_on": None,
                        "renewals": 0,
                        "renewable": False,
                        "blocker": "zero acting in window",
                    }
                ],
            }
        )
    )
    assert "not renewable: zero acting in window" in expiry_lines

    import strategy_experiment

    strat_path = tmp_path / "strategy-result.json"
    strat_path.write_text(
        json.dumps({"status": "completed", "exp_id": "exp-9", "costs": None, "comparison": {}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(strategy_experiment, "strategy_result_path", lambda: strat_path)
    monkeypatch.setattr(switch_review, "comparative_routing_line", lambda _r: "comparative: stub")
    monkeypatch.setattr(switch_review, "experiment_producer_line", lambda: "  producer: stub")

    formatted = switch_review.format_report(
        {
            "review_days": 7,
            "raise_count": 0,
            "held_off": [],
            "on_but_idle": [],
            "unconditioned": [],
            "stale_runners": [
                {
                    "pid": "4321",
                    "age_hours": 5.0,
                    "stale_by_hours": 2.5,
                    "command": "/usr/bin/python3 mirror/mcp_server.py",
                }
            ],
            "mirror_drift": {
                "status": "drifted",
                "absent_from_mirror": [],
                "differing": ["a.py"],
                "checkout_behind": 2,
            },
            "fleet_gates": {
                "promote": {"measurement": "unmeasured: offline"},
                "canaries": {"measurement": "unmeasured: no repos", "open_count": None},
                "maint_82_continuation": {"measurement": "unmeasured: offline"},
                "suspect": False,
            },
        }
    )
    assert "strategy experiment: completed exp-9" in formatted
    assert "pid 4321  up 5.0h  stale by 2.5h" in formatted
    assert "the checkout is 2 commit(s) behind upstream" in formatted
    assert "sync canaries: unmeasured: no repos" in formatted


def test_switch_review_review_and_cli_error_isolation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail_vc_heartbeat(cap_id: str, *_a, **_k):
        if cap_id == "value-chain-monitor":
            raise RuntimeError("vc heartbeat boom")

    monkeypatch.setattr(capabilities, "production_heartbeat", fail_vc_heartbeat)
    monkeypatch.setattr(
        switch_review,
        "switch_states",
        lambda **_k: {"held_off": [], "on_but_idle": []},
    )
    monkeypatch.setattr(switch_review, "stale_runners", lambda **_k: [])
    monkeypatch.setattr(switch_review, "mirror_drift", lambda **_k: {"status": "ok"})
    monkeypatch.setattr(
        switch_review, "fleet_gates", lambda **_k: {"status": "ok", "suspect": False}
    )
    monkeypatch.setattr(
        switch_review, "firing_regressions", lambda **_k: {"status": "ok", "rows": []}
    )
    monkeypatch.setattr(
        switch_review, "_exploration_gate", lambda: {"status": "ok", "suspect": False}
    )
    monkeypatch.setattr(
        switch_review,
        "gate_expiry",
        lambda **_k: {"status": "ok", "expiring": [], "lapsed": []},
    )
    monkeypatch.setattr(switch_review, "capacity_shed", lambda **_k: {"status": "ok", "suspect": 0})
    monkeypatch.setattr(switch_review, "weekly_pacing_check", lambda **_k: "pacing: ok")
    monkeypatch.setattr(switch_review, "worktree_git_refusals", lambda **_k: {"window_days": 7})

    def fail_assess(**_k):
        raise RuntimeError("corrupt assess ledger")

    monkeypatch.setattr(switch_review, "assess_sandbox_fallback_report", fail_assess)
    ledger_file = tmp_path / "empty-caps.json"
    ledger_file.write_text("{}", encoding="utf-8")
    rep = switch_review.review(
        now=1_760_000_000,
        env={"ORCH_VALUE_CHAIN_MONITOR": "1"},
        path=ledger_file,
        value_chain_inputs={"prs": {}, "issues": {}, "runs": [], "errors": []},
        feature_goals_fn=lambda _now: {"drifting": [], "errors": []},
    )
    assert "vc heartbeat boom" in capsys.readouterr().err
    assert rep["assess_sandbox_fallback"]["status"] == "unmeasured"
    assert "RuntimeError: corrupt assess ledger" in rep["assess_sandbox_fallback"]["reason"]

    import adversarial
    import issue_size_quality
    import python_standard_audit

    monkeypatch.setenv("ORCH_VALUE_CHAIN_MONITOR", "0")
    monkeypatch.setenv("ORCH_DISABLE_STEPS", "")
    monkeypatch.setattr(switch_review, "review", lambda **_k: dict(rep))
    monkeypatch.setattr(switch_review, "credential_exposure", lambda **_k: {"status": "ok"})
    monkeypatch.setattr(python_standard_audit, "snapshot", lambda: {"status": "ok"})
    monkeypatch.setattr(
        switch_review, "fleet_delivery_measures", lambda **_k: {"as_of": 1_760_000_000, "rows": {}}
    )
    monkeypatch.setattr(issue_size_quality, "fleet_repos", lambda: [])

    def fail_isq(**_k):
        raise RuntimeError("isq offline")

    def fail_shape(*_a, **_k):
        raise OSError("shape state read-only")

    monkeypatch.setattr(issue_size_quality, "run", fail_isq)
    monkeypatch.setattr(adversarial, "record_shape_measurement", fail_shape)

    monkeypatch.setattr(switch_review, "APPLY_ENABLED", False)
    assert switch_review.main(["--env", "process", "--json", "--raise"]) == 2
    assert "refusing to raise: set ORCH_SWITCH_REVIEW=1" in capsys.readouterr().err

    monkeypatch.setattr(switch_review, "APPLY_ENABLED", True)
    monkeypatch.setattr(
        switch_review,
        "raise_questions",
        lambda _r, *, dry_run: {"raised": ["X"], "dry_run": dry_run},
    )
    assert switch_review.main(["--env", "process", "--json", "--raise"]) == 0
    cli_payload = json.loads(capsys.readouterr().out)
    assert cli_payload["issue_size_quality"] == {"status": "unknown", "errors": ["isq offline"]}
    assert cli_payload["adversarial_shape_measurement_error"] == "shape state read-only"
    assert cli_payload["questions"] == {"raised": ["X"], "dry_run": False}
