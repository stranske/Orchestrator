"""No-network production composition: native helpers, real CLI callbacks and Core.

Only external executable/HTTP responses and the clock are synthetic. The actual
backend, selection, callback registration, Core authority and cleanup remain used.
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from test_closer_bounded_native_hooks import canonical_postrun, renderer
from test_closer_native_capacity import capacity_host as capacity_host
from test_closer_native_lease import _minimal_prerun
from test_closer_native_observer import host as host

import closer_campaign as campaign
import closer_frontier_policy as policy
import closer_native_observer as native
import closer_runtime_binding as binding

REAL_TIME = time.time

# This same transcript is consumed by the owned gh executable and by Core's
# documented external HTTP opener seam. Unexpected operations never use network.
TRANSCRIPT = r"""
import base64,datetime,hashlib,json,os,re,sys,time,urllib.error,urllib.parse,urllib.request
from pathlib import Path
STATE=Path(os.environ['COMPOSITION_STATE'])
OFFSET=Path(os.environ['COMPOSITION_OFFSET'])
LOG=Path(os.environ['COMPOSITION_LOG'])
def load(): return json.loads(STATE.read_text())
def save(value): STATE.write_text(json.dumps(value,sort_keys=True))
def event(kind,method,path,body=None):
    with LOG.open('a') as f: f.write(json.dumps({'kind':kind,'method':method,'path':path,'body_keys':sorted(body or {}),'time':time.time()})+'\n')
def _response(method,path,body=None):
    state=load(); parsed=urllib.parse.urlsplit(path); path=parsed.path; body=body or {}
    if not re.match(r'^/repos/offline/repo[12](?:/|$)',path) and path!='/graphql': raise AssertionError('unexpected synthetic URL '+path)
    match=re.search(r'/(?:issues|pulls)/(\d+)',path)
    number=int(match[1]) if match else None
    if path=='/graphql':
        query=body.get('query',''); match=re.search(r'pullRequest\(number:\s*(\d+)',query)
        if not match: raise AssertionError('unexpected GraphQL transcript')
        number=int(match[1]); label=bool(state['labels'].get(str(number)))
        return {'data':{'repository':{'pullRequest':{
            'number':number,'body':'Synthetic verification request fixture','state':'MERGED','isDraft':False,
            'headRefOid':'a'*40,'headRefName':'fixture','headRepository':{'nameWithOwner':'offline/repo'+str(number)},'baseRefOid':'b'*40,
            'mergeCommit':{'oid':'c'*40},'mergedAt':'2026-01-01T00:00:00Z','updatedAt':'2026-01-01T00:00:00Z',
            'mergeable':'MERGEABLE','mergeStateStatus':'CLEAN',
            'labels':{'nodes':[{'name':'verify:compare'}] if label else [],'pageInfo':{'hasNextPage':False}},
            'timelineItems':{'nodes':[{'id':'event-'+str(number),'createdAt':state['labels'].get(str(number)),'label':{'name':'verify:compare'}}] if label else [],'pageInfo':{'hasPreviousPage':False}},
            'closingIssuesReferences':{'nodes':[],'pageInfo':{'hasNextPage':False}},
            'reviewThreads':{'nodes':[],'pageInfo':{'hasNextPage':False,'endCursor':None}},
            'commits':{'nodes':[{'commit':{'oid':'a'*40,'statusCheckRollup':{'state':'SUCCESS','contexts':{'nodes':[],'pageInfo':{'hasNextPage':False}}}}}]},
            'comments':{'nodes':[],'pageInfo':{'hasPreviousPage':False}}
        }}}}
    if method=='GET' and re.fullmatch(r'/repos/offline/repo[12]/issues/\d+',path):
        return {'number':number,'state':'closed','body':'Synthetic verification request fixture','pull_request':{},'labels':[{'name':'verify:compare'}] if state['labels'].get(str(number)) else []}
    if method=='GET' and re.fullmatch(r'/repos/offline/repo[12]/pulls/\d+',path):
        return {'number':number,'head':{'sha':'a'*40},'base':{'sha':'b'*40},'merged':True,'merge_commit_sha':'c'*40,'labels':[{'name':'verify:compare'}] if state['labels'].get(str(number)) else []}
    if method=='POST' and re.fullmatch(r'/repos/offline/repo[12]/issues/\d+/labels',path):
        if body!={'labels[]':'verify:compare'}: raise AssertionError('unexpected label mutation')
        if state['labels'].get(str(number)): raise AssertionError('duplicate synthetic label request')
        state['labels'][str(number)]=datetime.datetime.fromtimestamp(time.time(),datetime.timezone.utc).isoformat().replace('+00:00','Z'); save(state)
        return [{'name':'verify:compare'}]
    if method=='GET' and '/actions/' in path and path.endswith('/artifacts'): return {'total_count':0,'artifacts':[]}
    if method=='GET' and '/actions/' in path and path.endswith('/runs'): return {'total_count':0,'workflow_runs':[]}
    if method=='GET' and (path.endswith('/files') or path.endswith('/comments') or path.endswith('/reviews')): return []
    if method=='GET' and '/compare/' in path: return {'files':[],'status':'ahead','ahead_by':1,'behind_by':0,'commits':[]}
    if method=='GET' and re.fullmatch(r'/repos/offline/repo[12]',path): return {'default_branch':'main'}
    if method=='GET' and '/git/ref/' in path: return {'object':{'sha':'d'*40}}
    if method=='POST' and path.endswith('/git/refs'): return {'ref':body['ref'],'object':{'sha':body['sha']}}
    if '/contents/.github/' in path:
        key=path; old=state['core'].get(key)
        if method=='GET':
            if old is None: raise urllib.error.HTTPError('https://api.github.com'+path,404,'fixture missing',{},None)
            return old
        if method=='PUT':
            if (old or {}).get('sha')!=body.get('sha'): raise urllib.error.HTTPError('https://api.github.com'+path,409,'fixture CAS conflict',{},None)
            sha=hashlib.sha1(body['content'].encode()).hexdigest()
            state['core'][key]={'sha':sha,'encoding':'base64','content':body['content']};save(state)
            return {'content':{'sha':sha}}
    if method=='GET' and '/git/blobs/' in path:
        for value in state['core'].values():
            if value['sha']==path.rsplit('/',1)[-1]: return value
    raise AssertionError('unhandled synthetic operation '+method+' '+path)
def response(method,path,body=None):
    import fcntl
    with STATE.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        return _response(method,path,body)
"""


def _core(tmp_path):
    from closer_core_support import core_source

    supplied = os.environ.get("CLOSER_CORE_PACKAGE")
    source = Path(supplied) if supplied else core_source()
    if not (source / "scripts/closer_runtime.py").is_file():
        pytest.skip("independently pinned Workflows Core source/package unavailable")
    destination = tmp_path / "closed-core"
    files = {}
    for name in binding.CORE_FILES:
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        data = (source / name).read_bytes()
        target.write_bytes(data)
        files[name] = hashlib.sha256(data).hexdigest()
    if (source / "closer-core-package.json").is_file():
        descriptor = json.loads((source / "closer-core-package.json").read_text())
    else:
        commit = subprocess.check_output(
            ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
        ).strip()
        descriptor = {
            "schema_version": 1,
            "package_kind": "closer-core",
            "owner": "Workflows",
            "contract": campaign.CONTRACT,
            "core_version": "1",
            "entrypoints": {"runtime": "scripts/closer_runtime.py"},
            "source_commit": commit,
            "files": files,
            "dependencies": binding.CORE_DEPENDENCIES,
        }
        descriptor["package_digest"] = binding._digest(
            [
                "closer-core",
                "Workflows",
                "1",
                campaign.CONTRACT,
                commit,
                sorted(files.items()),
                sorted(binding.CORE_DEPENDENCIES.items()),
            ]
        )
    (destination / "closer-core-package.json").write_text(json.dumps(descriptor))
    pin = {"package_root": str(destination), "package_digest": descriptor["package_digest"]}
    binding.CoreBundle(pin)
    assert binding.dependency_problems() == []
    return pin


def _fixture_worker_cleanup(obj, manifest, handoff, receipt_path):
    """Terminate only this fixture's exact private argv/process group on failure."""
    import signal

    import closer_native_lease

    terminated = []
    for row in (obj.read() or {}).get("workers", []):
        pid = row.get("pid")
        if type(pid) is not int or pid <= 1:
            continue
        if campaign.result_path(obj.path, row["worker_id"]).exists():
            until = time.monotonic() + 3
            while time.monotonic() < until:
                status = subprocess.run(
                    ["ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True, timeout=2
                )
                if status.returncode or status.stdout.strip().startswith("Z"):
                    break
                time.sleep(0.05)
            if status.returncode or status.stdout.strip().startswith("Z"):
                continue
        command = ["ps", "-p", str(pid), "-o", "lstart=", "-o", "command="]
        before = subprocess.run(command, capture_output=True, text=True, timeout=5)
        if before.returncode or not before.stdout.strip():
            continue
        expected = [
            str(obj.path),
            str(campaign.result_path(obj.path, row["worker_id"])),
            manifest["module_path"],
        ]
        if not all(value in before.stdout for value in expected):
            raise AssertionError("test worker identity changed; refusing cleanup")
        if os.getpgid(pid) != pid:
            raise AssertionError("test worker does not own its expected process group")
        again = subprocess.run(command, capture_output=True, text=True, timeout=5)
        if again.returncode:
            continue
        if again.stdout != before.stdout:
            raise AssertionError("test worker birth/argv changed before cleanup")
        os.killpg(pid, signal.SIGTERM)
        limit = time.monotonic() + 5
        while time.monotonic() < limit:
            status = subprocess.run(
                ["ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True, timeout=2
            )
            if status.returncode or status.stdout.strip().startswith("Z"):
                break
            time.sleep(0.05)
        else:
            current = subprocess.run(command, capture_output=True, text=True, timeout=5)
            if current.returncode == 0 and current.stdout == before.stdout:
                os.killpg(pid, signal.SIGKILL)
        terminated.append(pid)
    lane = Path(handoff) / ".lane-closer.lock"
    exact_cleanup = None
    if (lane / "meta").is_file():
        token = (lane / "meta").read_text()
        ok, exact_cleanup = closer_native_lease.release_with_receipt(Path(handoff), "closer", token)
        assert ok, "fixture cleanup refused exact owned token; preserve fixture UNKNOWN"
    receipt_path.write_text(
        json.dumps({"terminated_test_workers": terminated, "cleanup": exact_cleanup})
    )


def test_late_native_production_act_enters_actual_inner_queue(capacity_host, tmp_path, monkeypatch):
    manifest, path, obj, doc, source, clock, _invocation = capacity_host
    monkeypatch.setenv("ORCH_FEEDBACK_DB", str(tmp_path / "empty-routing.sqlite"))
    seams = tmp_path / "external-seams"
    seams.mkdir()
    (seams / "transcript.py").write_text(TRANSCRIPT)
    state = tmp_path / "synthetic-service.json"
    state.write_text(json.dumps({"labels": {}, "core": {}}))
    offset = tmp_path / "clock-offset"
    offset.write_text("0")
    log = tmp_path / "external-methods.jsonl"
    for key, value in {
        "COMPOSITION_STATE": state,
        "COMPOSITION_OFFSET": offset,
        "COMPOSITION_LOG": log,
        "COMPOSITION_ARM": tmp_path / "late-publication-armed",
    }.items():
        monkeypatch.setenv(key, str(value))
    monkeypatch.setenv(
        "COMPOSITION_SCOPED_ROOT",
        str(doc["worker_config"]["dispatch_state"]) + ".pipeline.json.scoped",
    )
    monkeypatch.setenv("GH_TOKEN", "isolated-fixture-token")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    (seams / "sitecustomize.py").write_text(
        """import io,json,os,socket,time,urllib.request,sys\nfrom pathlib import Path\nreal=time.time\ndef clock():\n return real()+float(Path(os.environ['COMPOSITION_OFFSET']).read_text())\ntime.time=clock\ndef blocked(*a,**k):raise AssertionError('fixture network is prohibited')\nsocket.socket.connect=blocked\nfrom transcript import response,event\nclass Reply(io.BytesIO):\n def __init__(self,value):super().__init__(json.dumps(value).encode())\ndef opener(request,timeout=None):\n from urllib.parse import urlsplit\n parsed=urlsplit(request.full_url)\n if parsed.scheme!='https' or parsed.netloc!='api.github.com':raise AssertionError('unexpected Core origin')\n method=request.get_method();body=json.loads(request.data) if request.data else None\n event('core-http',method,parsed.path,body)\n return Reply(response(method,parsed.path+('?' + parsed.query if parsed.query else ''),body))\nurllib.request.urlopen=opener\ndef trace(frame,kind,arg):\n if kind=='call' and frame.f_code.co_name=='publish_admission' and Path(frame.f_code.co_filename).name=='closer_native_observer.py' and Path(os.environ['COMPOSITION_ARM']).exists():\n  offset=Path(os.environ['COMPOSITION_OFFSET'])\n  if float(offset.read_text())==0:offset.write_text('1021')\n return None\nsys.settrace(trace)\n"""
    )
    gh = seams / "gh"
    gh.write_text(
        "#!"
        + sys.executable
        + "\n"
        + """import json,sys\nfrom transcript import response,event\na=sys.argv[1:];method=a[a.index('--method')+1] if '--method' in a else 'GET'\nfields={}\nfor i,x in enumerate(a):\n if x=='-f':key,value=a[i+1].split('=',1);fields[key]=value\noptions={'api','--include','-i','--method',method}\nendpoint=next(x for x in a if x not in options and not x.startswith('-') and not x.startswith(('query=','labels[]=')))\npath='/' + endpoint.lstrip('/')\nevent('gh-executable',method,path,fields)\nvalue=response(method,path,fields)\nprint(('HTTP/2 200\\nx-ratelimit-limit: 5000\\nx-ratelimit-remaining: 4999\\nx-ratelimit-reset: 9999999999\\n\\n' if '--include' in a or '-i' in a else '')+json.dumps([value] if '--slurp' in a else value))\n"""
    )
    gh.chmod(0o700)
    monkeypatch.setenv("PATH", str(seams) + ":" + os.environ["PATH"])
    monkeypatch.setenv("PYTHONPATH", str(seams) + ":" + str(Path(campaign.__file__).parent))
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")

    # Parent uses the same synthetic clock as all helper/CLI subprocesses.
    def now():
        return REAL_TIME() + float(offset.read_text())

    monkeypatch.setattr(time, "time", now)
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
    manifest["mechanism_contract"]["capacity_publication"].update(
        schema="native-scoped-capacity/v2",
        execution_policy=policy.POLICY,
        request_timeout_seconds=60,
        freshness_seconds=900,
    )
    manifest["mechanism_contract"]["capacity_publication"].pop("admission_seconds")
    campaign.write_manifest(path, manifest)
    root = native.contract(manifest)[3]
    # Fresh private fixture only: discard fixture's unexecuted publication marker
    # before exercising actual owned no-effect qualification for this exact pin.
    (root / "native-active.json").unlink()
    native._CURRENT_ENTRY.pop(str(root), None)
    native._CURRENT_ADMISSION.pop(str(root), None)
    qualified = native.supervise_native(manifest, path, budget_seconds=45)
    assert qualified["passed"], qualified
    assert campaign.qualify(manifest, path.parent) == []
    targets = ["offline/repo1#1", "offline/repo2#2"]
    source.write_text(json.dumps({"targets": [{"target": t} for t in targets]}))
    # Obtain real normalized observations through the owned gh executable.
    import closer_dispatcher

    backend = closer_dispatcher.Closer(
        closer_dispatcher.GitHub(deadline=now() + 60), state_path=tmp_path / "observation.json"
    )
    observed = {target: backend.observe(target) for target in targets}
    assert all(
        row["action"] == "post_merge_verify" and row["eligible"] for row in observed.values()
    ), observed
    frontier = policy.make_frontier_from_observations(
        observed, owner=doc["owner"], frontier_id="positive-late-native"
    )
    doc["worker_config"].update(
        finite_frontier=frontier, request_timeout_seconds=60, execution_policy=policy.POLICY
    )
    # Actual token-safe rendered helpers and same-byte physical dispatcher pin.
    mirror = Path.home() / ".codex/orchestrator-mirror"
    mirror.mkdir(parents=True)
    dispatcher = mirror / "closer_dispatcher.py"
    dispatcher.write_bytes(Path(campaign.__file__).with_name("closer_dispatcher.py").read_bytes())
    handoff = seams / "handoff"
    handoff.write_text("#!/bin/sh\nexit 0\n")
    handoff.chmod(0o700)
    monkeypatch.setenv("HANDOFF_BIN", str(handoff))
    monkeypatch.setenv("FAKE_HANDOFF", str(handoff))
    monkeypatch.setenv("FAKE_RELAY", str(seams / "must-not-run"))
    monkeypatch.setenv("EVENTS", str(tmp_path / "helper-events"))
    pre = tmp_path / "real-pre"
    post = tmp_path / "real-post"
    original = _minimal_prerun() + """if false; then
      "$HANDOFF_BIN" read-resume "$lane"
      exit 0
fi
"$HANDOFF_BIN" pickup >/dev/null
"""
    pre.write_bytes(renderer.patch_prerun(original.encode(), bounded_round=True))
    pre.chmod(0o700)
    post.write_bytes(renderer.patch_postrun(canonical_postrun().encode(), bounded_round=True))
    post.chmod(0o700)
    hooks = {
        phase: {"path": str(p), "sha256": binding._hash(p)}
        for phase, p in [("prerun", pre), ("postrun", post)]
    }
    lease = Path(manifest["module_dir"]) / "closer_native_lease.py"
    hooks.update(
        lease_module={"path": str(lease), "sha256": binding._hash(lease)}, bounded_no_new_relay=True
    )
    hooks["postrun_dependencies"] = {
        name: {"path": str(p), "sha256": binding._hash(p)}
        for name, p in {
            "interpreter": Path(sys.executable).resolve(),
            "dispatcher": dispatcher,
            "assignment": source,
        }.items()
    }
    doc["worker_config"].update(
        native_hooks=hooks, handoff_agent="codex", handoff_dir=os.environ["HANDOFF_DIR"]
    )
    doc["worker_config"]["dispatch_workers"] = 1
    obj.update(
        lambda current: current.update(
            worker_config=doc["worker_config"],
            assignment_targets=targets,
            release_pin=manifest["package_digest"],
        )
    )
    # Execute the actual normal wrapper. It records the submitted→terminal entry,
    # signs the worker handoff, and launches the real campaign worker command.
    # The time seam delays only first publication after the real entry recorded
    # its original start. It neither patches callbacks nor extends a report.
    (tmp_path / "late-publication-armed").touch()
    try:
        done = subprocess.run(
            ["/bin/bash", str(native.contract(manifest)[1]), "closer"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        (tmp_path / "normal-wrapper-output.json").write_text(
            json.dumps({"exit": done.returncode, "stdout": done.stdout, "stderr": done.stderr})
        )
        assert done.returncode == 0, (done.stdout, done.stderr)
        deadline = time.monotonic() + 90
        row = None
        while time.monotonic() < deadline:
            workers = obj.read().get("workers", [])
            if workers:
                row = workers[0]
                output = campaign.result_path(obj.path, row["worker_id"])
                if output.exists():
                    break
            time.sleep(0.1)
        assert row is not None and output.exists(), {"wrapper": done.stdout, "doc": obj.read()}
        result = json.loads(output.read_text())
        evidence = {
            "worker_result": result,
            "copied_modules": {
                name: binding._hash(Path(manifest["module_dir"]) / name)
                for name in manifest["modules"]
            },
            "package_digest": manifest["package_digest"],
            "clock_seam": "advance1021 at publish_admission entry after invocation start and before fresh publication/request",
            "wrapper_exit": done.returncode,
            "external": log.read_text(),
            "service": json.loads(state.read_text()),
            "qualification": qualified,
        }
        (tmp_path / "production-composition-result.json").write_text(json.dumps(evidence, indent=2))
        assert result["outcome"] == "ok", evidence
        assert set(json.loads(state.read_text())["labels"]) == {"1", "2"}, evidence
        assert not (Path(os.environ["HANDOFF_DIR"]) / ".lane-closer.lock").exists()
        detail = json.loads(result["detail"])
        completed = detail["completed_round"]
        cfg = native.result_admission_config(
            doc["worker_config"], result["native_admission"], obj.path, obj.read(), row
        )
        assert native.completed_round_proof(manifest, cfg, completed) == completed
        assert completed["accepted"] is False
        assert qualified["desktop_qualified"] is False
        assert qualified["binding"] == native.binding(manifest)
        assert float(offset.read_text()) == 1021
        original_admission = native.validate_admission(
            manifest, result["native_admission"], for_effects=True
        )
        fresh_report = json.loads(Path(original_admission["report"]["path"]).read_text())
        assert fresh_report["generated_at"] - original_admission["started_at"] >= 1021
        execution = detail["native_execution"]["phases"]
        assert (
            execution["B"]["started_at"] - detail["native_execution"]["admission_started_at"]
            >= 1021
        )
        cleanup = execution["C"]["cleanup"]
        assert cleanup["released"] is True and cleanup["exact_token_matched"] is True
        assert cleanup["expected_token_sha256"] == execution["A"]["token_sha256"]
        assert cleanup["released_lease_inode"] == execution["A"]["lease_inode"]
        assert (
            execution["C"]["started_at"] <= cleanup["released_at"] <= execution["C"]["finished_at"]
        )
        operations = []
        scoped = Path(str(doc["worker_config"]["dispatch_state"]) + ".pipeline.json.scoped")
        for inner_path in scoped.glob("targets/*/state.actions/*.json"):
            if inner_path.name.endswith(".changes.json"):
                continue  # producer change-journal sidecar, not an inner queue ledger
            inner = json.loads(inner_path.read_text())
            bucket = inner["frontiers"][frontier["frontier_id"]]
            assert bucket["policy"] == frontier
            for target, item in bucket["items"].items():
                operation = item["operation"]
                assert operation["status"] == "completed"
                assert operation["action"] == "post_merge_verify"
                assert operation["attempts"] == 1
                assert 0 < operation["deadline"] - operation["request_started_at"] <= 60
                assert operation["timeout_seconds"] <= 60
                assert operation["binding"]["head"] == "a" * 40
                assert operation["binding"]["merge"] == "c" * 40
                operations.append(operation)
        assert len(operations) == 2
        operations.sort(key=lambda value: value["request_started_at"])
        assert (
            operations[0]["request_started_at"] - detail["native_execution"]["admission_started_at"]
            >= 1021
        )
        events = [json.loads(line) for line in log.read_text().splitlines()]
        labels = [
            event
            for event in events
            if event["method"] == "POST" and event["path"].endswith("/labels")
        ]
        assert sorted(event["path"] for event in labels) == [
            "/repos/offline/repo1/issues/1/labels",
            "/repos/offline/repo2/issues/2/labels",
        ]
        service = json.loads(state.read_text())
        assert len(service["core"]) == 2
        for raw in service["core"].values():
            operation = json.loads(base64.b64decode(raw["content"]))
            assert operation["status"] == "succeeded"
            absolute = datetime.datetime.fromisoformat(
                operation["lease"]["deadline"].replace("Z", "+00:00")
            ).timestamp()
            # Core's UTC datetime clock is independent of the synthetic queue
            # time.time seam. Its actual operation lease remains finite900s.
            assert 0 < absolute - REAL_TIME() <= 900
        # Expired capacity evidence cannot authorize a new native admission, but
        # never invalidates the original signed terminal readback or cleanup.
        offset.write_text("2042")
        with pytest.raises(Exception, match="expired|operational inputs"):
            native.validate_admission(manifest, result["native_admission"], for_effects=True)
        original = output.read_bytes()
        external_before = log.read_bytes()
        assert (
            campaign.run_worker(
                str(obj.path),
                str(output),
                campaign.ROUND_UNIT,
                doc["owner"],
                1,
                holder=lambda *_: pytest.fail("existing result must stop before effects"),
            )
            == "ok"
        )
        assert output.read_bytes() == original and log.read_bytes() == external_before
        reconciled = campaign.reconcile(obj, pid_alive=lambda _: False)
        assert reconciled["targets"][campaign.ROUND_UNIT] == "done"
        assert output.read_bytes() == original
    finally:
        _fixture_worker_cleanup(
            obj, manifest, os.environ["HANDOFF_DIR"], tmp_path / "test-owned-cleanup.json"
        )
