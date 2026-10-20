"""Pinned controller hook lazy imports stay in the verified closure (offline subprocesses)."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys

import pytest
from scripts import install_closer_campaign_hook as hook

CONTROLLER = (
    "def main(argv: list[str] | None = None) -> int:\n"
    "    import demand_router\n    return demand_router.main(argv)\n"
)


def fixture(tmp_path):
    base = tmp_path / "pin"
    base.mkdir()
    sources = {
        "claims.py": 'VALUE = "pinned-claims"\n',
        "closer_release.py": 'VALUE = "pinned-release"\n',
        "closer_runtime_binding.py": 'VALUE = "pinned-binding"\n',
        "closer_campaign.py": (
            "def lazy():\n    import router, execution_profiles\n"
            "    from closer_distribution_package import VALUE\n"
            "    return [router.VALUE, execution_profiles.VALUE, VALUE]\n"
        ),
        "router.py": 'VALUE = "pinned-router"\n',
        "execution_profiles.py": 'VALUE = "pinned-profiles"\n',
        "closer_distribution_package.py": 'VALUE = "pinned-distribution"\n',
    }
    for name, source in sources.items():
        (base / name).write_text(source)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "module_dir": str(base),
                "modules": {
                    name: hashlib.sha256(source.encode()).hexdigest()
                    for name, source in sources.items()
                },
            }
        )
    )
    controller = tmp_path / "controller.py"
    controller.write_text(hook.render(CONTROLLER, str(manifest)))
    return base, controller


def invoke(controller, script):
    result = subprocess.run(
        [sys.executable, "-B", "-c", script, str(controller)],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_full_lazy_closure_beats_ambient_path_even_if_pin_was_later(tmp_path):
    base, controller = fixture(tmp_path)
    decoy = tmp_path / "ambient"
    decoy.mkdir()
    for name in ("router", "execution_profiles", "closer_distribution_package"):
        (decoy / (name + ".py")).write_text('raise AssertionError("ambient imported")\n')
    script = f"""import runpy,sys,json
sys.path[:0] = [{str(decoy)!r}, {str(base)!r}]
module=runpy.run_path(sys.argv[1])['_load_pinned_closer_campaign']()
assert module is not None
assert sys.path[0] == {str(base)!r}
assert 'router' not in sys.modules and 'execution_profiles' not in sys.modules
print(json.dumps(module.lazy()))
"""
    assert invoke(controller, script) == ["pinned-router", "pinned-profiles", "pinned-distribution"]


@pytest.mark.parametrize("name", ["router", "execution_profiles", "closer_distribution_package"])
def test_already_loaded_foreign_lazy_dependency_refuses_without_replacing_it(tmp_path, name):
    _base, controller = fixture(tmp_path)
    script = f"""import runpy,sys,json,types
foreign=types.ModuleType({name!r});foreign.__file__='/ambient/{name}.py'
sys.modules[{name!r}]=foreign
loaded=runpy.run_path(sys.argv[1])['_load_pinned_closer_campaign']()
print(json.dumps([loaded is None,sys.modules[{name!r}] is foreign]))
"""
    assert invoke(controller, script) == [True, True]


def test_lazy_module_tampering_refuses_before_any_pinned_module_executes(tmp_path):
    base, controller = fixture(tmp_path)
    (base / "router.py").write_text('raise AssertionError("tampered")\n')
    script = """import runpy,sys,json
loaded=runpy.run_path(sys.argv[1])['_load_pinned_closer_campaign']()
print(json.dumps([loaded is None,'closer_campaign' not in sys.modules]))
"""
    assert invoke(controller, script) == [True, True]


@pytest.mark.parametrize(
    "name", ["router.py", "execution_profiles.py", "closer_distribution_package.py"]
)
def test_missing_lazy_dependency_pin_holds_instead_of_resolving_host(tmp_path, name):
    _base, controller = fixture(tmp_path)
    manifest = tmp_path / "manifest.json"
    doc = json.loads(manifest.read_text())
    del doc["modules"][name]
    manifest.write_text(json.dumps(doc))
    script = "import runpy,sys,json;print(json.dumps(runpy.run_path(sys.argv[1])['_load_pinned_closer_campaign']() is None))"
    assert invoke(controller, script) is True
