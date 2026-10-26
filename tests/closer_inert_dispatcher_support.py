"""Test-only scoped caller inputs and genuine inert broker execution.

No result/origin/qualification validator is replaced. The sole executable is a
fixture-generated native-result emitter; ps is limited to this test/its children.
"""

import copy
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from test_closer_role_qualification import record

import adapters
import closer_claude_auth
import closer_data_only_execution as execution
import closer_dispatcher as closer
import closer_evidence as evidence
import closer_frontier_policy as policy
import closer_pipeline as pipeline
import closer_role_qualification as roles
import dispatcher
import execution_profiles

REAL_POPEN, REAL_RUN = subprocess.Popen, subprocess.run
REAL_QUALIFIED = roles.is_runtime_qualified
REAL_EXECUTE = closer._execute_profile_route
REAL_STAGED = closer._run_staged_pipeline
REAL_GITHUB = closer.GitHub
REAL_BROKER = dispatcher.offload
REAL_COLLECT = evidence.collect_evidence
LOW = "claude-sonnet-5-5-low"
SECOND = "claude-sonnet-5-5-high-v2"


def fixture_api_transport(api, argv, *, read_text_fn=None, **kwargs):
    """Translate only the source GitHub.api protocol into explicit fixture I/O."""
    if (
        not isinstance(argv, (list, tuple))
        or len(argv) < 4
        or list(argv[:2]) != ["api", "--method"]
        or not isinstance(argv[2], str)
        or argv[2] not in {"GET", "POST", "PATCH", "PUT", "DELETE"}
        or not isinstance(argv[3], str)
        or not argv[3]
        or argv[3].startswith(("-", "/"))
        or "://" in argv[3]
        or any(ch.isspace() for ch in argv[3])
        or set(kwargs) - {"resource", "timeout_s", "raw_body"}
        or type(kwargs.get("raw_body", False)) is not bool
        or len(argv[4:]) % 2
    ):
        raise AssertionError("unsupported fixture GitHub argv or transport option")
    fields = {}
    for flag, value in zip(argv[4::2], argv[5::2], strict=True):
        if flag != "-f" or not isinstance(value, str) or "=" not in value:
            raise AssertionError("unsupported fixture GitHub field option")
        name, field = value.split("=", 1)
        if not name or name in fields:
            raise AssertionError("ambiguous fixture GitHub field")
        fields[name] = field
    method, endpoint = argv[2:4]
    if kwargs.get("raw_body"):
        if method != "GET" or fields or read_text_fn is None:
            raise AssertionError("explicit fixture GET text reader required")
        body = read_text_fn(endpoint)
    else:
        body = api(endpoint, method=method, fields=fields or None)
    return 0, body  # The unchanged source GitHub.api validates body/status/type.


class InertCaller:
    def __init__(self, world, monkeypatch):
        self.world, self.patch = world, monkeypatch
        self.results, self.prompts = [], []
        self.timeout_seconds = 45
        self.children = {os.getpid()}
        self.control = world.root / "inert-response.json"
        script = world.cli.read_text()
        script = script.replace(
            "mode='success'",
            "control=json.loads(Path("
            + repr(str(self.control))
            + ").read_text())\nmode=control.get('mode','success')",
        )
        script = script.replace(
            "final=json.dumps({'classification':'relevant','authority':False,'evidence':[]})",
            "final=control['output']",
        )
        script = script.replace(
            "if mode=='resumed':row['resumed']=True",
            "if mode=='resumed':row['resumed']=True\nif mode=='native_error':row.update(is_error=True,subtype='error_during_execution')",
        )
        script = script.replace(
            "print(json.dumps(row))",
            "\nif control.get('exit',0):\n print(control.get('error','inert failure'),file=sys.stderr)\n if final: print(json.dumps(row))\n raise SystemExit(control['exit'])\nprint(json.dumps(row))",
        )
        world.cli.write_text(script)
        self.binary = hashlib.sha256(world.cli.read_bytes()).hexdigest()
        self.control.write_text(json.dumps({"output": "{}"}))
        self.activate()

    def activate(self):
        patch, world = self.patch, self.world
        patch.setitem(sys.modules, "dispatcher", dispatcher)
        patch.setattr(adapters, "DATA_ONLY_CLAUDE_SHA256", self.binary)
        patch.setattr(execution, "CLAUDE_BINARY_SHA256", self.binary)
        patch.setattr(roles, "is_runtime_qualified", REAL_QUALIFIED)
        records = {}
        for profile_id in (LOW, SECOND):
            profile = execution_profiles.get_profile(profile_id)
            for task in ("closer_interpret", "closer_exception"):
                descriptor = closer._controlled_model_contract(task)
                name = profile_id + ":" + task
                value = record(profile, roles.TASK_ROLES[task])
                value.update(
                    qualification_id=name,
                    qualified_at=time.time() - 10,
                    expires_at=time.time() + 600,
                    execution_limits={
                        **world.args["data_only_limits"],
                        "role": roles.TASK_ROLES[task],
                        "model_contract_sha256": descriptor["contract_sha256"],
                        "timeout_seconds": self.timeout_seconds,
                    },
                    production_model_contract=descriptor,
                )
                value["confinement"]["adapter_build_sha256"] = self.binary
                records[name] = value
        patch.setattr(roles, "ROLE_QUALIFICATIONS", records)
        patch.setattr(
            closer_claude_auth,
            "borrow_claude_oauth_token",
            lambda **kw: world.args["trusted_auth_supplier"](),
        )
        patch.setattr(closer, "_execute_profile_route", REAL_EXECUTE)
        patch.setitem(sys.modules, "dispatcher", dispatcher)
        patch.setattr(subprocess, "Popen", self.popen)
        patch.setattr(subprocess, "run", self.run)
        if not (Path(os.environ["HANDOFF_DIR"]) / "capacity.json").exists():
            self.seed_capacity()

    def seed_capacity(self):
        handoff = Path(os.environ["HANDOFF_DIR"])
        handoff.mkdir(parents=True, exist_ok=True)
        profiles = {
            p: {"state": "ok", "capacity_pool_ids": ["claude-subscription"]} for p in (LOW, SECOND)
        }
        (handoff / "capacity.json").write_text(
            json.dumps(
                {
                    "generated_at": time.time(),
                    "agents": {"claude": {"state": "ok"}},
                    "profiles": profiles,
                    "pools": {"claude-subscription": {"state": "ok"}},
                }
            )
        )

    def allowed(self, command):
        command = list(command)
        return command and (
            command[0] == str(self.world.cli)
            or (
                Path(command[0]).name == "ps"
                and command[1:2] == ["-p"]
                and len(command) == 5
                and command[2].isdigit()
                and int(command[2]) in self.children
                and command[3:] == ["-o", "lstart="]
            )
        )

    def popen(self, command, **kwargs):
        assert not kwargs.get("shell") and self.allowed(command), ("forbidden subprocess", command)
        child = REAL_POPEN(command, **kwargs)
        if command[0] == str(self.world.cli):
            self.children.add(child.pid)
        return child

    def run(self, command, **kwargs):
        assert self.allowed(command), ("forbidden subprocess", command)
        return REAL_RUN(command, **kwargs)

    def response(self, result, agent, prompt, **kwargs):
        assert agent == "claude" and kwargs["profile_id"] in (LOW, SECOND)
        self.prompts.append(prompt)
        control = {
            "output": result.get("output", ""),
            "exit": result.get("exit", 0),
            "error": result.get("output") or result.get("error", "inert failure"),
        }
        if result.get("outcome_uncertain") and not result.get("completion_persistence"):
            control.update(exit=0, mode="uncertain")
        if result.get("raw_exit") == 0 and result.get("exit") == 70:
            control["exit"] = 0
        if result.get("_native_mode"):
            control.update(mode=result["_native_mode"], exit=0)
        self.control.write_text(json.dumps(control))
        if result.get("rate_incident_evidence"):
            control["error"] = "quota exceeded: usage limit reached"
            if result.get("raw_exit") == 0:
                control["output"] = "OFFLOAD_INCOMPLETE: quota exceeded: usage limit reached"
            self.control.write_text(json.dumps(control))
        with self.patch.context() as local:
            if result.get("completion_persistence"):
                import sqlite3

                def busy(*args, **kwargs):
                    raise sqlite3.OperationalError("database is locked")

                local.setattr(dispatcher.feedback, "complete_profile_attempt", busy)
                local.setattr(dispatcher.feedback, "complete_profile_attempt_unresolved", busy)
            actual = REAL_BROKER(agent, prompt, **kwargs)
        self.results.append(actual)
        return actual

    def frontier(self, snapshots, backend=None):
        bindings = {}
        for target, snap in snapshots.items():
            if backend:
                observed = backend.observe(target)
                action_id = policy.action_identity(
                    target, snap["revision"], observed["action"], observed["operation_binding"]
                )
            else:
                action_id = hashlib.sha256(json.dumps(snap, sort_keys=True).encode()).hexdigest()
            bindings[target] = {"revision": snap["revision"], "action_id": action_id}
        return policy.make_frontier(
            list(snapshots),
            owner="inert-fixture-owner",
            frontier_id="inert-fixture-frontier",
            bindings=bindings,
        )

    def bind_worker(self, backend):
        self.activate()
        if "claude-sonnet-5-5-low" not in json.loads(
            (Path(os.environ["HANDOFF_DIR"]) / "capacity.json").read_bytes()
        ).get("profiles", {}):
            self.seed_capacity()
        original = backend.worker

        def worker(target, action, snap, operation, **kwargs):
            if action == "repair":
                return original(target, action, snap, operation, **kwargs)
            packet = self.world.root / (
                "advisory-" + hashlib.sha256(target.encode()).hexdigest() + ".json"
            )
            packet.write_text(
                json.dumps(
                    {
                        "target": target,
                        "revision": snap["revision"],
                        "sources": snap.get("sources", []),
                    }
                )
            )
            context = {
                **(kwargs.pop("advisory_context", None) or {}),
                "packet_ref": {
                    "path": str(packet),
                    "sha256": hashlib.sha256(packet.read_bytes()).hexdigest(),
                },
            }
            op = {**operation, "finite_frontier": self.frontier({target: snap})}
            return original(target, action, snap, op, advisory_context=context, **kwargs)

        backend.worker = worker
        return backend

    def temporary_campaign_guard(self):
        """Create only beneath this supplied physical pytest root, never OS home."""
        import pwd
        import stat

        root = self.world.root
        assert root.is_absolute() and root.resolve(strict=True) == root
        home = root / "private-home"
        assert Path(os.environ["HOME"]).resolve() == home
        account_home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
        assert root != account_home and not root.is_relative_to(account_home)
        guard = home / ".codex/automations/model-allocation/assess.py"
        assert guard.is_relative_to(root) and not guard.is_relative_to(account_home)
        current = Path(guard.anchor)
        for component in guard.parts[1:]:
            current = current / component
            assert not current.is_symlink()
            if current.exists():
                assert current.resolve(strict=True) == current
        guard.parent.mkdir(parents=True, exist_ok=True)
        assert guard.parent.resolve(strict=True) == guard.parent
        data = b"def enforce_campaign_budget(args):\n    args.timeout=min(args.timeout,30)\n"
        if getattr(self, "created_guard", None) == guard:
            assert stat.S_ISREG(guard.stat(follow_symlinks=False).st_mode)
            assert guard.read_bytes() == data
            return guard
        fd = os.open(guard, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        self.created_guard = guard
        return guard

    def staged(
        self,
        args,
        targets,
        backend,
        deadline,
        *,
        deterministic=False,
        snapshot_fn=None,
        read_text_fn=None,
    ):
        """Use the unchanged CLI backend constructor for finite model positives."""
        snapshots = {
            target: (
                copy.deepcopy(snapshot_fn(target))
                if snapshot_fn is not None
                else closer.GitHub(deadline=deadline).snapshot(target)
            )
            for target in targets
        }
        if deterministic:
            return REAL_STAGED(args, targets, backend, deadline)
        import argparse
        from types import SimpleNamespace

        import closer_resources as resources

        self.temporary_campaign_guard()
        module = sys.modules["closer_pipeline"]
        fake = module.run_pipeline
        for key, value in vars(pipeline).items():
            if not hasattr(module, key):
                setattr(module, key, value)
        captured = {}

        def scoped(path, selected, **callbacks):
            actual_backend = dict(
                zip(
                    callbacks["prepare"].__code__.co_freevars,
                    (cell.cell_contents for cell in callbacks["prepare"].__closure__),
                )
            )["backend"]
            captured["backend"] = actual_backend
            import contextlib

            root = resources.open_root(path, pipeline_schema=pipeline.SCHEMA)
            with contextlib.ExitStack() as stack:
                leases = {}
                for target in selected:
                    lease = resources.acquire(
                        root,
                        target,
                        owner="inert-stage-fixture",
                        finite_frontier=admitted.finite_frontier,
                        request_deadline=time.time() + 30,
                    )
                    assert lease is not None
                    stack.callback(lease.release)
                    leases[target] = lease
                for stage in ("prepare", "interpret", "exception", "act", "reconcile"):
                    if stage not in callbacks:
                        continue
                    original = callbacks[stage]

                    def invoke(target, context, fn=original):
                        shard = resources.shard_dir(root, target) / "state.json"
                        scoped = {
                            **context,
                            "work_key": context.get("work_key", "inert-work-" + target),
                        }
                        scoped.setdefault("state_path", str(shard))
                        scoped.setdefault(
                            "action_state_path",
                            str(
                                shard.parent
                                / "state.actions"
                                / (pipeline._digest(target) + ".json")
                            ),
                        )
                        with resources.fenced(leases[target]):
                            return fn(target, scoped)

                    callbacks[stage] = invoke
                captured["result"] = fake(path, selected, **callbacks)
                return captured["result"]

        assignment = self.world.root / "pipeline-assignment.json"
        assignment.write_text(json.dumps({"targets": [{"target": target} for target in targets]}))
        dispositions = self.world.root / "pipeline-dispositions.json"
        dispositions.write_text(json.dumps(backend.dispositions))
        admitted = SimpleNamespace(
            **{
                **vars(args),
                "assignment": str(assignment),
                "budget_seconds": None,
                "finite_frontier": self.frontier(snapshots, backend),
                "request_timeout_seconds": 60.0,
                "coordinator_mode": "resource_scoped",
                "pipeline": True,
                "apply": True,
                "workers": True,
                "worker_timeout": 60,
                "runtime_manifest": None,
                "dispositions": str(dispositions),
                "workspaces": None,
            }
        )
        with self.patch.context() as local:
            local.setattr(closer, "GitHub", REAL_GITHUB)
            local.setattr(
                REAL_GITHUB,
                "snapshot",
                lambda self, target: copy.deepcopy(
                    snapshot_fn(target) if snapshot_fn is not None else snapshots[target]
                ),
            )
            local.setattr(
                closer.gh_capacity,
                "gh_run",
                lambda argv, **kwargs: fixture_api_transport(
                    backend.gh.api,
                    argv,
                    read_text_fn=read_text_fn or getattr(backend.gh, "read_text", None),
                    **kwargs,
                ),
            )
            local.setattr(closer, "known_workspaces", lambda targets: backend.workspaces)
            local.setattr(module, "run_pipeline", scoped)
            with resources.coordination_admission(
                closer._pipeline_state_path(args.state), targets, mode="resource_scoped"
            ) as admission:
                admitted._coordination_admission = admission
                closer._main_admitted(
                    admitted,
                    argparse.ArgumentParser(),
                    {target: {"target": target} for target in targets},
                    targets,
                )
        actual = captured["backend"]
        backend.dispositions = actual.dispositions
        backend.validated_nonpass_waits = actual.validated_nonpass_waits
        return captured["result"]

    def packet(self, snap, root):
        import io
        import zipfile

        stream = io.BytesIO()
        criterion = closer.requirements(snap["sources"][0]["body"])[0]
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr(
                "junit.xml", '<testsuite tests="1"><testcase name="' + criterion + '"/></testsuite>'
            )
        number = int(snap["target"].split("#")[1])

        def api(endpoint, **kwargs):
            if "/compare/" in endpoint:
                return {
                    "status": "ahead",
                    "files": [
                        {
                            "filename": "behavior.py",
                            "status": "added",
                            "patch": "+def preserve_behavior(): return True",
                            "sha": "c" * 40,
                        }
                    ],
                }
            if "/actions/runs?" in endpoint:
                return {"workflow_runs": [{"id": 7, "head_sha": snap["head"]}]}
            if endpoint.endswith("/runs/7"):
                return {
                    "id": 7,
                    "run_attempt": 2,
                    "head_sha": snap["head"],
                    "status": "completed",
                    "conclusion": "success",
                    "pull_requests": [{"number": number}],
                }
            if endpoint.endswith("/runs/7/artifacts?per_page=100"):
                return {
                    "artifacts": [
                        {"id": 9, "name": "tests", "size_in_bytes": 100, "expired": False}
                    ]
                }
            if "/artifacts?" in endpoint:
                return {"artifacts": []}
            if "/actions/runs/" in endpoint:
                return {
                    "id": int(endpoint.rsplit("/", 1)[1]),
                    "run_attempt": 1,
                    "head_sha": snap["head"],
                    "status": "completed",
                    "conclusion": "success",
                    "pull_requests": [{"number": number}],
                }
            raise AssertionError(endpoint)

        packet = REAL_COLLECT(snap, api, root, artifact_fetch=lambda *_a, **_k: stream.getvalue())
        path = Path(root) / "manifests" / (packet["manifest"]["packet_sha256"] + ".json")
        raw = packet["sections"]["ci_artifacts"]["content"]["artifacts"][0]["receipts"][0]["raw"]
        proof = {
            "evidence_sha256": raw["sha256"],
            "artifact_id": 9,
            "run_id": 7,
            "run_attempt": 2,
            "head_sha": snap["head"],
            "kind": "junit",
            "receipt_path": "junit.xml",
            "excerpt": criterion,
            "relevance": "Actual passing testcase exercises " + criterion,
        }
        rows = [
            {
                "source": source["target"],
                "criterion": value,
                "result": "PASS",
                "evidence": [raw],
                "proofs": [proof],
            }
            for source in snap["sources"]
            for value in [source["body"], *closer.requirements(source["body"])]
        ]
        proposal = {
            "target": snap["target"],
            "head": snap["head"],
            "base": snap["base"],
            "classification": "relevant",
            "substantive_needs": [],
            "findings": [],
            "decision": {"criteria": rows},
        }
        return (
            packet,
            {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()},
            proposal,
        )

    def interpretation(self, snap, packet, ref, proposal, *, native_mode=None):
        import closer_model_contracts as model
        import closer_resources as resources

        readiness = evidence.action_evidence_readiness(
            snap, packet, "collect_evidence", retained_dir=Path(ref["path"]).parent.parent
        )
        prompt = model.render_classification_prompt(
            {
                "target": snap["target"],
                "head": snap["head"],
                "base": snap["base"],
                "packet_ref": ref,
                "snapshot_revision": snap["revision"],
                "manifest": packet["manifest"],
                "section_refs": packet["sections"],
                "data_only_evidence": closer.data_only_evidence_context(
                    packet, readiness=readiness, snapshot=snap
                ),
            },
            source_rows=packet["sections"]["source"]["content"],
            diff_rows={},
            review_rows=[],
            artifact_rows={},
            data_only=True,
            readiness=readiness,
        )
        frontier = self.frontier({snap["target"]: snap})
        with resources.pool_slot(
            self.world.root / "pools",
            hashlib.sha256(b"claude-subscription").hexdigest(),
            1,
            deadline=time.time() + 10,
            finite_frontier=True,
            reservation={
                "reservation_id": "original-act-fixture",
                "target": snap["target"],
                "frontier_id": frontier["frontier_id"],
                "agent": "claude",
                "profile_id": LOW,
            },
        ):
            result = closer._controlled_offload(
                agent="claude",
                prompt=prompt,
                profile_id=LOW,
                task_type="closer_interpret",
                operation_id="original-act-interpretation",
                frontier=frontier,
                target=snap["target"],
                basis={
                    "target": snap["target"],
                    "revision": snap["revision"],
                    "head": snap["head"],
                    "base": snap["base"],
                    "packet_sha256": ref["sha256"],
                },
                deadline=time.time() + 30,
                offload=lambda agent, prompt, **kwargs: self.response(
                    {"exit": 0, "output": json.dumps(proposal), "_native_mode": native_mode},
                    agent,
                    prompt,
                    **kwargs,
                ),
                mode="assess",
            )
        if native_mode is not None:
            return result
        assert result["exit"] == 0, result
        return {
            "actual": closer._worker_actual(result),
            "proposal": closer._worker_proposal(result, agent="claude"),
            "requested_agent": "claude",
            "requested_profile": LOW,
            "substantive_needs": [],
        }
