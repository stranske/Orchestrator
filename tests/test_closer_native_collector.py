"""Production collector bindings also work outside a staged queue invocation."""

import hashlib
import io
import json
import os
import sys
import time
import zipfile
from types import SimpleNamespace

import pytest

import closer_dispatcher as dispatcher
import closer_evidence
import gh_capacity

HEAD = "a" * 40
BASE = "b" * 40


def snapshot():
    return {
        "target": "acme/repo#12",
        "kind": "pr",
        "head": HEAD,
        "base": BASE,
        "review_revision": "review-1",
        "sources": [{"target": "acme/repo#3", "body": "## Tasks\n- [ ] Ship it"}],
        "threads": [],
        "checks": None,
    }


def api_base(endpoint, **kwargs):
    if "/compare/" in endpoint:
        return {"status": "ahead", "ahead_by": 1, "files": []}
    if "/actions/runs?" in endpoint:
        return {"workflow_runs": []}
    raise AssertionError(endpoint)


def connection(nodes, next_page=False, cursor=None):
    return {
        "data": {
            "repository": {
                "pullRequest": {
                    "reviewThreads": {
                        "nodes": nodes,
                        "pageInfo": {"hasNextPage": next_page, "endCursor": cursor},
                    }
                }
            }
        }
    }


def thread(n):
    return {
        "id": f"thread-{n}",
        "isResolved": False,
        "isOutdated": False,
        "comments": {
            "nodes": [{"id": f"comment-{n}", "body": f"Finding {n}"}],
            "pageInfo": {"hasNextPage": False, "hasPreviousPage": False},
        },
    }


def test_native_collector_fetches_complete_review_pages_without_queue(tmp_path):
    calls = []

    def api(endpoint, **kwargs):
        calls.append((endpoint, kwargs))
        if endpoint == "graphql":
            assert kwargs["method"] == "POST"
            query = kwargs["fields"]["query"]
            assert query.startswith("query {") and "mutation" not in query
            return (
                connection([thread(2)])
                if 'after: "page-1"' in query
                else connection([thread(1)], True, "page-1")
            )
        return api_base(endpoint, **kwargs)

    packet = dispatcher.collect_native_evidence(
        snapshot(), SimpleNamespace(api=api), tmp_path, deadline=time.monotonic() + 10
    )
    assert closer_evidence.validate_packet(packet)
    review = packet["sections"]["review_conversations"]
    assert review["status"] == "complete"
    assert len(review["content"]) == 2
    assert sum(endpoint == "graphql" for endpoint, _ in calls) == 2
    assert all(
        kwargs.get("method", "GET") == "GET" for endpoint, kwargs in calls if endpoint != "graphql"
    )


def test_native_collector_preserves_incomplete_review_status(tmp_path):
    def api(endpoint, **kwargs):
        if endpoint == "graphql":
            return connection([thread(1)], True, None)
        return api_base(endpoint, **kwargs)

    packet = dispatcher.collect_native_evidence(
        snapshot(), SimpleNamespace(api=api), tmp_path, deadline=time.monotonic() + 10
    )
    assert closer_evidence.validate_packet(packet)
    assert packet["sections"]["review_conversations"]["status"] != "complete"


@pytest.mark.parametrize("high_fd", [False, True], ids=["normal-fd", "high-fd"])
def test_native_collector_downloads_real_capped_transport_archive(tmp_path, monkeypatch, high_fd):
    duplicated_stdout = []
    if high_fd:
        fcntl = pytest.importorskip("fcntl")
        resource = pytest.importorskip("resource")
        soft_limit, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
        if soft_limit != resource.RLIM_INFINITY and soft_limit <= 1024:
            pytest.skip("existing descriptor limit does not permit FD_SETSIZE regression")
        real_popen = dispatcher.subprocess.Popen

        def high_stdout_popen(*args, **kwargs):
            proc = real_popen(*args, **kwargs)
            if kwargs.get("stdout") == dispatcher.subprocess.PIPE:
                original = proc.stdout
                high = None
                try:
                    high = fcntl.fcntl(original.fileno(), fcntl.F_DUPFD, 1024)
                    original.close()
                    proc.stdout = os.fdopen(high, "rb")
                except BaseException:
                    if high is not None:
                        os.close(high)
                    original.close()
                    proc.kill()
                    proc.wait()
                    raise
                duplicated_stdout.append((high, proc.stdout))
            return proc

        monkeypatch.setattr(dispatcher.subprocess, "Popen", high_stdout_popen)

    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("junit.xml", '<testsuite tests="1"><testcase name="one"/></testsuite>')
    archive_bytes = archive.getvalue()
    path = tmp_path / "artifact.zip"
    path.write_bytes(archive_bytes)
    cli = tmp_path / "gh"
    cli.write_text(
        f"#!{sys.executable}\n"
        "import sys,pathlib\n"
        "assert 'api' in sys.argv and not any(a in sys.argv for a in ['PATCH','DELETE','POST','merge'])\n"
        "sys.stdout.buffer.write(b'HTTP/2.0 200 OK\\r\\nContent-Type: application/zip\\r\\n\\r\\n')\n"
        f"sys.stdout.buffer.write(pathlib.Path({str(path)!r}).read_bytes())\n"
    )
    cli.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    monkeypatch.setattr(gh_capacity, "GH_LEDGER", tmp_path / "rate.ndjson")
    monkeypatch.setattr(gh_capacity, "throttle", lambda *_a, **_k: {"action": "proceed"})

    def api(endpoint, **kwargs):
        if endpoint == "graphql":
            return connection([])
        if "/actions/runs?" in endpoint:
            return {"workflow_runs": [{"id": 7, "head_sha": HEAD}]}
        if endpoint.endswith("/runs/7"):
            return {
                "id": 7,
                "run_attempt": 1,
                "head_sha": HEAD,
                "pull_requests": [{"number": 12}],
                "event": "pull_request",
                "status": "completed",
                "conclusion": "success",
            }
        if endpoint.endswith("/runs/7/artifacts?per_page=100"):
            return {
                "artifacts": [
                    {
                        "id": 8,
                        "name": "test-output",
                        "size_in_bytes": len(archive_bytes),
                        "expired": False,
                    }
                ]
            }
        return api_base(endpoint, **kwargs)

    transport_diagnostics = []
    real_gh_run = gh_capacity.gh_run

    def traced_gh_run(args, **kwargs):
        runner = kwargs["runner"]

        def traced_runner(*runner_args, **runner_kwargs):
            try:
                result = runner(*runner_args, **runner_kwargs)
            except Exception as exc:
                transport_diagnostics.append(f"{type(exc).__name__}: {exc}")
                raise
            transport_diagnostics.append(f"returncode={result.returncode}")
            return result

        return real_gh_run(args, **{**kwargs, "runner": traced_runner})

    monkeypatch.setattr(gh_capacity, "gh_run", traced_gh_run)
    packet = dispatcher.collect_native_evidence(
        # This exercises successful owned-process/archive transport, not its
        # timeout boundary. Leave room for instrumented parallel CI scheduling;
        # the collector still receives one unchanged absolute deadline.
        snapshot(),
        SimpleNamespace(api=api),
        tmp_path / "retained",
        deadline=time.monotonic() + 30,
    )
    assert closer_evidence.validate_packet(packet)
    ci = packet["sections"]["ci_artifacts"]
    diagnostic = json.dumps(
        {"section": ci, "transport": transport_diagnostics},
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    )
    if high_fd:
        assert duplicated_stdout, "real owned artifact child did not exercise a high descriptor"
        assert all(fd >= 1024 for fd, _ in duplicated_stdout)
        assert all(pipe.closed for _, pipe in duplicated_stdout), "owned high descriptors leaked"
    assert ci["status"] == "complete", diagnostic
    assert ci["content"]["retrieval_status"] == "complete", diagnostic
    # Retrieval is complete, while required-check topology is still unknown.
    assert ci["content"]["proof_status"] == "partial", diagnostic
    artifact = ci["content"]["artifacts"][0]
    assert artifact["status"] == "complete", diagnostic
    assert artifact["id"] == 8 and artifact["run_id"] == 7, diagnostic
    assert artifact["run_attempt"] == 1 and artifact["head_sha"] == HEAD, diagnostic
    assert artifact["archive"]["sha256"] == hashlib.sha256(archive_bytes).hexdigest(), diagnostic
    junit = next(receipt for receipt in artifact["receipts"] if receipt["kind"] == "junit")
    assert (
        junit["raw"]["sha256"]
        == hashlib.sha256(b'<testsuite tests="1"><testcase name="one"/></testsuite>').hexdigest()
    ), diagnostic
    retained = list((tmp_path / "retained").rglob("*"))
    assert any(
        p.is_file() and p.read_bytes() == b'<testsuite tests="1"><testcase name="one"/></testsuite>'
        for p in retained
    )
    assert hashlib.sha256(path.read_bytes()).digest() == hashlib.sha256(archive_bytes).digest()


@pytest.mark.parametrize("failure", ["overflow", "deadline"])
def test_native_artifact_transport_failure_closes_owned_child(tmp_path, monkeypatch, failure):
    cli = tmp_path / "gh"
    cli.write_text(
        f"#!{sys.executable}\n"
        "import sys,time\n"
        + (
            "sys.stdout.buffer.write(b'x' * 4096);sys.stdout.buffer.flush()\n"
            if failure == "overflow"
            else ""
        )
        + "time.sleep(30)\n"
    )
    cli.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    monkeypatch.setattr(gh_capacity, "GH_LEDGER", tmp_path / "rate.ndjson")
    monkeypatch.setattr(gh_capacity, "throttle", lambda *_a, **_k: {"action": "proceed"})
    children = []
    real_popen = dispatcher.subprocess.Popen

    def tracked_popen(*args, **kwargs):
        proc = real_popen(*args, **kwargs)
        children.append(proc)
        return proc

    monkeypatch.setattr(dispatcher.subprocess, "Popen", tracked_popen)

    def exercise_artifact(_snapshot, _api, _retained, **kwargs):
        deadline = kwargs["deadline"]
        # Small explicit per-artifact allowance exercises actual streaming refusal,
        # not a fake collector status or an enormous fixture.
        return kwargs["artifact_fetch"](
            {"id": 8, "size_in_bytes": 1}, deadline=deadline, max_bytes=32
        )

    failures = []
    real_gh_run = gh_capacity.gh_run

    def traced_gh_run(args, **kwargs):
        runner = kwargs["runner"]

        def traced_runner(*runner_args, **runner_kwargs):
            try:
                return runner(*runner_args, **runner_kwargs)
            except Exception as error:
                failures.append(error)
                raise

        return real_gh_run(args, **{**kwargs, "runner": traced_runner})

    monkeypatch.setattr(gh_capacity, "gh_run", traced_gh_run)
    monkeypatch.setattr(closer_evidence, "collect_evidence", exercise_artifact)
    expected = ValueError if failure == "overflow" else TimeoutError
    message = "byte cap" if failure == "overflow" else "deadline"
    with pytest.raises(RuntimeError, match="artifact archive download failed"):
        dispatcher.collect_native_evidence(
            snapshot(),
            SimpleNamespace(api=api_base),
            tmp_path / "retained",
            deadline=time.monotonic() + (30 if failure == "overflow" else 1.5),
        )
    assert len(failures) == 1
    assert isinstance(failures[0], expected) and message in str(failures[0])
    assert len(children) == 1
    child = children[0]
    assert child.poll() is not None, "owned child must be reaped after transport failure"
    assert child.stdout.closed, "owned stdout must close on transport failure"
    with pytest.raises(ProcessLookupError):
        os.killpg(child.pid, 0)


@pytest.mark.parametrize("target", [None, "acme/repo", "foreign", 12])
def test_native_collector_refuses_bad_transport_identity_before_reads(tmp_path, target):
    gh = SimpleNamespace(api=lambda *_a, **_k: pytest.fail("invalid identity reached transport"))
    with pytest.raises(ValueError, match="exact target"):
        dispatcher.collect_native_evidence(
            {**snapshot(), "target": target}, gh, tmp_path, deadline=time.monotonic() + 10
        )
    assert not list(tmp_path.iterdir())
