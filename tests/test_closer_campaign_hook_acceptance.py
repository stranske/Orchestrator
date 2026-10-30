"""Offline acceptance of the existing controller hook, not scheduler qualification.

All state is temporary. The ordering/repin cases explicitly document deployment
limitations so a green wiring result cannot be mistaken for current-demand admission.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from scripts import install_closer_campaign_hook as hook

import closer_campaign as campaign

SOURCE = (
    "def main(argv: list[str] | None = None) -> int:\n"
    "    import demand_router\n    return demand_router.main(argv)\n"
)


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("ORCH_CLOSER_CAMPAIGN_DIR", raising=False)
    monkeypatch.setenv("ORCH_CLOSER_CAMPAIGN_TICK", "1")


def write_valid_non_native_pin(root):
    """Real manifest validation and module hashes; no native/runner qualification."""
    pin = root / "pin.manifest.json"
    manifest = campaign.make_manifest(
        Path(campaign.__file__).parent,
        candidate_sha="a" * 40,
        state_versions={"campaign": 1, "release": 1},
        campaign_root=root,
        manifest_path=pin,
    )
    campaign.write_manifest(pin, manifest)
    return pin


def test_missing_pin_never_calls_tick(tmp_path, monkeypatch):
    monkeypatch.setattr(campaign, "tick", lambda *a: pytest.fail("no pin"))
    assert campaign.tick_safe() == []
    assert list(tmp_path.iterdir()) == []


def test_explicit_optout_never_calls_tick(tmp_path, monkeypatch):
    root = tmp_path / "closer-campaigns"
    root.mkdir()
    (root / "pin.manifest.json").write_text("{}")
    monkeypatch.setenv("ORCH_CLOSER_CAMPAIGN_TICK", "0")
    monkeypatch.setattr(campaign, "tick", lambda *a: pytest.fail("optout"))
    assert campaign.tick_safe() == []


def test_explicit_root_binds_state_and_pin_together(tmp_path, monkeypatch):
    root = tmp_path / "chosen"
    root.mkdir()
    pin = write_valid_non_native_pin(root)
    seen = []
    monkeypatch.setattr(campaign, "tick", lambda *args: seen.append(args) or [])
    assert campaign.tick_safe(str(root)) == []
    assert seen == [(root, pin)]


def test_hook_currently_ticks_before_demand_refresh(monkeypatch):
    # Characterization, not proof of due/no-demand gating. Deployment must supply
    # an admission contract before enabling a campaign on the controller path.
    seen = []
    monkeypatch.setattr(campaign, "tick_safe", lambda: seen.append("tick"))

    def fallback(argv):
        seen.append(("demand_refresh", argv))
        return 17

    assert campaign.controller_entry(["--collect"], fallback) == 17
    assert seen == ["tick", ("demand_refresh", ["--collect"])]


def test_v2_closure_refusal_never_ticks_or_runs_mutable_fallback(monkeypatch):
    monkeypatch.setattr(
        campaign,
        "controller_demand_before_discovery",
        lambda: {"publisher_closure": {}, "contract": {}},
    )
    monkeypatch.setattr(campaign, "tick_safe", lambda: pytest.fail("closure refusal ticked"))
    assert campaign.controller_entry(["--collect"], None) == 0
    assert campaign._LAST_CONTROLLER_DEMAND_REPORT["status"] == "held"


def test_v2_closure_refusal_ignores_an_old_hook_fallback(monkeypatch):
    monkeypatch.setattr(
        campaign,
        "controller_demand_before_discovery",
        lambda: {"publisher_closure": {}, "contract": {}},
    )
    monkeypatch.setattr(campaign, "tick_safe", lambda: pytest.fail("closure refusal ticked"))
    assert campaign.controller_entry(["--collect"], lambda _: pytest.fail("fallback ran")) == 0


def test_tick_failure_keeps_existing_demand_path(tmp_path, monkeypatch):
    root = tmp_path / "closer-campaigns"
    root.mkdir()
    write_valid_non_native_pin(root)

    def fail(*args):
        raise RuntimeError("offline fixture")

    monkeypatch.setattr(campaign, "tick", fail)
    assert campaign.tick_safe() == [{"kind": "error", "reason": "RuntimeError: offline fixture"}]
    assert campaign.controller_entry([], lambda argv: 19) == 19


def test_explicit_campaign_command_does_not_tick_or_fallback(monkeypatch):
    monkeypatch.setattr(campaign, "tick_safe", lambda: pytest.fail("scheduled tick"))
    monkeypatch.setattr(campaign, "main", lambda argv: 23 if argv == ["status"] else 1)
    assert (
        campaign.controller_entry(
            ["closer-campaign", "status"], lambda argv: pytest.fail("fallback")
        )
        == 23
    )


def test_render_preserves_existing_controller_and_has_one_hook(tmp_path):
    source = "# existing controller contract\n" + SOURCE
    rendered = hook.render(source, str(tmp_path / "pin.json"))
    assert rendered.startswith("# existing controller contract\n")
    assert rendered.count(hook.MARKER) == 1
    assert rendered.count("return campaign.controller_entry(argv, demand_router.main)") == 1
    assert hook.render(rendered, str(tmp_path / "pin.json")) == rendered
    compile(rendered, "controller.preview.py", "exec")


def test_v2_render_defers_mutable_router_import_until_campaign_refuses(tmp_path):
    pin = tmp_path / "pin.json"
    pin.write_text(
        '{"mechanism_contract":{"controller_demand":{"schema":"closer-controller-demand/v2"}}}'
    )
    rendered = hook.render(SOURCE, str(pin))
    assert "return campaign.controller_entry(argv, None)" in rendered
    assert rendered.index("campaign = _load_pinned_closer_campaign()") < rendered.index(
        "import demand_router"
    )


def test_existing_marker_does_not_repin_to_new_manifest(tmp_path):
    first = hook.render(SOURCE, str(tmp_path / "first.json"))
    with pytest.raises(SystemExit, match="pin|manifest"):
        hook.render(first, str(tmp_path / "second.json"))
    assert str(tmp_path / "second.json") not in first


@pytest.mark.parametrize(
    "source", [SOURCE + SOURCE, SOURCE.replace("return demand_router", "return other"), ""]
)
def test_unexpected_controller_shape_refuses(source):
    with pytest.raises(SystemExit, match="expected single"):
        hook.render(source, "/offline/pin.json")


def test_renderer_cannot_overwrite_input_controller(tmp_path):
    controller = tmp_path / "controller.py"
    controller.write_text(SOURCE)
    with pytest.raises(SystemExit):
        hook.main(
            ["--controller", str(controller), "--manifest", "/missing", "--out", str(controller)]
        )
    assert controller.read_text() == SOURCE


def test_differing_existing_pin_must_refuse_instead_of_claiming_success(tmp_path):
    first = hook.render(SOURCE, str(tmp_path / "first.json"))
    with pytest.raises(SystemExit, match="pin|manifest"):
        hook.render(first, str(tmp_path / "second.json"))


def test_existing_substantive_dispatch_remains_whole_assignment_pipeline(tmp_path):
    cfg = {
        "assignment": str(tmp_path / "assignment.json"),
        "dispatch_state": str(tmp_path / "queue.json"),
        "apply": True,
        "runtime_manifest": str(tmp_path / "runtime.json"),
    }
    command = campaign.dispatch_command(cfg, "/pinned/python", tmp_path)
    assert command[:3] == ["/pinned/python", str(tmp_path / "closer_dispatcher.py"), "run"]
    for flag, value in (
        ("--assignment", cfg["assignment"]),
        ("--state", cfg["dispatch_state"]),
        ("--runtime-manifest", cfg["runtime_manifest"]),
    ):
        assert command[command.index(flag) + 1] == value
    assert all(flag in command for flag in ("--apply", "--workers", "--pipeline"))


def test_changed_same_path_disk_pin_must_not_execute_cached_old_module(tmp_path):
    # Use the existing isolated subprocess fixture, never alter this pytest
    # process's imports or any installed controller/package.
    from test_closer_campaign_hook_loader import fixture, invoke

    _base, controller = fixture(tmp_path)
    script = """import runpy,sys,json,hashlib
from pathlib import Path
ns=runpy.run_path(sys.argv[1])
old=ns['_load_pinned_closer_campaign']()
assert old is not None
old.RUNTIME_VERSION='old-cache'
path=Path(old.__file__)
path.write_text(path.read_text()+chr(10)+'RUNTIME_VERSION="new-disk"'+chr(10))
compile(path.read_text(), str(path), 'exec')
manifest_path=Path(ns['CLOSER_CAMPAIGN_MANIFEST'])
manifest=json.loads(manifest_path.read_text())
manifest['modules']['closer_campaign.py']=hashlib.sha256(path.read_bytes()).hexdigest()
manifest_path.write_text(json.dumps(manifest))
loaded=ns['_load_pinned_closer_campaign']()
print(json.dumps(loaded is None or getattr(loaded,'RUNTIME_VERSION',None)=='new-disk'))
"""
    assert invoke(controller, script) is True


@pytest.mark.parametrize(
    "mutation",
    [
        lambda source: source + hook.MARKER + "\n",
        lambda source: source.replace(
            "CLOSER_CAMPAIGN_MANIFEST = ", "CLOSER_CAMPAIGN_MANIFEST = object() # ", 1
        ),
        lambda source: source + "\nCLOSER_CAMPAIGN_MANIFEST = '/conflict'\n",
        lambda source: source.replace("CLOSER_CAMPAIGN_MANIFEST = ", "OTHER_PIN = ", 1),
        lambda source: source + "\nthis is invalid python !\n",
        lambda source: source.replace(
            "return campaign.controller_entry(argv, demand_router.main)", "return 0"
        ),
        lambda source: source
        + "\ndef overwrite():\n    global CLOSER_CAMPAIGN_MANIFEST\n    CLOSER_CAMPAIGN_MANIFEST = '/conflict'\n",
    ],
    ids=[
        "multiple-markers",
        "computed-pin",
        "multiple-bindings",
        "missing-binding",
        "syntax",
        "changed-hook",
        "nested-binding",
    ],
)
def test_marked_malformed_controller_refuses(tmp_path, mutation):
    pin = str(tmp_path / "pin.json")
    rendered = hook.render(SOURCE, pin)
    with pytest.raises(SystemExit, match="manifest pin rejected"):
        hook.render(mutation(rendered), pin)


def test_marker_without_loader_is_not_idempotent_success(tmp_path):
    with pytest.raises(SystemExit, match="manifest pin rejected"):
        hook.render(hook.MARKER + "\n" + SOURCE, str(tmp_path / "pin.json"))


def test_same_binding_idempotency_normalizes_requested_path(tmp_path):
    pin = tmp_path / "pin.json"
    rendered = hook.render(SOURCE, str(pin))
    assert hook.render(rendered, str(tmp_path / "subdir" / ".." / "pin.json")) == rendered


@pytest.mark.parametrize(
    "name",
    [
        "claims",
        "closer_release",
        "closer_runtime_binding",
        "closer_campaign",
        "router",
        "execution_profiles",
        "closer_distribution_package",
    ],
)
def test_same_path_cached_dependency_refuses_without_import_state_mutation(tmp_path, name):
    from test_closer_campaign_hook_loader import fixture, invoke

    base, controller = fixture(tmp_path)
    script = f"""import runpy,sys,json,types
ns=runpy.run_path(sys.argv[1])
foreign=types.ModuleType({name!r});foreign.__file__={str(base / (name + '.py'))!r}
sys.modules[{name!r}]=foreign
before=dict(sys.modules);paths=list(sys.path);bytecode=sys.dont_write_bytecode
loaded=ns['_load_pinned_closer_campaign']()
print(json.dumps([loaded is None,all(sys.modules.get(key) is value for key,value in before.items()),sys.path==paths,sys.dont_write_bytecode==bytecode]))
"""
    assert invoke(controller, script) == [True, True, True, True]
