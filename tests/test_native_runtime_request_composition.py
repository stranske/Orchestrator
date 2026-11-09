"""Real native publication -> pinned Core -> scoped queue; only GitHub is inert.

The owned qualification children exercise no-effect startup/crash/restart, never
an agent or provider. No campaign, RuntimeBinding, Core store, or queue admits via
an accepted stub. The existing fixtures' model-only admission stub is restored.
"""

import base64
import hashlib
import json
import subprocess
import time
from pathlib import Path

import pytest
from test_closer_merge_successor_integration import public_merge_world as public_merge_world
from test_closer_native_capacity import (
    _REAL_FRESH_CAPACITY,
    _REAL_PIPELINE_CAPACITY,
    _REAL_TIME,
)
from test_closer_native_capacity import (
    capacity_host as capacity_host,
)
from test_closer_native_observer import host as host
from test_closer_native_production_composition import _core
from test_closer_reasoning_boundary import native_world as native_world
from test_closer_runtime_binding import Contents

import closer_campaign as campaign
import closer_dispatcher as dispatcher
import closer_frontier_policy as policy
import closer_native_observer as native
import closer_pipeline as pipeline
import closer_resources as resources
import closer_runtime_binding as binding
import execution_profiles

_REAL_CAMPAIGN_ADMISSION = dispatcher.Closer.campaign_admission


@pytest.fixture
def runtime_request(capacity_host, public_merge_world, monkeypatch, tmp_path, request):
    world = public_merge_world
    manifest, manifest_path, _campaign, doc, assignment, clock, invocation = capacity_host
    monkeypatch.setenv("HOME", str(assignment.parents[3]))
    clock[0] = _REAL_TIME()
    monkeypatch.setattr(time, "time", _REAL_TIME)
    monkeypatch.setattr(dispatcher.Closer, "campaign_admission", _REAL_CAMPAIGN_ADMISSION)
    monkeypatch.setattr(execution_profiles, "get_profile", lambda *_: None)
    monkeypatch.setattr(dispatcher, "_fresh_capacity_report", _REAL_FRESH_CAPACITY)
    monkeypatch.setattr(dispatcher, "_pipeline_capacity", _REAL_PIPELINE_CAPACITY)
    manifest["portable"] = _core(tmp_path)
    manifest["package_digest"] = campaign.sha256_json(
        [
            manifest["candidate_sha"],
            sorted(manifest["modules"].items()),
            manifest["runtime_digest"],
            manifest["state_digest"],
            manifest["portable"],
        ]
    )
    manifest["distribution"]["core"]["pinned"] = manifest["portable"]
    publication = manifest["mechanism_contract"]["capacity_publication"]
    publication.update(
        schema="native-scoped-capacity/v2",
        execution_policy=policy.POLICY,
        request_timeout_seconds=30,
        freshness_seconds=900,
    )
    publication.pop("admission_seconds")
    campaign.write_manifest(manifest_path, manifest)
    assert (
        hashlib.sha256(Path(dispatcher.__file__).read_bytes()).hexdigest()
        == manifest["modules"]["closer_dispatcher.py"]
    )
    root = Path(manifest["mechanism_contract"]["observation_root"])
    # This fresh fixture marker has never executed; actual qualification must
    # establish its own signed owned-process receipt for the updated Core pin.
    (root / "native-active.json").unlink()
    native._CURRENT_ENTRY.pop(str(root), None)
    native._CURRENT_ADMISSION.pop(str(root), None)
    world.inert_caller.register_native_fixture(manifest, manifest_path)
    with world.inert_caller.native_fixture_environment():
        qualified = native.supervise_native(manifest, manifest_path, budget_seconds=45)
    assert qualified["passed"] and qualified["desktop_qualified"] is False
    assert campaign.qualify(manifest, manifest_path.parent) == []

    world.snapshot.update(
        state="MERGED",
        merge="d" * 40,
        merged_at="2026-10-22T00:00:00Z",
        revision="merged-request",
        verifier=None,
        verifier_history_complete=True,
    )
    observer = dispatcher.Closer(
        dispatcher.GitHub(deadline=time.time() + 30),
        state_path=world.raw,
        apply=True,
        workers=True,
    )
    observed = observer.observe("owner/repo#12")
    assert observed["action"] == "post_merge_verify" and observed["eligible"]
    world.frontier = policy.make_frontier_from_observations(
        {"owner/repo#12": observed},
        owner=doc["owner"],
        frontier_id="runtime-request",
    )
    assignment.write_text(json.dumps({"targets": [{"target": "owner/repo#12"}]}))
    doc["worker_config"].update(
        finite_frontier=world.frontier,
        request_timeout_seconds=30,
        execution_policy=policy.POLICY,
    )
    workspace = world.root / "mapped-product"
    if (
        getattr(request.node, "callspec", None)
        and request.node.callspec.params.get("checkout") == "held"
    ):
        workspace.mkdir()
    world.runtime_workspace = workspace
    workspace_config = world.root / "runtime-workspaces.json"
    workspace_config.write_text(
        json.dumps(
            {
                "schema": "closer-workspace-bindings/v1",
                "historical": {},
                "execution": {"owner/repo#12": str(workspace)},
            }
        )
    )
    doc["worker_config"]["workspaces"] = {
        "path": str(workspace_config),
        "sha256": hashlib.sha256(workspace_config.read_bytes()).hexdigest(),
    }
    invocation(2)
    native._CURRENT_ADMISSION[str(root)].pop("original_deadline")
    native._CURRENT_ADMISSION[str(root)]["execution_policy"] = policy.POLICY
    context = native.publish_admission(manifest, manifest_path, _campaign.path, doc)
    assert native.validate_admission(manifest, context, for_effects=True) == context
    monkeypatch.setenv("CLOSER_CAPACITY_REPORT", context["report"]["path"])
    assert dispatcher._pipeline_capacity() == {"io": 4, "low": 0, "exception": 0}

    contents = Contents()
    core_requests = []

    def core_http(self, method, path, body):
        if self is not None:
            assert type(self.bundle) is binding.CoreBundle
            assert self.bundle.root == Path(manifest["portable"]["package_root"])
        core_requests.append((type(self), method, path))
        if method == "GET" and path == "/repos/owner/repo/pulls/12":
            return {
                "head": {"sha": world.snapshot["head"]},
                "merged": True,
                "merge_commit_sha": world.snapshot["merge"],
                "labels": [{"name": name} for name in world.snapshot["labels"]],
            }
        return contents(method, path, body)

    # External GitHub Contents/CAS transport only; the actual HTTP adapter,
    # RuntimeBinding factory/Core loader/store/fence/submit/reconcile all run.
    monkeypatch.setattr(binding._CoreHTTPTransport, "__call__", core_http)
    original_run = subprocess.run

    def native_read(argv, *args, **kwargs):
        if argv[0] == "gh":
            assert argv == [
                "gh",
                "api",
                "--method",
                "GET",
                "--paginate",
                "--slurp",
                "repos/owner/repo/pulls/12",
            ]  # No unmocked network or mutation command may escape.
            value = core_http(None, "GET", "/repos/owner/repo/pulls/12", None)
            return subprocess.CompletedProcess(argv, 0, json.dumps([value]), "")
        return original_run(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", native_read)
    monkeypatch.setenv("GH_TOKEN", "offline-never-sent")
    world.argv[world.argv.index("--assignment") + 1] = context["assignment"]["path"]
    world.argv[world.argv.index("--finite-frontier-json") + 1] = json.dumps(world.frontier)
    world.argv[world.argv.index("--coordinator-contract-pin") + 1] = json.dumps(
        pipeline.scoped_capability()
    )
    world.argv += [
        "--runtime-manifest",
        str(manifest_path),
        "--workspaces",
        context["workspace_config"]["path"],
        "--workspaces-sha256",
        context["workspace_config"]["sha256"],
    ]
    return world, manifest, manifest_path, context, contents, core_requests


@pytest.mark.parametrize("checkout", ["held", "absent"])
def test_native_publication_runtime_core_scoped_queue_requests_without_models(
    runtime_request, monkeypatch, capsys, checkout
):
    world, manifest, path, context, contents, core_requests = runtime_request
    workspace = world.runtime_workspace
    assert workspace.is_dir() == (checkout == "held")
    assert dispatcher.Closer.campaign_admission is _REAL_CAMPAIGN_ADMISSION
    assert native.validate_admission(manifest, context, for_effects=True) == context
    with resources._workspace_locks(
        resources.scoped_root(world.ledger),
        ["worktree:" + str(workspace)],
        deadline=time.time() + 3,
        now=time.time,
    ):
        assert dispatcher.main(world.argv) == 78
    result = json.loads(capsys.readouterr().out)
    assert len(world.verifies) == 1 and not world.merges, result["targets"]
    assert world.events["provider"] == 0
    assert contents.writes > 0 and core_requests
    assert any(item[0] is binding._CoreHTTPTransport for item in core_requests)
    assert all(item[0] in {binding._CoreHTTPTransport, type(None)} for item in core_requests)
    records = [json.loads(base64.b64decode(row["content"])) for row in contents.rows.values()]
    assert any(row.get("action") == "verify_request" for row in records), records
    completed = next(row for row in records if row.get("status") == "succeeded")
    intent = completed["mutation_intent"]["native_binding"]
    assert intent["action"] == "post_merge_verify"
    assert intent["pr_target"] == "owner/repo#12"
    assert intent["head"] == world.snapshot["head"] and intent["merge"] == world.snapshot["merge"]
    assert all(p.read_bytes() == before for p, before in world.originals.items())


def test_changed_runtime_bytes_hold_before_core_or_native_effect(runtime_request, capsys):
    world, manifest, _path, _context, contents, core_requests = runtime_request
    pinned = Path(manifest["module_dir"]) / "closer_dispatcher.py"
    pinned.write_text(pinned.read_text() + "\n# changed runtime binding\n")
    with pytest.raises(campaign.CampaignError, match="digest differs from manifest"):
        dispatcher.main(world.argv)
    capsys.readouterr()
    assert not world.verifies and not world.merges and world.events["provider"] == 0
    assert not contents.writes and not core_requests
