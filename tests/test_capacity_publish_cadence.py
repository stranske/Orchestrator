"""Exercise tick observations, real remote pushes, and the read-only lag query offline."""

import copy
import json
import subprocess
import sys

import pytest

import pacing
import route_weights_export as export

NOW = 2_000_000_000


def snapshot(state, timestamp):
    return {"generated_at": timestamp, "agents": {"codex": {"state": state, "window": "weekly"}}}


@pytest.fixture
def local_capacity(tmp_path, monkeypatch):
    path = tmp_path / "capacity.json"
    monkeypatch.setattr(export.capacity, "OUT", path)
    return path


def observe(path, state_dir, state, timestamp):
    path.write_text(json.dumps(snapshot(state, timestamp)))
    assert export.record_capacity_tick(state_dir, now=timestamp)


@pytest.fixture
def publication(tmp_path, monkeypatch):
    for key in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{key}_NAME", "Cadence Test")
        monkeypatch.setenv(f"GIT_{key}_EMAIL", "cadence@example.invalid")

    def git(path, *args):
        return subprocess.run(
            ["git", "-C", str(path), *args], check=True, capture_output=True, text=True
        ).stdout.strip()

    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", "--initial-branch=main", str(remote))
    checkout = tmp_path / "checkout"
    git(tmp_path, "clone", str(remote), str(checkout))
    document = {
        "generated_at": "2033-05-18T03:33:20Z",
        "source_version": 1,
        "task_types": {"implement": {"evidence_ok": True}},
        "capacity": {"codex": {"state": "ok", "window": "weekly", "observed_at": NOW}},
    }
    target = checkout / export.EXPORT_PATH
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps(document))
    git(checkout, "add", ".")
    git(checkout, "commit", "-m", "baseline")
    git(checkout, "push", "origin", f"HEAD:{export.EXPORT_BRANCH}")
    monkeypatch.setattr(export.provision, "ensure_canonical", lambda *a: checkout)
    monkeypatch.setattr(export, "_now", lambda: "2033-05-18T03:33:20Z")
    return document, remote, checkout, git


@pytest.mark.parametrize("initial,new", [("ok", "warn"), ("ok", "shed"), ("shed", "ok")])
def test_blip_holds_and_two_ticks_publish_in_either_direction(
    publication, local_capacity, tmp_path, monkeypatch, capsys, initial, new
):
    document, remote, checkout, git = publication
    document["capacity"]["codex"]["state"] = initial
    (checkout / export.EXPORT_PATH).write_text(json.dumps(document))
    git(checkout, "add", ".")
    if initial != "ok":
        git(checkout, "commit", "-m", "initial shed")
        git(checkout, "push", "origin", f"HEAD:{export.EXPORT_BRANCH}")
    state_dir = tmp_path / "stamps"
    incoming = copy.deepcopy(document)
    incoming["capacity"]["codex"]["state"] = new
    incoming["capacity"]["codex"]["pacing"] = {"state": new}
    head = git(remote, "rev-parse", export.EXPORT_BRANCH)
    monkeypatch.setattr(export, "_now", lambda: "2033-05-18T03:33:20Z")
    # Freeze wall-clock validation at each observation without changing datetime globally.
    real_damp = export.damp_capacity_states
    clock = [NOW]
    monkeypatch.setattr(
        export,
        "damp_capacity_states",
        lambda doc, published, directory: real_damp(doc, published, directory, now=clock[0]),
    )
    for state in (new, initial, new):
        clock[0] += 3600
        observe(local_capacity, state_dir, state, clock[0])
        assert not export.publish_document(incoming, state_dir=state_dir)
        assert git(remote, "rev-parse", export.EXPORT_BRANCH) == head
    # Re-reading one snapshot is not a second tick.
    assert export.record_capacity_tick(state_dir, now=clock[0])
    assert not export.publish_document(incoming, state_dir=state_dir)
    assert f"local={new} published={initial} seen=1/2" in capsys.readouterr().out
    clock[0] += 3600
    observe(local_capacity, state_dir, new, clock[0])
    assert export.publish_document(incoming, state_dir=state_dir)
    assert git(remote, "rev-parse", export.EXPORT_BRANCH) != head
    published = json.loads(git(remote, "show", f"{export.EXPORT_BRANCH}:{export.EXPORT_PATH}"))
    assert published["capacity"]["codex"] == incoming["capacity"]["codex"]
    assert not export.publish_document(incoming, state_dir=state_dir)


@pytest.mark.parametrize("gap", [7201, 10_000])
def test_missing_tick_restarts_observation(local_capacity, tmp_path, gap):
    observe(local_capacity, tmp_path, "shed", NOW)
    observe(local_capacity, tmp_path, "shed", NOW + gap)
    assert json.loads((tmp_path / export.CAPACITY_TICK_FILE).read_text())["codex"]["seen"] == 1


def test_stale_or_missing_snapshot_clears_latch(local_capacity, tmp_path):
    observe(local_capacity, tmp_path, "shed", NOW)
    assert not export.record_capacity_tick(tmp_path, now=NOW + 7201)
    assert json.loads((tmp_path / export.CAPACITY_TICK_FILE).read_text()) == {}
    local_capacity.unlink()
    assert not export.record_capacity_tick(tmp_path, now=NOW)


def test_expired_latch_cannot_publish_new_state(local_capacity, tmp_path, capsys):
    observe(local_capacity, tmp_path, "shed", NOW)
    observe(local_capacity, tmp_path, "shed", NOW + 3600)
    document = {"capacity": {"codex": {"state": "shed"}}}
    published = {"capacity": {"codex": {"state": "ok"}}}
    assert export.damp_capacity_states(document, published, tmp_path, now=NOW + 10801) == published
    assert "seen=0/2" in capsys.readouterr().out


@pytest.mark.parametrize("mode", ["sync", "different", "unmeasured"])
def test_lag_cli_is_read_only(local_capacity, tmp_path, monkeypatch, capsys, mode):
    local_capacity.write_text(json.dumps(snapshot("shed", NOW - 10)))
    published = {
        "generated_at": "2033-05-18T01:33:19Z",
        "capacity": export._capacity_block(snapshot("shed", NOW - 3600)),
    }
    if mode == "different":
        published["capacity"]["codex"]["state"] = "ok"
    if mode == "unmeasured":

        def fetch():
            raise subprocess.CalledProcessError(1, ["git", "fetch"])

    else:

        def fetch():
            return published

    monkeypatch.setattr(export, "read_published_export", fetch)
    monkeypatch.setattr(
        export, "build_document", lambda *a: pytest.fail("lag must not build/write")
    )
    real_lag = export.publish_lag
    monkeypatch.setattr(
        export, "publish_lag", lambda **kwargs: real_lag(now=NOW, state_dir=tmp_path)
    )
    monkeypatch.setattr(sys, "argv", ["route_weights_export.py", "--publish-lag"])
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}
    assert export.main() == 0
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()} == before
    out = capsys.readouterr().out
    assert "local capacity age=10s" in out
    expected = {
        "sync": "published=shed published_age=7201s lag=ok",
        "different": "published=ok published_age=7201s lag=STALE",
        "unmeasured": "published=unmeasured published_age=unmeasured lag=STALE",
    }[mode]
    assert f"pool codex: local=shed {expected}" in out
    assert ("all pools in sync" in out) == (mode == "sync")
    assert out.count("pool ") == len(export.capacity.AGENTS)


def test_read_publication_uses_remote_branch_without_touching_checkout(publication):
    document, remote, checkout, git = publication
    before = git(checkout, "status", "--porcelain")
    assert export.read_published_export(remote_url=str(remote)) == document
    assert git(checkout, "status", "--porcelain") == before


@pytest.mark.parametrize("malformed", [None, [], {"codex": "invalid"}])
def test_publication_repairs_malformed_capacity_baseline(publication, tmp_path, malformed):
    document, remote, checkout, git = publication
    broken = {**document, "capacity": malformed}
    (checkout / export.EXPORT_PATH).write_text(json.dumps(broken))
    git(checkout, "add", ".")
    git(checkout, "commit", "-m", "malformed capacity baseline")
    git(checkout, "push", "origin", f"HEAD:{export.EXPORT_BRANCH}")
    assert export.publish_document(document, state_dir=tmp_path / "stamps")
    repaired = json.loads(git(remote, "show", f"{export.EXPORT_BRANCH}:{export.EXPORT_PATH}"))
    assert repaired["capacity"] == document["capacity"]


# Every continuously changing diagnostic emitted by the current pacing model.
VOLATILE = (
    "ts",
    "reason",
    "burn_rate_per_day",
    "burn_per_day",
    "fyi",
    "allowed_fraction",
    "projected_fraction",
    "routing_weight",
    "used_fraction",
    "period_start",
    "reset_expiries",
    "effective_reserve",
    "stale",
    "source",
    "measurement",
    "previous_routing_weight",
    "target_routing_weight",
    "effective_projected_fraction",
    "span_h",
    "confidence",
    "sustainable_rate_per_hour",
    "burn_short_per_hour",
    "burn_period_per_hour",
    "burn_effective_per_hour",
)


@pytest.mark.parametrize("field", VOLATILE)
def test_pacing_diagnostic_only_change_never_pushes(publication, field):
    document, remote, checkout, git = publication
    document["capacity"]["codex"]["pacing"] = {field: 1}
    target = checkout / export.EXPORT_PATH
    target.write_text(json.dumps(document))
    git(checkout, "add", ".")
    git(checkout, "commit", "-m", "diagnostic baseline")
    git(checkout, "push", "origin", f"HEAD:{export.EXPORT_BRANCH}")
    head = git(remote, "rev-parse", export.EXPORT_BRANCH)
    document["capacity"]["codex"]["pacing"][field] = 2
    assert not export.publish_document(document)
    assert git(remote, "rev-parse", export.EXPORT_BRANCH) == head


def test_actual_pacing_clock_only_drift_is_nonsemantic():
    def document(now):
        row = pacing.evaluate(
            "codex",
            now=now,
            used_fraction=0.2,
            period_start=NOW - 86400,
            period_end=NOW + 6 * 86400,
            resets=[],
            samples=[],
            source="fixture",
            source_ts=NOW,
            previous_weight=0.8,
        )
        return {"capacity": {"codex": {"state": row["state"], "pacing": row}}}

    assert export._semantic_for_compare(document(NOW)) == export._semantic_for_compare(
        document(NOW + 100)
    )


def test_weights_can_publish_while_entire_capacity_row_is_held(
    publication, local_capacity, tmp_path, monkeypatch
):
    document, remote, checkout, git = publication
    observe(local_capacity, tmp_path, "shed", NOW)
    real_damp = export.damp_capacity_states
    monkeypatch.setattr(
        export,
        "damp_capacity_states",
        lambda doc, published, directory: real_damp(doc, published, directory, now=NOW),
    )
    incoming = copy.deepcopy(document)
    incoming["source_version"] = 2
    incoming["capacity"]["codex"].update(
        state="shed", pacing={"state": "shed", "routing_weight": 0}
    )
    assert export.publish_document(incoming, state_dir=tmp_path)
    published = json.loads(git(remote, "show", f"{export.EXPORT_BRANCH}:{export.EXPORT_PATH}"))
    assert published["source_version"] == 2
    assert published["capacity"] == document["capacity"]


@pytest.mark.parametrize("due,attempt", [(1, 0), (0, 1), (0, 0)])
def test_tick_observation_is_independent_of_due_and_backoff(tmp_path, due, attempt):
    import paths

    source = (paths.REPO_ROOT / "orchestrate.sh").read_text()
    start = source.index('python3 "$ORCH/route_weights_export.py" --record-capacity-tick')
    block = source[start : source.index("if _cadence_due periodic-report ", start)]
    stubs = f"""
_cadence_due() {{ return {due}; }}
_attempt_ok() {{ return {attempt}; }}
_mark_success() {{ echo SUCCESS; }}
_mark_fail() {{ echo FAIL; }}
python3() {{
  printf '%s\\n' "$@" >> "$STAMP_DIR/export-args"
  if [[ "$2" == "--record-capacity-tick" ]]; then echo OBSERVED; else
    echo '[cadence] capacity publish held: codex local=shed published=ok seen=1/2'
  fi
}}
"""
    result = subprocess.run(
        ["bash", "-c", stubs + block],
        check=True,
        capture_output=True,
        text=True,
        env={"STAMP_DIR": str(tmp_path), "ORCH": str(tmp_path)},
    )
    calls = (tmp_path / "export-args").read_text().splitlines()
    assert calls[:4] == [
        str(tmp_path / "route_weights_export.py"),
        "--record-capacity-tick",
        "--state-dir",
        str(tmp_path),
    ]
    if due == 0 and attempt == 0:
        assert calls[4:] == [
            str(tmp_path / "route_weights_export.py"),
            "--publish",
            "--state-dir",
            str(tmp_path),
        ]
        import re

        assert re.search(
            r"\[cadence\] route weights export start \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z duration=0",
            result.stdout,
        )
        assert re.search(
            r"\[cadence\] route weights export end \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z duration=\d+",
            result.stdout,
        )
    assert result.stdout.count("OBSERVED") == 1
    assert ("SUCCESS" in result.stdout) == (due == 0 and attempt == 0)
    assert ("seen=1/2" in result.stdout) == (due == 0 and attempt == 0)


def _seed(publication, document):
    _, _, checkout, git = publication
    (checkout / export.EXPORT_PATH).write_text(json.dumps(document))
    git(checkout, "add", ".")
    git(checkout, "commit", "-m", "pacing baseline")
    git(checkout, "push", "origin", f"HEAD:{export.EXPORT_BRANCH}")


def test_used_fraction_drift_does_not_push(publication):
    document = publication[0]
    document["capacity"]["codex"]["pacing"] = {"used_fraction": 0.43}
    assert not export.publish_document(document)  # Adding only diagnostics is also silent.
    _seed(publication, document)
    incoming = copy.deepcopy(document)
    incoming["capacity"]["codex"]["pacing"]["used_fraction"] = 0.47
    assert not export.publish_document(incoming)


@pytest.mark.parametrize("field,new", [("period_end", NOW + 86400), ("resets_available", 2)])
def test_period_and_reset_bank_changes_push(publication, field, new):
    document = publication[0]
    document["capacity"]["codex"]["pacing"] = {field: 1}
    _seed(publication, document)
    incoming = copy.deepcopy(document)
    incoming["capacity"]["codex"]["pacing"][field] = new
    assert export.publish_document(incoming)


def test_24_hour_ledger_shape_replay_only_refreshes_once(publication, monkeypatch, capsys):
    import datetime as dt

    document, remote, _, git = publication

    def iso(now):
        return dt.datetime.fromtimestamp(now, dt.timezone.utc).isoformat().replace("+00:00", "Z")

    # Same shape as pacing_usage's ledger append: diagnostics are separate from pacing.
    def ledger(hour):
        return {
            "event": "pacing_snapshot",
            "ts": NOW + hour * 3600,
            "agents": {
                "codex": {
                    "state": "ok",
                    "pacing": {
                        "state": "ok",
                        "used_fraction": 0.43 + hour * 0.002,
                        "routing_weight": 0.8 - hour * 0.001,
                        "period_start": NOW - 86400,
                        "period_end": NOW + 6 * 86400 + (60 if hour % 2 else -60),
                        "resets_available": 0,
                        "measurement": "measured",
                    },
                    "pacing_diagnostics": {"span_h": hour + 1, "confidence": 0.9},
                }
            },
        }

    def comparative_drift(doc, hour):
        doc["comparative"] = {"lambda": {"claude": 71.96 + hour}, "status": str(hour)}
        for row in doc["task_types"]["implement"]["ranking"] + doc["reserve"]["implement"]:
            row.update(
                comparative_rank=hour,
                comparative_status=str(hour),
                comparative_reason=str(hour),
                comparative_future_field=hour,
            )

    document["task_types"]["implement"]["ranking"] = [{"agent": "codex", "posterior": 0.9}]
    document["reserve"] = {"implement": [{"agent": "claude", "posterior": 0.8}]}
    comparative_drift(document, 0)
    first = ledger(0)
    document["capacity"] = export._capacity_block({**first, "generated_at": first["ts"]})
    document["generated_at"] = iso(NOW - 1)
    _seed(publication, document)
    head = git(remote, "rev-parse", export.EXPORT_BRANCH)
    pushed = 0
    steps = export.PUBLISH_REFRESH_FLOOR_S // 3600
    for hour in range(1, steps + 1):
        row = ledger(hour)
        incoming = {
            **copy.deepcopy(document),
            "generated_at": iso(row["ts"]),
            "capacity": export._capacity_block({**row, "generated_at": row["ts"]}),
        }
        comparative_drift(incoming, hour)
        monkeypatch.setattr(export, "_now", lambda t=row["ts"]: iso(t))
        pushed += export.publish_document(incoming)
        assert pushed == (1 if hour == steps else 0)
    assert pushed <= 1
    assert int(git(remote, "rev-list", "--count", f"{head}..{export.EXPORT_BRANCH}")) == 1
    published = json.loads(git(remote, "show", f"{export.EXPORT_BRANCH}:{export.EXPORT_PATH}"))
    assert published == incoming  # Includes the latest lambda and both kinds of comparative rows.
    out = capsys.readouterr().out
    assert f"unchanged published_age=3601s/floor={export.PUBLISH_REFRESH_FLOOR_S}s" in out
    assert (
        f"refresh published_age={export.PUBLISH_REFRESH_FLOOR_S + 1}s/floor={export.PUBLISH_REFRESH_FLOOR_S}s"
        in out
    )


@pytest.mark.parametrize("bad", [None, "broken", [], float("inf")])
def test_corrupt_seen_still_publishes_on_second_observation(
    publication, local_capacity, tmp_path, monkeypatch, bad
):
    document = copy.deepcopy(publication[0])
    document["capacity"]["codex"]["state"] = "shed"
    observe(local_capacity, tmp_path, "shed", NOW)
    tick_path = tmp_path / export.CAPACITY_TICK_FILE
    ticks = json.loads(tick_path.read_text())
    ticks["codex"]["seen"] = bad
    tick_path.write_text(json.dumps(ticks))
    real_damp = export.damp_capacity_states
    monkeypatch.setattr(
        export,
        "damp_capacity_states",
        lambda doc, pub, directory: real_damp(doc, pub, directory, now=NOW + 3600),
    )
    assert not export.publish_document(document, state_dir=tmp_path)
    observe(local_capacity, tmp_path, "shed", NOW + 3600)
    assert export.publish_document(document, state_dir=tmp_path)


@pytest.mark.parametrize(
    "age,seen,expected", [(7200, 2, "ok"), (7201, 2, "STALE"), (9000, 1, "held(1/2)")]
)
def test_lag_threshold_and_hold(local_capacity, tmp_path, monkeypatch, capsys, age, seen, expected):
    import datetime as dt

    local_capacity.write_text(json.dumps(snapshot("shed", NOW)))
    generated = dt.datetime.fromtimestamp(NOW - age, dt.timezone.utc).isoformat()
    monkeypatch.setattr(
        export,
        "read_published_export",
        lambda: {
            "generated_at": generated,
            "capacity": {"codex": {"state": "ok", "observed_at": NOW - 99}},
        },
    )
    (tmp_path / export.CAPACITY_TICK_FILE).write_text(
        json.dumps({"codex": {"state": "shed", "observed_at": NOW, "seen": seen}})
    )
    export.publish_lag(now=NOW, state_dir=tmp_path)
    out = capsys.readouterr().out
    assert f"published_age={age}s lag={expected} observed_age=99s" in out
    assert "all pools in sync" not in out


@pytest.mark.parametrize(
    "delta,loud",
    [(-3600, False), (-60, False), (60, False), (3600, False), (-3601, True), (3601, True)],
)
def test_period_end_tolerance_is_against_published_baseline(publication, delta, loud):
    assert export.PERIOD_END_TOLERANCE_S >= 3600
    document, remote, _, git = publication
    document["capacity"]["codex"]["pacing"] = {"period_end": NOW + 86400}
    _seed(publication, document)
    before = copy.deepcopy(document)
    head = git(remote, "rev-parse", export.EXPORT_BRANCH)
    incoming = copy.deepcopy(document)
    incoming["capacity"]["codex"]["pacing"]["period_end"] += delta
    assert export.publish_document(incoming) == loud
    assert (git(remote, "rev-parse", export.EXPORT_BRANCH) != head) == loud
    assert document == before  # Comparison cannot mutate the baseline.


def test_local_write_period_end_drift_accumulates_against_published_baseline(tmp_path):
    target = tmp_path / "artifact.json"
    doc = {"capacity": {"claude": {"state": "ok", "pacing": {"period_end": NOW}}}}
    assert export.write_document(target, doc)
    for delta in range(60, export.PERIOD_END_TOLERANCE_S + 1, 60):
        doc["capacity"]["claude"]["pacing"]["period_end"] = NOW + delta
        assert not export.write_document(target, doc)
    doc["capacity"]["claude"]["pacing"]["period_end"] = NOW + export.PERIOD_END_TOLERANCE_S + 1
    assert export.write_document(target, doc)


def test_missing_local_snapshot_prints_unmeasured_age(
    local_capacity, tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(export, "read_published_export", lambda: {})
    export.publish_lag(now=NOW, state_dir=tmp_path)
    out = capsys.readouterr().out
    assert "local capacity age=unmeasured\n" in out
    assert "unmeasureds" not in out
    assert "all pools in sync" not in out


@pytest.mark.parametrize("generated", [None, "invalid"])
def test_unmeasured_published_age_refreshes(publication, tmp_path, capsys, generated):
    document, remote, _, git = publication
    if generated is None:
        document.pop("generated_at")
    else:
        document["generated_at"] = generated
    _seed(publication, document)
    incoming = {**document, "generated_at": "2033-05-18T03:33:20Z"}
    assert export.publish_document(incoming, state_dir=tmp_path)
    assert (
        f"refresh published_age=unmeasured/floor={export.PUBLISH_REFRESH_FLOOR_S}s"
        in capsys.readouterr().out
    )
    published = json.loads(git(remote, "show", f"{export.EXPORT_BRANCH}:{export.EXPORT_PATH}"))
    assert published == incoming


def test_lag_hold_uses_tick_validity_bound(local_capacity, tmp_path, monkeypatch, capsys):
    local_capacity.write_text(json.dumps(snapshot("shed", NOW)))
    monkeypatch.setattr(export, "CAPACITY_TICK_MAX_AGE", 100)
    monkeypatch.setattr(
        export,
        "read_published_export",
        lambda: {"generated_at": "2033-05-18T00:00:00Z", "capacity": {"codex": {"state": "ok"}}},
    )
    (tmp_path / export.CAPACITY_TICK_FILE).write_text(
        json.dumps({"codex": {"state": "shed", "observed_at": NOW - 101, "seen": 1}})
    )
    export.publish_lag(now=NOW, state_dir=tmp_path)
    codex_line = next(
        line for line in capsys.readouterr().out.splitlines() if line.startswith("pool codex:")
    )
    assert "lag=STALE" in codex_line
    assert "held(" not in codex_line


@pytest.mark.parametrize("failure", [False, True])
def test_publish_history_read_only_query(local_capacity, tmp_path, monkeypatch, capsys, failure):
    local_capacity.write_text(json.dumps(snapshot("ok", NOW)))
    monkeypatch.setattr(
        export,
        "read_published_export",
        lambda: {
            "generated_at": "2033-05-18T03:33:20Z",
            "capacity": export._capacity_block(snapshot("ok", NOW)),
        },
    )
    calls = []

    def fetch(endpoint):
        calls.append(endpoint)
        if failure:
            raise subprocess.CalledProcessError(1, ["gh", "api"])
        return [{"sha": "a"}, {"sha": "b"}]

    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    export.publish_history(now=NOW, state_dir=tmp_path, fetcher=fetch)
    assert calls == [
        "repos/stranske/Orchestrator/commits?sha=exports/route-weights&since=2033-05-17T03:33:20Z&per_page=100"
    ]
    count = "unmeasured" if failure else "2"
    assert f"commits_24h={count} (state flips local: see log) lag=ok" in capsys.readouterr().out
    assert before == {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}


def test_publish_history_cli_cannot_enter_write_path(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "route_weights_export.py",
            "--publish-history",
            "24h",
            "--publish",
            "--state-dir",
            str(tmp_path),
        ],
    )
    monkeypatch.setattr(export, "publish_history", lambda **kw: calls.append(kw))
    monkeypatch.setattr(export, "build_document", lambda *a: pytest.fail("history built an export"))
    monkeypatch.setattr(
        export, "publish_document", lambda *a, **kw: pytest.fail("history published")
    )
    assert export.main() == 0
    assert calls == [{"state_dir": tmp_path}]


def test_publish_history_fetch_paginates_without_mutation(monkeypatch):
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, json.dumps([[{"sha": "a"}], [{"sha": "b"}]]))

    monkeypatch.setattr(export.subprocess, "run", run)
    assert export._fetch_publish_commits(
        "repos/stranske/Orchestrator/commits?sha=exports/route-weights"
    ) == [{"sha": "a"}, {"sha": "b"}]
    assert calls == [
        (
            [
                "gh",
                "api",
                "--paginate",
                "--slurp",
                "repos/stranske/Orchestrator/commits?sha=exports/route-weights",
            ],
            {"check": True, "capture_output": True, "text": True, "timeout": 60},
        )
    ]
