"""Real default Core transport in an owned child; external HTTP seam, no network."""

import datetime
import importlib.util
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
from pathlib import Path

import pytest
import test_closer_runtime_binding as binding_tests

import closer_resources as resources
import closer_runtime_binding as rb
import dispatcher


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    return binding_tests.bundle.__wrapped__(tmp_path, monkeypatch)


@pytest.fixture
def http_seam(tmp_path, monkeypatch):
    original_run = dispatcher.subprocess.run

    def fixture_birth(command, **kwargs):
        if command[:1] == ["ps"]:
            return subprocess.CompletedProcess(command, 0, "offline-fixture-birth", "")
        return original_run(command, **kwargs)

    # Sandbox denies ps; substitute ONLY the offline birth-read observation.
    # Production still uses the exact supervisor and real kill/reap controls.
    monkeypatch.setattr(dispatcher.subprocess, "run", fixture_birth)
    seam = tmp_path / "http-seam"
    seam.mkdir()
    audit = tmp_path / "http-attempts.jsonl"
    (seam / "sitecustomize.py").write_text("""
import io,json,os,time,urllib.request,urllib.error
from http.client import HTTPMessage
from pathlib import Path
original_read_bytes=Path.read_bytes
def delayed_loader_read(path):
    if str(path)==os.environ.get("CORE_HTTP_SLOW_LOADER"):
        time.sleep(2)
    return original_read_bytes(path)
Path.read_bytes=delayed_loader_read
time.sleep(float(os.environ.get("CORE_HTTP_BOOT_DELAY","0")))

def opener(request, *, timeout):
    with open(os.environ["CORE_HTTP_AUDIT"],"a") as stream:
        stream.write(json.dumps({"method":request.method,"path":request.full_url.split("api.github.com")[-1],"timeout":timeout,"pid":os.getpid()})+"\\n")
    time.sleep(float(os.environ.get("CORE_HTTP_DELAY","0")))
    status=int(os.environ.get("CORE_HTTP_STATUS","200"))
    if status != 200:
        headers=HTTPMessage()
        for key,value in json.loads(os.environ.get("CORE_HTTP_HEADERS",'{"Retry-After":"7","X-RateLimit-Remaining":"0"}')).items():
            headers[key]=value
        raise urllib.error.HTTPError(request.full_url,status,os.environ.get("CORE_HTTP_REASON","fixture"),headers,io.BytesIO(b"secret error body"))
    return io.BytesIO(b'{"default_branch":"main"}')
urllib.request.urlopen=opener
""")
    monkeypatch.setenv("PYTHONPATH", str(seam) + os.pathsep + os.environ.get("PYTHONPATH", ""))
    monkeypatch.setenv("CORE_HTTP_AUDIT", str(audit))
    monkeypatch.setenv("GH_TOKEN", "fixture-token-must-not-appear-in-output")
    return audit


def test_default_core_transport_composes_once_in_child(bundle, http_seam):
    transport = rb._CoreHTTPTransport(bundle)
    assert transport("GET", "/repos/owner/repo", None) == {"default_branch": "main"}
    rows = [json.loads(line) for line in http_seam.read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]["pid"] != os.getpid()
    assert rows[0]["timeout"] == 30


@pytest.mark.parametrize("status", [401, 403, 404, 409, 412, 422, 429])
def test_http_status_headers_retained_without_body(bundle, http_seam, monkeypatch, status):
    monkeypatch.setenv("CORE_HTTP_STATUS", str(status))
    with pytest.raises(urllib.error.HTTPError) as caught:
        rb._CoreHTTPTransport(bundle)("GET", "/repos/owner/repo", None)
    assert caught.value.code == status
    assert caught.value.headers["Retry-After"] == "7"
    assert caught.value.headers["X-RateLimit-Remaining"] == "0"
    assert caught.value.closed
    assert "secret" not in str(caught.value)
    assert len(http_seam.read_text().splitlines()) == 1


@pytest.mark.parametrize("bootstrap_delay", [0, 2])
def test_hung_response_killed_once_by_original_request_window(
    bundle, http_seam, monkeypatch, bootstrap_delay
):
    monkeypatch.setenv("CORE_HTTP_BOOT_DELAY", str(bootstrap_delay))
    monkeypatch.setenv("CORE_HTTP_DELAY", "5")
    spawned = []
    original_popen = dispatcher.subprocess.Popen

    def record_spawn(*args, **kwargs):
        child = original_popen(*args, **kwargs)
        spawned.append(child.pid)
        return child

    monkeypatch.setattr(dispatcher.subprocess, "Popen", record_spawn)
    started = time.monotonic()
    with resources.inventory_request(time.time() + 1.2):
        with pytest.raises(subprocess.TimeoutExpired):
            rb._CoreHTTPTransport(bundle)("GET", "/repos/owner/repo", None)
    assert time.monotonic() - started < 3.5
    # The original window also covers interpreter/Core bootstrap. A child killed
    # there has made zero HTTP attempts; it must still be reaped without retry.
    rows = (
        [json.loads(line) for line in http_seam.read_text().splitlines()]
        if http_seam.exists()
        else []
    )
    assert len(spawned) == 1
    assert len(rows) <= 1
    if bootstrap_delay:
        assert rows == []
    if rows:
        assert rows[0]["pid"] == spawned[0]
    with pytest.raises(ProcessLookupError):
        os.kill(spawned[0], 0)


def test_expired_request_refuses_before_spawn(bundle, http_seam):
    with resources.inventory_request(time.time() - 1):
        with pytest.raises(TimeoutError):
            rb._CoreHTTPTransport(bundle)("GET", "/repos/owner/repo", None)
    assert not http_seam.exists()


def test_core_allowlist_refuses_before_spawn(bundle, http_seam):
    with pytest.raises(bundle.runtime.PolicyDenied):
        rb._CoreHTTPTransport(bundle)("PUT", "/repos/owner/repo/pulls/1/merge", {})
    assert not http_seam.exists()


@pytest.mark.parametrize("raw", ["not-json", "[]", '{"schema":"wrong","status":"ok"}', "x" * 1025])
def test_malformed_or_oversize_protocol_is_unknown(bundle, http_seam, monkeypatch, raw):
    monkeypatch.setattr(rb, "_CORE_HTTP_PROTOCOL_BYTES", 1024)
    monkeypatch.setattr(
        dispatcher,
        "run_observed_offload",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 0, raw, ""),
    )
    with pytest.raises(rb.BindingError):
        rb._CoreHTTPTransport(bundle)("GET", "/repos/owner/repo", None)
    assert not http_seam.exists()


def test_original_window_and_credentials_not_in_request_protocol(bundle, http_seam, monkeypatch):
    seen = []

    def run(command, **kwargs):
        packet = json.load(kwargs["stdin"])
        assert "fixture-token" not in json.dumps(packet)
        assert "fixture-token" not in repr(command)
        assert os.fstat(kwargs["stdin"].fileno()).st_mode & 0o777 == 0o600
        seen.append(kwargs["timeout"])
        return subprocess.CompletedProcess(
            command, 0, '{"schema":"core-http-result/v1","status":"ok","value":{}}', ""
        )

    monkeypatch.setattr(dispatcher, "run_observed_offload", run)
    with resources.inventory_request(time.time() + 0.7):
        time.sleep(0.03)
        assert rb._CoreHTTPTransport(bundle)("GET", "/repos/owner/repo", None) == {}
    assert 0 < seen[0] < 0.68


def test_birth_denial_preserves_supervisor_failure(bundle, http_seam, monkeypatch):
    def deny(*args, **kwargs):
        raise PermissionError("offline explicit birth-read denial")

    monkeypatch.setattr(dispatcher.subprocess, "run", deny)
    with pytest.raises(dispatcher.ObservedOffloadFailure, match="PermissionError") as caught:
        rb._CoreHTTPTransport(bundle)("GET", "/repos/owner/repo", None)
    assert caught.value.observation["cleanup_terminal"] is True


def test_trusted_transport_injection_unchanged(bundle):
    calls = []

    def injected(*args):
        return calls.append(args)

    binding = rb.RuntimeBinding(bundle, {}, transport=injected)
    adapter = binding._adapter("owner/repo")
    assert adapter.store._request is injected
    assert calls == []


@pytest.mark.parametrize("hang", [False, True])
def test_finite_pool_observes_exact_request_and_resolves_only_reaped_group(
    bundle, http_seam, monkeypatch, tmp_path, hang
):
    root = tmp_path / "pool-root"
    if hang:
        monkeypatch.setenv("CORE_HTTP_DELAY", "5")
        monkeypatch.setenv("CORE_HTTP_SLOW_LOADER", str(Path(rb.__file__).resolve()))
    with resources.pool_slot(
        root,
        "io",
        1,
        deadline=time.time() + 2,
        finite_frontier=True,
        reservation={
            "reservation_id": "owned-http",
            "target": "owner/repo#1",
            "frontier_id": "frozen-http",
            "agent": "closer",
            "profile_id": "io",
        },
    ):
        with resources.inventory_request(time.time() + 1.2):
            if hang:
                with pytest.raises(subprocess.TimeoutExpired):
                    rb._CoreHTTPTransport(bundle)("GET", "/repos/owner/repo", None)
            else:
                assert (
                    rb._CoreHTTPTransport(bundle)("GET", "/repos/owner/repo", None)[
                        "default_branch"
                    ]
                    == "main"
                )
        row = json.loads((root / "pools/io/slot-0.json").read_text())
        assert row["status"] == "resolved"
        assert row["resolution"] == "process_group_absent"
        assert row["frontier_id"] == "frozen-http"
        assert row["target"] == "owner/repo#1"
        assert row["run_id"].startswith("core-http-")
        # This resolves local request-process capacity ONLY; no assertion of a
        # remote effect or Core/queue operation completion is inferred.
    if hang:
        assert not http_seam.exists()  # the original deadline expires in loader bootstrap
        with pytest.raises(ProcessLookupError):
            os.killpg(row["process_group"], 0)
    else:
        assert len(http_seam.read_text().splitlines()) == 1


def test_loaded_bootstrap_replacement_refused_before_request(bundle, http_seam, tmp_path):
    path = tmp_path / "loaded_binding.py"
    path.write_bytes(Path(rb.__file__).read_bytes())
    spec = importlib.util.spec_from_file_location("review_owned_binding", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # Construct AFTER replacement too: neither constructor nor request may bless it.
    path.write_text("class CoreBundle: pass\n")
    transport = module._CoreHTTPTransport(bundle)
    with pytest.raises(module.BindingError, match="binding source changed"):
        transport("GET", "/repos/owner/repo", None)
    assert not http_seam.exists()


def test_child_rechecks_bootstrap_after_parent_check(bundle, http_seam, tmp_path, monkeypatch):
    path = tmp_path / "race_binding.py"
    path.write_bytes(Path(rb.__file__).read_bytes())
    spec = importlib.util.spec_from_file_location("review_race_binding", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    real_runner = dispatcher.run_observed_offload

    def replace_before_spawn(*args, **kwargs):
        path.write_text("class CoreBundle: pass\n")
        return real_runner(*args, **kwargs)

    monkeypatch.setattr(dispatcher, "run_observed_offload", replace_before_spawn)
    with pytest.raises(module.BindingError, match="outcome unknown"):
        module._CoreHTTPTransport(bundle)("GET", "/repos/owner/repo", None)
    assert not http_seam.exists()


@pytest.mark.parametrize(
    "status,reason,headers,limited",
    [
        (403, "API rate limit exceeded", {}, True),
        (403, "forbidden secret reason", {}, False),
        (403, "quota exhausted", {}, False),
        (403, "forbidden", {"X-RateLimit-Remaining": "0"}, True),
        (403, "forbidden", {"Retry-After": "7"}, True),
        (429, "busy", {}, True),
        (401, "API rate limit exceeded", {}, False),
    ],
)
def test_exact_core_rate_classification_without_raw_reason(
    bundle, http_seam, monkeypatch, status, reason, headers, limited
):
    monkeypatch.setenv("CORE_HTTP_STATUS", str(status))
    monkeypatch.setenv("CORE_HTTP_REASON", reason)
    monkeypatch.setenv("CORE_HTTP_HEADERS", json.dumps(headers))
    baseline = urllib.error.HTTPError("/repos/owner/repo", status, reason, headers, None)
    assert bundle.github._rate_limited(baseline) is limited
    baseline.close()
    with pytest.raises(urllib.error.HTTPError) as caught:
        rb._CoreHTTPTransport(bundle)("GET", "/repos/owner/repo", None)
    assert bundle.github._rate_limited(caught.value) is limited
    assert caught.value.code == status
    assert caught.value.closed
    assert reason not in str(caught.value)
    assert len(http_seam.read_text().splitlines()) == 1


@pytest.mark.parametrize("limited", [None, 1, "true", []])
def test_rate_classification_requires_strict_boolean(bundle, http_seam, monkeypatch, limited):
    raw = json.dumps(
        {
            "schema": "core-http-result/v1",
            "status": "http_error",
            "code": 403,
            "headers": {},
            "rate_limited": limited,
        }
    )
    monkeypatch.setattr(
        dispatcher,
        "run_observed_offload",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 0, raw, ""),
    )
    with pytest.raises(rb.BindingError, match="error protocol invalid"):
        rb._CoreHTTPTransport(bundle)("GET", "/repos/owner/repo", None)
    assert not http_seam.exists()


@pytest.mark.parametrize("method", ["GET", "PUT"])
@pytest.mark.parametrize("slow_loader", [False, True])
def test_http_child_self_deadline_without_parent_watchdog(
    bundle, http_seam, monkeypatch, method, slow_loader, record_property
):
    if slow_loader:
        monkeypatch.setenv("CORE_HTTP_SLOW_LOADER", str(Path(rb.__file__).resolve()))
    monkeypatch.setenv("CORE_HTTP_DELAY", "2")
    deadline = time.monotonic() + 0.8
    path = (
        "/repos/owner/repo"
        if method == "GET"
        else "/repos/owner/repo/contents/.github/closer-operation-portable/v1/" + "a" * 64 + ".json"
    )
    packet = {
        "loader_path": str(Path(rb.__file__).resolve()),
        "loader_sha256": rb._BINDING_SOURCE_SHA256,
        "pin": {
            "package_root": str(bundle.root),
            "package_digest": bundle.namespace.removeprefix("_closer_core_"),
        },
        "method": method,
        "path": path,
        "body": (
            None
            if method == "GET"
            else {"branch": "keepalive-authority-state", "content": "fixture"}
        ),
        "max_bytes": rb._CORE_HTTP_PROTOCOL_BYTES,
        "request_deadline_mono": deadline,
    }
    started = time.monotonic()
    # No production supervisor here. Harness3s is only a test runaway guard;
    # the child must independently stop at the captured0.8s absolute deadline.
    with subprocess.Popen(
        [sys.executable, "-c", rb._CORE_HTTP_CHILD, str(deadline)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as child:
        try:
            child.communicate(json.dumps(packet), timeout=3)
        except subprocess.TimeoutExpired:
            child.kill()
            child.communicate()
            raise
    assert child.returncode == -signal.SIGALRM
    assert time.monotonic() - started < 1.5
    # Child SIGALRM covers loader/Core bootstrap as well as response reads.
    # The HTTP audit cannot identify a child that died before its first request.
    rows = (
        [json.loads(line) for line in http_seam.read_text().splitlines()]
        if http_seam.exists()
        else []
    )
    record_property("http_attempt_count", len(rows))
    record_property("reaped_child_pid", child.pid)
    assert len(rows) <= 1
    if slow_loader:
        assert rows == []
    if rows:
        assert rows[0]["method"] == method and rows[0]["pid"] == child.pid
    with pytest.raises(ProcessLookupError):
        os.kill(child.pid, 0)


@pytest.mark.parametrize("deadline", ["expired", "nan", "inf", "long"])
def test_http_child_refuses_invalid_or_expired_deadline_without_http(bundle, http_seam, deadline):
    value = (
        time.monotonic() - 1
        if deadline == "expired"
        else time.monotonic() + 1000 if deadline == "long" else float(deadline)
    )
    done = subprocess.run(
        [sys.executable, "-c", rb._CORE_HTTP_CHILD, str(value)],
        input="{}",
        capture_output=True,
        text=True,
        timeout=3,
    )
    assert done.returncode == 0
    assert json.loads(done.stdout)["status"] == "unknown"
    assert not http_seam.exists()


@pytest.mark.parametrize(
    "wire_name,canonical,value",
    [
        ("retry-after", "Retry-After", "7"),
        ("x-ratelimit-remaining", "X-RateLimit-Remaining", "0"),
    ],
)
def test_error_headers_retain_case_insensitive_http_semantics(
    bundle, http_seam, monkeypatch, wire_name, canonical, value
):
    monkeypatch.setenv("CORE_HTTP_STATUS", "403")
    monkeypatch.setenv("CORE_HTTP_REASON", "fixture refusal")
    monkeypatch.setenv("CORE_HTTP_HEADERS", json.dumps({wire_name: value}))
    with pytest.raises(urllib.error.HTTPError) as caught:
        rb._CoreHTTPTransport(bundle)("GET", "/repos/owner/repo", None)
    assert caught.value.headers.get(canonical) == value
    assert caught.value.headers.get(canonical.upper()) == value
    assert caught.value.headers.get(wire_name) == value
    assert bundle.github._rate_limited(caught.value) is True
    assert caught.value.closed
    assert len(http_seam.read_text().splitlines()) == 1


@pytest.mark.parametrize("value", [None, 0, [], {}])
def test_non_string_header_values_are_rejected(bundle, http_seam, monkeypatch, value):
    raw = json.dumps(
        {
            "schema": "core-http-result/v1",
            "status": "http_error",
            "code": 403,
            "headers": {"retry-after": value},
            "rate_limited": False,
        }
    )
    monkeypatch.setattr(
        dispatcher,
        "run_observed_offload",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 0, raw, ""),
    )
    with pytest.raises(rb.BindingError, match="error protocol invalid"):
        rb._CoreHTTPTransport(bundle)("GET", "/repos/owner/repo", None)
    assert not http_seam.exists()


@pytest.mark.parametrize(
    "status,headers,category",
    [
        (401, {}, "denied"),
        (403, {}, "denied"),
        (403, {"retry-after": "7"}, "capacity"),
        (403, {"x-ratelimit-remaining": "0"}, "capacity"),
        (429, {}, "capacity"),
        (404, {}, "absent"),
        (500, {}, "unavailable"),
    ],
)
def test_actual_core_read_consumer_matrix(
    bundle, http_seam, monkeypatch, status, headers, category
):
    monkeypatch.setenv("CORE_HTTP_STATUS", str(status))
    monkeypatch.setenv("CORE_HTTP_HEADERS", json.dumps(headers))
    store = bundle.github.GitHubContentsOperationStore(rb._CoreHTTPTransport(bundle), "owner/repo")
    expected = {
        "denied": bundle.runtime.PolicyDenied,
        "capacity": bundle.github.RemoteUnavailable,
        "absent": urllib.error.HTTPError,
        "unavailable": bundle.github.RemoteUnavailable,
    }[category]
    with pytest.raises(expected) as caught:
        store._get("/repos/owner/repo")
    if category == "capacity":
        assert "rate limited" in str(caught.value)
    if category == "denied":
        assert "no fallback" in str(caught.value)
    if category == "absent":
        assert caught.value.code == 404 and caught.value.closed
    assert len(http_seam.read_text().splitlines()) == 1


@pytest.mark.parametrize(
    "status,headers,category",
    [
        (401, {}, "denied"),
        (403, {}, "denied"),
        (403, {"retry-after": "7"}, "capacity"),
        (403, {"x-ratelimit-remaining": "0"}, "capacity"),
        (429, {}, "capacity"),
        (409, {}, "cas"),
        (422, {}, "cas"),
        (412, {}, "unknown"),
    ],
)
def test_actual_core_write_consumer_matrix(
    bundle, http_seam, monkeypatch, status, headers, category
):
    monkeypatch.setenv("CORE_HTTP_STATUS", str(status))
    monkeypatch.setenv("CORE_HTTP_HEADERS", json.dumps(headers))
    core = bundle.runtime
    spec = core.OperationSpec(
        "owner/repo", 1, "verify_request", "a" * 40, None, "b" * 64, "review", {"authority": 2}
    )
    owner = core.ExecutorIdentity("orchestrator_local", "fixture-owner")
    record = core.new_operation_record(
        spec,
        owner,
        lease_deadline=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=2),
    )
    store = bundle.github.GitHubContentsOperationStore(rb._CoreHTTPTransport(bundle), "owner/repo")
    # Isolate error classification from pre-/post-write observations. Actual
    # clean-claim schema, authority/CAS validators and default HTTP remain used.
    monkeypatch.setattr(store, "_read_remote", lambda _: (None, None))
    expected = {
        "denied": core.PolicyDenied,
        "capacity": bundle.github.RemoteUnavailable,
        "cas": core.CasConflict,
        "unknown": bundle.github.UnknownOutcome,
    }[category]
    with pytest.raises(expected) as caught:
        store.write_cas(
            core.canonical_target_fence_key(spec),
            record,
            expected_revision=None,
            authority=core.WriteAuthority("claim", owner, None),
        )
    if category == "capacity":
        assert "rate limited" in str(caught.value)
    if category == "denied":
        assert "no fallback" in str(caught.value)
    if category == "unknown":
        assert caught.value.no_fallback is False
    rows = [json.loads(line) for line in http_seam.read_text().splitlines()]
    assert len(rows) == 1 and rows[0]["method"] == "PUT"
