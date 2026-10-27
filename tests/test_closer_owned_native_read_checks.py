"""Finite real method bodies; synthetic leaf authentication/I/O, no issuance/effect."""

import json
import os
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

import closer_codex_native_confinement as confinement
import closer_codex_owned_stdio as owned
import closer_coding_worker as worker
import closer_correction_controller as ctl


def world(mp):
    counts = {"root": 0, "writes": 0, "writer": 0, "live": 0, "changed": False}
    root = object.__new__(worker._Root)

    def root_check(self):
        counts["root"] += 1
        if counts["changed"]:
            raise owned.BootstrapHeld("synthetic_changed_root")

    mp.setattr(worker._Root, "check", root_check)
    reg = {"reader_entrypoint": owned.ENTRYPOINT}
    paths = {
        Path(owned.__file__).name: {
            "path": str(Path(owned.__file__)),
            "sha256": owned._LOADED_SOURCE_SHA256,
        },
        Path(confinement.__file__).name: {
            "path": str(Path(confinement.__file__)),
            "sha256": confinement._LOADED_SHA,
        },
    }
    object.__setattr__(
        root, "accepted_utf8", json.dumps({"purpose": {"consumer_id": "synthetic"}}).encode()
    )
    object.__setattr__(root, "installation", SimpleNamespace(source_closure=paths))
    controller = SimpleNamespace(
        config=SimpleNamespace(consumers={"synthetic": reg}), installation=root.installation
    )
    controller._require_writer = lambda *a: counts.__setitem__("writer", counts["writer"] + 1)
    controller.live = lambda *a: counts.__setitem__("live", counts["live"] + 1)
    operation = {}
    reader = object.__new__(owned.SourceReader)
    reader._controller = controller
    reader._operation = operation
    guard = SimpleNamespace(
        active=True,
        controller=controller,
        operation=operation,
        reader=reader,
        custody=SimpleNamespace(path=Path("/synthetic-owned-custody")),
    )
    frame = object()
    origin = object()
    tid = threading.get_ident()
    plan = SimpleNamespace(
        workspace="/synthetic-owned-worktree",
        permission_profile="synthetic",
        custody_path="/synthetic-owned-custody",
    )
    profile = {"profile_id": "synthetic"}
    route = owned.BootstrapRoute(
        guard,
        origin,
        json.dumps(profile).encode(),
        ("never-executed",),
        ("never-executed",),
        (),
        plan.workspace,
        (),
        (),
        plan.permission_profile,
        plan,
        tid,
    )
    s = object.__new__(owned._OwnedSession)
    s.thread = tid
    s.pid = os.getpid()
    s.frame = frame
    s.guard = guard
    s.reader = reader
    s.route = route
    s.phase = "metadata"
    s.refresh_count = 0
    s.turn_written = False
    s.output_limit = 65536
    p = SimpleNamespace(_pending=None, _id=8, _native_account_id="synthetic-account", _ready=True)
    p.snapshot = lambda: {
        "ready_for_source_admission": True,
        "native_claim_identity_match_present": True,
        "native_thread_selection": None,
    }
    p._count = lambda n: None
    p.feed = lambda *a, **kw: setattr(p, "_pending", None)
    s.protocol = p
    mp.setitem(
        owned._READERS, id(reader), (reader, root, controller, operation, tid, ctl.canonical(reg))
    )
    mp.setitem(owned._ROUTES, (tid, origin), route)
    mp.setitem(owned._SESSIONS, (tid, frame), s)
    mp.setattr(owned, "_managed_route_absent", lambda: None)
    mp.setattr(confinement, "verify_confinement_profile", lambda profile: None)
    mp.setattr(confinement, "_recheck", lambda profile: None)
    mp.setattr(confinement, "_require_builtin_helper_coverage", lambda *a: None)
    mp.setattr(confinement, "project_configuration_data", lambda *a: {})
    mp.setitem(confinement._WITNESSES, s, (plan, {}, tid))

    def write(session, raw):
        counts["writes"] += 1

    mp.setattr(owned, "_write_bytes", write)
    mp.setattr(
        owned,
        "_next_native_frame",
        lambda session: (
            b"{}"
            if p._pending is not None
            else (json.dumps({"id": 100000 + s.rpc_count, "result": {}}) + "\n").encode()
        ),
    )
    return counts, root, reader, s, controller


def measure():
    out = {}
    for phase in ["session_check", "identity", "confinement"]:
        with pytest.MonkeyPatch.context() as mp:
            c, root, reader, s, controller = world(mp)
            if phase == "session_check":
                s.check()
            elif phase == "identity":
                assert reader.read_native_identity() == {
                    "account_id": "synthetic-account",
                    "profile_id": "synthetic",
                }
            else:
                reader.verify_installed_confinement(
                    command=["never-executed"],
                    cwd=s.route.cwd,
                    env={},
                    custody_path=s.guard.custody.path,
                )
            out[phase] = dict(c)
    return out


def invoke(reader, s, phase):
    if phase == "identity":
        return reader.read_native_identity()
    return reader.verify_installed_confinement(
        command=["never-executed"], cwd=s.route.cwd, env={}, custody_path=s.guard.custody.path
    )


def test_actual_method_counts_retain_fixed_native_io_and_mutable_writer_checks():
    values = measure()
    assert values["session_check"] == dict(root=1, writes=0, writer=1, live=1, changed=False)
    assert values["identity"] == dict(root=2, writes=2, writer=2, live=2, changed=False)
    assert values["confinement"] == dict(root=4, writes=2, writer=3, live=3, changed=False)


@pytest.mark.parametrize("phase", ["identity", "confinement"])
@pytest.mark.parametrize(
    "change",
    [
        "changed_root",
        "expired",
        "foreign_session",
        "foreign_reader",
        "pid",
        "inactive",
        "physical_route",
    ],
)
def test_mutation_or_expired_lifetime_refuses_before_native_io(monkeypatch, phase, change):
    c, root, reader, s, controller = world(monkeypatch)
    if change == "changed_root":
        c["changed"] = True
    elif change == "expired":

        def expired(*a):
            raise owned.BootstrapHeld("synthetic_expired_operation")

        controller.live = expired
    elif change == "foreign_session":
        owned._SESSIONS.pop((s.thread, s.frame))
    elif change == "foreign_reader":
        owned._READERS.pop(id(reader))
    elif change == "pid":
        s.pid = -1
    elif change == "inactive":
        s.guard.active = False
    elif change == "physical_route":

        def changed():
            raise owned.BootstrapHeld("synthetic_changed_route")

        monkeypatch.setattr(owned, "_managed_route_absent", changed)
    with pytest.raises(owned.BootstrapHeld):
        invoke(reader, s, phase)
    assert c["writes"] == 0


def test_root_changes_during_same_call_writer_read_are_caught_before_io(monkeypatch):
    c, root, reader, s, controller = world(monkeypatch)
    controller.live = lambda *a: c.__setitem__("changed", True)
    with pytest.raises(owned.BootstrapHeld, match="synthetic_changed_root"):
        reader.read_native_identity()
    assert c["writes"] == 0


def test_identity_root_changed_by_last_native_response_cannot_escape(monkeypatch):
    c, root, reader, s, controller = world(monkeypatch)

    def feed(*a, **kw):
        s.protocol._pending = None
        if c["writes"] == 2:
            c["changed"] = True

    s.protocol.feed = feed
    with pytest.raises(owned.BootstrapHeld, match="synthetic_changed_root"):
        reader.read_native_identity()
    assert c["writes"] == 2 and c["root"] == 2


def test_confinement_root_changed_after_fresh_pair_cached_witness_cannot_escape(monkeypatch):
    c, root, reader, s, controller = world(monkeypatch)

    def project(*a):
        c["changed"] = True
        return {}

    monkeypatch.setattr(confinement, "project_configuration_data", project)
    with pytest.raises(owned.BootstrapHeld, match="synthetic_changed_root"):
        invoke(reader, s, "confinement")
    assert c["writes"] == 2


@pytest.mark.parametrize("phase", ["identity", "confinement"])
def test_writer_expires_after_native_pair_before_return(monkeypatch, phase):
    c, root, reader, s, controller = world(monkeypatch)
    original = controller.live

    def live(*a):
        if c["writes"] == 2:
            raise owned.BootstrapHeld("synthetic_expired_after_io")
        return original(*a)

    controller.live = live
    with pytest.raises(owned.BootstrapHeld, match="synthetic_expired_after_io"):
        invoke(reader, s, phase)
    assert c["writes"] == 2


def test_native_identity_refresh_limit_still_refuses_without_io(monkeypatch):
    c, root, reader, s, controller = world(monkeypatch)
    s.refresh_count = 3
    with pytest.raises(owned.BootstrapHeld):
        reader.read_native_identity()
    assert c["writes"] == 0


def test_confinement_still_refuses_changed_cached_projection(monkeypatch):
    c, root, reader, s, controller = world(monkeypatch)
    monkeypatch.setitem(
        confinement._WITNESSES, s, (s.route.confinement, {"changed": True}, s.thread)
    )
    with pytest.raises(owned.BootstrapHeld, match="confinement_confinement_session_changed"):
        invoke(reader, s, "confinement")
    assert c["writes"] == 2
