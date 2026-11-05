"""Actual local cwd/file controls plus synthetic frame DATA; no provider/authority."""

import ast
import inspect
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_closer_coding_publication import TEST_POLICY

import closer_codex_owned_stdio as owned
import closer_coding_publication as pub
import closer_coding_worker as worker
import dispatcher


def isolation(baseline):
    tree = ast.parse(inspect.getsource(worker._execute))
    call = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "offload"
    )
    value = next(k.value for k in call.keywords if k.arg == "isolate")
    return eval(
        compile(ast.Expression(value), "<actual-call-option>", "eval"),
        {"publication_baseline_ref": baseline},
    )


def test_selected_publication_uses_owned_workspace_ordinary_keeps_isolation():
    assert isolation(object()) is False
    assert isolation(None) is True


def test_owned_route_validates_workspace_before_creating_transport_files():
    tree = ast.parse(inspect.getsource(owned.prepare))
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
    check = next(
        n
        for n in calls
        if isinstance(n.func, ast.Attribute) and n.func.attr == "validate_coding_workspace"
    )
    mkdir = next(n for n in calls if isinstance(n.func, ast.Attribute) and n.func.attr == "mkdir")
    assert check.lineno < mkdir.lineno
    # Existing actual run-frame cwd and frozen physical-directory validation stay present.
    assert 'frame.f_locals.get("cwd") == route.cwd' in inspect.getsource(owned.route_for_run)
    assert "_physical(value[0], directory=True) == value" in inspect.getsource(owned.verify_route)


@pytest.mark.parametrize("old_isolation", [True, False])
def test_real_local_child_file_reaches_only_its_actual_cwd_and_candidate(
    tmp_path, monkeypatch, old_isolation
):
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    (workspace / "src").mkdir()
    (workspace / "src/a.py").write_bytes(b"partial\n")
    git = "/usr/bin/git"
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null", HOME=str(tmp_path))

    def run(*args):
        return subprocess.check_output(
            [git, "-C", str(workspace), *args], stderr=subprocess.DEVNULL, env=env, timeout=10
        )

    # Repository-location variables from an outer Git hook must never redirect
    # setup into that repository. Poison only this local setup scope.
    with monkeypatch.context() as inherited:
        inherited.setenv("GIT_DIR", str(tmp_path / "not-the-owned-git-dir"))
        inherited.setenv("GIT_WORK_TREE", str(tmp_path / "not-the-owned-worktree"))
        inherited.setenv("GIT_INDEX_FILE", str(tmp_path / "not-the-owned-index"))
        run("init", "--initial-branch=correction/owned")
        run("add", "src/a.py")
        run(
            "-c",
            "user.name=Inert test",
            "-c",
            "user.email=inert@example.invalid",
            "commit",
            "-m",
            "partial",
        )
    head = run("rev-parse", "HEAD").decode().strip()
    commands = {
        "head": ["rev-parse", "HEAD"],
        "branch": ["symbolic-ref", "HEAD"],
        "commit": ["cat-file", "commit", head],
        "tree": ["ls-tree", "-r", "-z", "--full-tree", head],
        "index": ["ls-files", "--stage", "-z"],
        "index_flags": ["ls-files", "-v", "-z"],
    }
    host = SimpleNamespace(
        workspace=workspace,
        observed=SimpleNamespace(policy=dict(TEST_POLICY)),
        pr={"head_oid": head, "base_oid": head, "head_ref": "correction/owned"},
        check=lambda: None,
        read=lambda kind: run(*commands[kind]),
    )
    baseline = pub._capture_baseline(host)
    monkeypatch.setattr(dispatcher, "OFFLOAD_DIR", tmp_path / "offloads")
    # Exact dispatcher copy helper, then a real local Python child; no model command.
    cwd = dispatcher._isolate_offload_cwd(workspace) if old_isolation else workspace
    child = subprocess.check_output(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; Path('src/a.py').write_bytes(b'fixed\\n'); print(Path.cwd())",
        ],
        cwd=cwd,
        env=env,
        timeout=10,
    )
    assert Path(child.decode().strip()) == cwd.resolve()
    if old_isolation:
        assert (workspace / "src/a.py").read_bytes() == b"partial\n"
        assert (cwd / "src/a.py").read_bytes() == b"fixed\n"
        with pytest.raises(pub.PublicationHeld):
            pub._capture_candidate(host, baseline)
    else:
        assert cwd == workspace
        candidate = pub._capture_candidate(host, baseline)
        assert [row["path"] for row in candidate["files"]] == ["src/a.py"]
        assert candidate["files"][0]["after"]["git_oid"] == pub.blob_oid(b"fixed\n")


def frame_data(workspace):
    root, controller, operation, custody = object(), object(), object(), object()
    caller = SimpleNamespace(f_code=worker._execute.__code__, f_globals=worker.__dict__)
    observed = SimpleNamespace(
        caller=caller, root=root, controller=controller, operation=operation, custody=custody
    )
    baseline = object.__new__(pub._BaselineLease)
    baseline.observed = observed
    baseline.host = SimpleNamespace(observed=observed, workspace=workspace)
    baseline.closed = False
    caller.f_locals = {
        "publication_baseline_ref": baseline,
        "publication_policy": {},
        "root": root,
        "controller": controller,
        "operation": operation,
        "guard": SimpleNamespace(custody=custody),
        "workspace": str(workspace),
    }
    closure = dict(zip(dispatcher.offload.__code__.co_freevars, dispatcher.offload.__closure__))
    signature = closure["signature"].cell_contents
    args, kwargs = ("codex", "synthetic"), {
        "cwd": str(workspace),
        "isolate": False,
        "data_only": False,
    }
    bound = signature.bind(*args, **kwargs)
    wrapper = SimpleNamespace(
        f_back=caller,
        f_code=dispatcher.offload.__code__,
        f_globals=dispatcher.__dict__,
        f_locals={
            "function": dispatcher.offload.__wrapped__,
            "signature": signature,
            "bound": bound,
            "values": bound.arguments,
            "args": args,
            "kwargs": kwargs,
        },
    )
    origin = SimpleNamespace(
        f_back=wrapper,
        f_code=dispatcher._CODING_DELIVERY_OFFLOAD_CODE,
        f_globals=dispatcher.__dict__,
        f_locals={"agent": "codex", "cwd": str(workspace)},
    )
    return origin, caller, baseline


@pytest.mark.parametrize(
    "change",
    [
        "copy",
        "relocated",
        "origin_code",
        "origin_globals",
        "worker_code",
        "worker_globals",
        "lease_type",
        "missing_lease",
        "root",
        "operation",
        "custody",
        "ended",
        "host_observation",
        "wrapper_code",
        "wrapper_globals",
        "wrapped_function",
        "signature",
        "values",
        "kwargs",
        "data_only",
    ],
)
def test_selected_workspace_and_source_identity_refusals(tmp_path, monkeypatch, change):
    origin, caller, baseline = frame_data(tmp_path)
    checked = []
    # Synthetic leaf DATA only: full source issuance is not claimed by these controls.
    monkeypatch.setattr(pub._BaselineLease, "check", lambda self: checked.append(self))
    cwd = str(tmp_path)
    wrapper = origin.f_back
    if change == "wrapper_code":
        wrapper.f_code = test_selected_workspace_and_source_identity_refusals.__code__
    elif change == "wrapper_globals":
        wrapper.f_globals = {}
    elif change == "wrapped_function":
        wrapper.f_locals["function"] = lambda: None
    elif change == "signature":
        wrapper.f_locals["signature"] = inspect.signature(lambda: None)
    elif change == "values":
        wrapper.f_locals["values"] = dict(wrapper.f_locals["values"])
    elif change == "kwargs":
        wrapper.f_locals["kwargs"] = dict(wrapper.f_locals["kwargs"], cwd="/wrong")
    elif change == "data_only":
        wrapper.f_locals["values"]["data_only"] = True
    if change in {"copy", "relocated"}:
        cwd = str(tmp_path / change)
    elif change == "origin_code":
        origin.f_code = test_selected_workspace_and_source_identity_refusals.__code__
    elif change == "origin_globals":
        origin.f_globals = {}
    elif change == "worker_code":
        caller.f_code = test_selected_workspace_and_source_identity_refusals.__code__
    elif change == "worker_globals":
        caller.f_globals = {}
    elif change == "lease_type":
        caller.f_locals["publication_baseline_ref"] = {}
    elif change == "missing_lease":
        caller.f_locals["publication_baseline_ref"] = None
    elif change == "root":
        caller.f_locals["root"] = object()
    elif change == "operation":
        caller.f_locals["operation"] = object()
    elif change == "custody":
        caller.f_locals["guard"].custody = object()
    elif change == "ended":
        baseline.observed.caller = object()
    elif change == "host_observation":
        baseline.host.observed = object()
    with pytest.raises(pub.PublicationHeld):
        pub.validate_coding_workspace(origin, cwd)


def test_same_authenticated_workspace_and_ordinary_mode_data(tmp_path, monkeypatch):
    origin, caller, baseline = frame_data(tmp_path)
    checked = []
    monkeypatch.setattr(pub._BaselineLease, "check", lambda self: checked.append(self))
    assert pub.validate_coding_workspace(origin, str(tmp_path)) is None
    assert checked == [baseline]
    caller.f_locals.update(publication_baseline_ref=None, publication_policy=None)
    assert pub.validate_coding_workspace(origin, str(tmp_path / "ordinary")) is None
    assert checked == [baseline]


def test_unissued_lease_cannot_bypass_real_baseline_check(tmp_path):
    origin, caller, baseline = frame_data(tmp_path)
    baseline.closed = True
    with pytest.raises(pub.PublicationHeld, match="lease closed"):
        pub.validate_coding_workspace(origin, str(tmp_path))


def test_real_decorated_call_has_exact_wrapper_and_bound_argument_chain(monkeypatch, tmp_path):
    import closer_correction_maintenance as maintenance

    monkeypatch.setattr(maintenance, "assert_offload_allowed", lambda: None)
    captured = []

    class StopBeforeBody(Exception):
        pass

    def observe(frame, event, arg):
        if event == "call" and frame.f_code is dispatcher._CODING_DELIVERY_OFFLOAD_CODE:
            wrapper = frame.f_back
            captured.append(
                (
                    wrapper.f_code is dispatcher.offload.__code__,
                    wrapper.f_globals is dispatcher.__dict__,
                    wrapper.f_locals["function"] is dispatcher.offload.__wrapped__,
                    wrapper.f_locals["bound"].arguments is wrapper.f_locals["values"],
                    dict(wrapper.f_locals["values"]),
                )
            )
            raise StopBeforeBody

    previous = sys.getprofile()
    try:
        sys.setprofile(observe)
        with pytest.raises(StopBeforeBody):
            dispatcher.offload(
                "codex", "synthetic", cwd=str(tmp_path), isolate=False, data_only=False
            )
    finally:
        sys.setprofile(previous)
    assert len(captured) == 1 and captured[0][:4] == (True, True, True, True)
    assert captured[0][4]["cwd"] == str(tmp_path)
    assert captured[0][4]["isolate"] is False
