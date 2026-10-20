"""Offline production CLI harness. Only external evidence/provider/queue boundaries are fakes."""

from __future__ import annotations

import contextlib
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULES = ROOT / "src" if (ROOT / "src").is_dir() else ROOT
sys.path.insert(0, str(MODULES))

import closer_dispatcher as d  # noqa: E402
import closer_evidence  # noqa: E402
import closer_resources  # noqa: E402
import dispatcher  # noqa: E402
import router  # noqa: E402


def main():
    spec = json.loads(sys.argv[1])
    target = spec["target"]
    directory = Path(spec["directory"])
    directory.mkdir(parents=True, exist_ok=True)
    os.environ["HANDOFF_DIR"] = spec["common"]
    assignment = directory / "assignment.json"
    assignment.write_text(json.dumps({"targets": [{"target": target}]}))
    workspace = directory / "workspaces.json"
    workspace.write_text(json.dumps({target: str(directory)}))

    def event(name, **values):
        with (directory / "events.jsonl").open("a") as stream:
            stream.write(json.dumps({"event": name, **values}) + "\n")

    def barrier(name):
        if spec.get("hold") == name:
            (directory / (name + ".entered")).touch()
            until = time.time() + 15
            while not (directory / "release").exists():
                if time.time() > until:
                    raise TimeoutError("test barrier timed out")
                time.sleep(0.01)

    class GitHub:
        def __init__(self, *, deadline):
            self.deadline = deadline

        def snapshot(self, name):
            event("prepare", target=name)
            barrier("prepare")
            result = {
                "target": name,
                "kind": "pr",
                "head": "a" * 40,
                "base": "b" * 40,
                "head_branch": spec.get("branch", name),
                "head_repository": spec.get("head_repository", name.split("#")[0]),
                "sources": [
                    {
                        "target": spec.get("source", name.split("#")[0] + "#900"),
                        "state": "open",
                        "body": "## Acceptance criteria\n- Keep behavior",
                    }
                ],
                "state": "OPEN",
                "checks": "SUCCESS",
                "active_threads": 0,
                "review_revision": "r",
                "mergeable": "MERGEABLE",
                "labels": [],
                "body": "Delivery",
                "draft": False,
                "verifier": None,
            }
            if spec.get("pr_target"):
                result["pr_target"] = spec["pr_target"]
            result["revision"] = d.digest(result)
            return result

        def api(self, *a, **kw):
            raise AssertionError("live API forbidden")

    def offload(agent, prompt, **kw):
        event("offload_enter", profile=kw["profile_id"])
        barrier("offload")
        if spec.get("crash"):
            os._exit(9)
        event("offload_exit", profile=kw["profile_id"])
        if spec.get("failure"):
            auth = spec["failure"] == "auth"
            return {
                "exit": 1,
                "run_id": "failed-" + target,
                "error": "HTTP 401 unauthorized" if auth else "quota exhausted",
                "rate_incident_evidence": {
                    "category": "authentication" if auth else "quota",
                    "subcategory": "expired_token" if auth else "quota_exhausted",
                    "evidence_hash": "f" * 64,
                    "is_authoritative": not auth,
                },
            }
        # Actual uncertainty proof deliberately retained by the production pipeline.
        return {
            "exit": 1,
            "run_id": "offline-" + target,
            "outcome_uncertain": True,
            "outcome_status": "uncertain",
            "retry_safe": False,
            "error": "offline fixture",
        }

    if spec.get("instrument_reservation"):

        def instrument(original_reserve):
            @contextlib.contextmanager
            def observed_reserve(*a, **kw):
                event("reservation_attempt")
                (directory / "reservation.attempted").touch()
                with original_reserve(*a, **kw):
                    event("reservation_enter", at=time.time())
                    barrier("reservation")
                    if spec.get("crash_reservation"):
                        os._exit(9)
                    yield
                    if spec.get("uncertain_reservation"):
                        raise ConnectionError("offline unknown action result")
                    event("reservation_exit", at=time.time())

            return observed_reserve

        closer_resources.reserve = instrument(closer_resources.reserve)
        closer_resources.legacy_reserve = instrument(closer_resources.legacy_reserve)

    if spec.get("observe_runlock"):
        import fcntl

        native_flock = fcntl.flock
        runlock = Path(
            str(d._pipeline_state_path(spec["state"], shadow=spec.get("shadow", True))) + ".runlock"
        )

        def observed_flock(handle, flags):
            try:
                return native_flock(handle, flags)
            except BlockingIOError:
                fd = handle if isinstance(handle, int) else handle.fileno()
                if runlock.exists() and (os.fstat(fd).st_dev, os.fstat(fd).st_ino) == (
                    runlock.stat().st_dev,
                    runlock.stat().st_ino,
                ):
                    event("runlock_contested")
                    (directory / "runlock.contested").touch()
                raise

        fcntl.flock = observed_flock

    if spec.get("hold") == "release_boundary":
        native_release = closer_resources.release_legacy_boundary

        def held_release(*a, **kw):
            barrier("release_boundary")
            return native_release(*a, **kw)

        closer_resources.release_legacy_boundary = held_release

    # External campaign permits are a fixture boundary, like fake GitHub/offload.
    # Never depend on a developer's ~/.codex automation being installed in CI.
    d.Closer.campaign_admission = lambda self, target, snapshot, remaining: {
        "allowed": True,
        "timeout": remaining,
    }
    d.GitHub = GitHub
    d._pipeline_capacity = lambda: {"io": 4, "low": 2, "exception": 1}
    d._closer_learned = lambda *_: None
    router.select_agent = lambda _task, cap, **kw: next(
        (
            {"agent": agent, "selected_profile_id": p["profile_id"]}
            for agent in sorted(kw["only"])
            for p in __import__("execution_profiles").profiles_for_agent(agent, transport="offload")
            if cap.get("profiles", {}).get(p["profile_id"], {}).get("state") in {"ok", "warn"}
        ),
        None,
    )
    dispatcher.offload = offload
    closer_evidence.collect_evidence = lambda *a, **kw: {
        "schema": "closer-evidence/v1",
        "producer_sha256": "f" * 64,
        "semantic_cache_key": "closer-evidence-semantic/v1:" + "e" * 64,
        "sections": {},
        "section_digests": {},
        "manifest": {"packet_sha256": "d" * 64},
    }
    closer_evidence.validate_packet = lambda *_: False
    # This process/pool fixture starts after semantic admission, which has its own real-packet tests.
    closer_evidence.action_evidence_readiness = lambda *_a, **_k: {
        "schema": "closer-action-evidence-readiness/v1",
        "action": _a[2],
        "ready": True,
        "mode": "acceptance",
        "gaps": [],
    }
    closer_evidence.drift_sample = lambda *a, **kw: []
    # A shadow completion has no native action. Any accidental queue dispatch fails.
    import closer_queue

    closer_queue.run_round = lambda *a, **kw: (_ for _ in ()).throw(
        AssertionError("native queue forbidden")
    )
    pin = {
        "schema": closer_resources.CONTRACT_SCHEMA,
        "layout": 1,
        "pipeline_schema": "closer-pipeline/v1",
        "coordination_gates": 1,
    }
    args = [
        "run",
        "--assignment",
        str(assignment),
        "--workspaces",
        str(workspace),
        "--state",
        spec["state"],
        "--budget-seconds",
        "20",
    ]
    args += ["--shadow"] if spec.get("shadow", True) else ["--pipeline", "--apply", "--workers"]
    args += ["--coordinator-mode", spec.get("mode", "resource_scoped")]
    if spec.get("mode", "resource_scoped") == "resource_scoped":
        args += ["--coordinator-contract-pin", json.dumps(pin)]
    raise SystemExit(d.main(args))


if __name__ == "__main__":
    main()
