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
    published = {"capacity": export._capacity_block(snapshot("shed", NOW - 3600))}
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
    monkeypatch.setattr(
        export, "publish_lag", lambda original=export.publish_lag: original(now=NOW)
    )
    monkeypatch.setattr(sys, "argv", ["route_weights_export.py", "--publish-lag"])
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}
    assert export.main() == 0
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()} == before
    out = capsys.readouterr().out
    assert "local capacity age=10s" in out
    expected = {
        "sync": "published=shed published_age=3600s lag=ok",
        "different": "published=ok published_age=3600s lag=STALE",
        "unmeasured": "published=unmeasured published_age=unmeasureds lag=STALE",
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
    assert result.stdout.count("OBSERVED") == 1
    assert ("SUCCESS" in result.stdout) == (due == 0 and attempt == 0)
    assert ("seen=1/2" in result.stdout) == (due == 0 and attempt == 0)
