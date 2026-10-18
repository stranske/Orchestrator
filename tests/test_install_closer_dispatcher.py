"""Installer controls run against disposable homes, never live automations."""

import fcntl
import importlib.util
import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "install_closer_dispatcher",
    Path(__file__).resolve().parents[1] / "scripts/install_closer_dispatcher.py",
)
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


def fake_home(tmp_path, *, label_repair=False):
    home = tmp_path / "home"
    prompt = home / ".codex/automations/imi-merge-verify-closer/automation.toml"
    prompt.parent.mkdir(parents=True)
    prompt.write_text(
        '# preserved comment\nversion = 1\nstatus = "PAUSED"\nmodel = "gpt-6.1-sol"\n'
        'rrule = "FREQ=HOURLY"\nmodel_reasoning_effort = "medium"\n'
        'prompt = "After the sweep, select at most one complex repo lane per run.\\nHANDOFF_AGENT=codex"\n'
    )
    opener = home / ".codex/automations/pd-workloop-resume/automation.toml"
    opener.parent.mkdir(parents=True)
    opener.write_text('prompt = "opener HANDOFF_AGENT=codex"\n')
    postrun = home / ".codex/bin/handoff-postrun.sh"
    postrun.parent.mkdir(parents=True)
    trap = (
        'trap \'rm -rf "$LANE_MUTEX_DIR" 2>/dev/null || true; '
        "launch_keepalive_label_repair' EXIT"
        if label_repair
        else installer.TRAP_ANCHOR
    )
    postrun.write_text(
        '#!/bin/bash\nset -euo pipefail\nlane="${1:-closer}"\n'
        'LANE_MUTEX_DIR="$HOME/mutex"\n'
        + (
            'launch_keepalive_label_repair() { echo repaired >> "$HOME/repair-actions"; }\n'
            if label_repair
            else ""
        )
        + trap
        + "\n"
        + 'echo end-round >> "$HOME/actions"\necho relay >> "$HOME/actions"\n'
    )
    postrun.chmod(0o755)
    dispatcher = home / ".codex/orchestrator-mirror/closer_dispatcher.py"
    dispatcher.parent.mkdir(parents=True)
    dispatcher.write_text(
        "import os,sys\nfrom pathlib import Path\n"
        'Path(os.environ["HOME"],"validation-args").write_text(repr(sys.argv))\n'
        'sys.exit(int(os.environ.get("FAKE_VALIDATION_EXIT","0")))\n'
    )
    renderer = postrun.with_name("render-claude-prompts.sh")
    renderer.write_text(
        f"#!{sys.executable}\n"
        "import os,tomllib\nfrom pathlib import Path\n"
        'h=Path(os.environ["HOME"])\n'
        'for lane,ident in [("opener","pd-workloop-resume"),("closer","imi-merge-verify-closer")]:\n'
        ' source=h/".codex/automations"/ident/"automation.toml"\n'
        ' p=tomllib.loads(source.read_text())["prompt"].replace("HANDOFF_AGENT=codex","HANDOFF_AGENT=claude_code")\n'
        ' header=(f"<!-- AUTO-GENERATED from {source} by render-claude-prompts.sh — DO NOT EDIT. "\n'
        ' "Regenerated from the TOML (the single source of truth) at the start of every Claude "\n'
        ' "scheduled round; hand edits here are overwritten. Edit the prompt in the TOML. -->\\n\\n")\n'
        ' out=h/f".codex/handoff/prompts/claude-{lane}.md"\n'
        " out.parent.mkdir(parents=True,exist_ok=True)\n"
        " out.write_text(header+p)\n"
    )
    renderer.chmod(0o755)
    return home, prompt, postrun, renderer


def test_dry_run_preserves_every_runtime_byte_and_nonprompt_field(tmp_path):
    home, prompt, postrun, _ = fake_home(tmp_path)
    before = {p: p.read_bytes() for p in home.rglob("*") if p.is_file()}
    receipt = installer.install(home)
    assert receipt["mode"] == "dry-run" and len(receipt["changes"]) == 2
    assert before == {p: p.read_bytes() for p in home.rglob("*") if p.is_file()}
    transformed = installer.updated_prompt(prompt.read_bytes(), "new policy")
    old, new = tomllib.loads(prompt.read_text()), tomllib.loads(transformed.decode())
    assert {k: v for k, v in old.items() if k != "prompt"} == {
        k: v for k, v in new.items() if k != "prompt"
    }
    assert "at most one complex repo lane" not in new["prompt"]
    assert transformed.startswith(b"# preserved comment\n")


def test_apply_idempotence_and_preserved_executable_mode(tmp_path):
    home, prompt, postrun, _ = fake_home(tmp_path)
    installer.install(home, apply=True)
    first = prompt.read_bytes(), postrun.read_bytes()
    second = installer.install(home, apply=True)
    assert first == (prompt.read_bytes(), postrun.read_bytes())
    assert second["changes"] == []
    assert postrun.stat().st_mode & 0o777 == 0o755
    assert (
        "HANDOFF_AGENT=claude_code"
        in (home / ".codex/handoff/prompts/claude-closer.md").read_text()
    )


def test_default_reinstallation_never_downgrades_enforced_gate(tmp_path):
    home, _, postrun, _ = fake_home(tmp_path)
    installer.install(home, apply=True, enforce=True)
    before = postrun.read_bytes()
    receipt = installer.install(home, apply=True)
    assert receipt["stop_enforcement"] == "required"
    assert receipt["changes"] == [] and postrun.read_bytes() == before


@pytest.mark.parametrize("validation_exit", [0, 1])
@pytest.mark.parametrize("label_repair", [False, True])
def test_gate_precedes_round_and_relay_but_failure_runs_exit_cleanup(
    tmp_path, validation_exit, label_repair
):
    home, _, postrun, _ = fake_home(tmp_path, label_repair=label_repair)
    installer.install(home, apply=True, enforce=True)
    (home / "mutex").mkdir()
    result = subprocess.run(
        ["/bin/bash", str(postrun), "closer"],
        env=dict(
            os.environ,
            HOME=str(home),
            CLOSER_ROUND_STATE=str(home / "round.json"),
            FAKE_VALIDATION_EXIT=str(validation_exit),
        ),
        capture_output=True,
    )
    assert not (home / "mutex").exists()
    assert (home / "repair-actions").exists() is label_repair
    if label_repair:
        assert (home / "repair-actions").read_text() == "repaired\n"
    assert (home / "actions").exists() == (validation_exit == 0)
    assert result.returncode == (0 if validation_exit == 0 else 65)
    assert "--assignment" in (home / "validation-args").read_text()


@pytest.mark.parametrize("enforce", [False, True])
def test_migration_missing_state_does_not_break_old_round(tmp_path, enforce):
    home, _, postrun, _ = fake_home(tmp_path)
    installer.install(home, apply=True, enforce=enforce)
    (home / "mutex").mkdir()
    env = dict(os.environ, HOME=str(home))
    env.pop("CLOSER_ROUND_STATE", None)
    result = subprocess.run(["/bin/bash", str(postrun), "closer"], env=env, capture_output=True)
    assert result.returncode == (65 if enforce else 0)
    assert (home / "actions").exists() is (not enforce)
    assert not (home / "mutex").exists()
    if not enforce:
        assert b"migration pending" in result.stderr


def test_missing_dispatcher_fails_closed_only_for_closer(tmp_path):
    home, _, postrun, _ = fake_home(tmp_path)
    installer.install(home, apply=True, enforce=True)
    (home / ".codex/orchestrator-mirror/closer_dispatcher.py").unlink()
    env = dict(os.environ, HOME=str(home), CLOSER_ROUND_STATE=str(home / "round.json"))
    closer = subprocess.run(["/bin/bash", str(postrun), "closer"], env=env, capture_output=True)
    assert closer.returncode == 65 and not (home / "actions").exists()
    opener = subprocess.run(["/bin/bash", str(postrun), "opener"], env=env, capture_output=True)
    assert opener.returncode == 0 and (home / "actions").exists()


def test_renderer_failure_rolls_back_sources_and_retains_exact_backups(tmp_path):
    home, prompt, postrun, renderer = fake_home(tmp_path)
    before = {p: p.read_bytes() for p in (prompt, postrun)}
    renderer.write_text("#!/bin/bash\nexit 42\n")
    with pytest.raises(subprocess.CalledProcessError):
        installer.install(home, apply=True)
    assert all(path.read_bytes() == content for path, content in before.items())
    for path, content in before.items():
        backups = list(path.parent.glob(path.name + ".closer-dispatcher-backup-*"))
        assert len(backups) == 1 and backups[0].read_bytes() == content


def test_compare_before_replace_rejects_competing_source_and_preserves_it(tmp_path):
    path = tmp_path / "source"
    path.write_bytes(b"other writer")
    with pytest.raises(RuntimeError, match="concurrent source change"):
        installer.atomic_write(path, b"ours", b"original")
    assert path.read_bytes() == b"other writer"


def test_rollback_never_overwrites_concurrent_source_change(tmp_path, monkeypatch):
    home, prompt, _, _ = fake_home(tmp_path)

    def competing_renderer(*args, **kwargs):
        prompt.write_text("concurrent writer payload")
        raise subprocess.CalledProcessError(1, args[0])

    monkeypatch.setattr(installer.subprocess, "run", competing_renderer)
    with pytest.raises(RuntimeError, match="rollback refused concurrent"):
        installer.install(home, apply=True)
    assert prompt.read_text() == "concurrent writer payload"


def test_unknown_postrun_anchor_refuses_install_before_mutation(tmp_path):
    home, prompt, postrun, _ = fake_home(tmp_path)
    postrun.write_text("#!/bin/bash\necho changed architecture\n")
    before = prompt.read_bytes()
    with pytest.raises(ValueError, match="anchor changed"):
        installer.install(home, apply=True)
    assert prompt.read_bytes() == before


def test_ambiguous_postrun_anchors_refuse_install_before_mutation(tmp_path):
    home, prompt, postrun, _ = fake_home(tmp_path)
    postrun.write_text(
        postrun.read_text() + 'trap \'rm -rf "$LANE_MUTEX_DIR" 2>/dev/null || true; '
        "launch_keepalive_label_repair' EXIT\n"
    )
    before = prompt.read_bytes()
    with pytest.raises(ValueError, match="anchor changed"):
        installer.install(home, apply=True)
    assert prompt.read_bytes() == before


def test_cooperating_concurrent_install_is_refused(tmp_path):
    home, prompt, _, _ = fake_home(tmp_path)
    before = prompt.read_bytes()
    with (home / ".codex/bin/.install-closer-dispatcher.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with pytest.raises(RuntimeError, match="another closer dispatcher"):
            installer.install(home, apply=True)
    assert prompt.read_bytes() == before


def test_renderer_failure_preserves_unknown_concurrent_cache_write(tmp_path, monkeypatch):
    home, prompt, postrun, _ = fake_home(tmp_path)
    before = prompt.read_bytes(), postrun.read_bytes()
    cache = home / ".codex/handoff/prompts/claude-closer.md"
    cache.parent.mkdir(parents=True)
    cache.write_bytes(b"old generated cache")

    def concurrent_render(*args, **kwargs):
        cache.write_bytes(b"unrecognized competing write")
        raise subprocess.CalledProcessError(1, args[0])

    monkeypatch.setattr(installer.subprocess, "run", concurrent_render)
    with pytest.raises(RuntimeError, match="rollback refused concurrent"):
        installer.install(home, apply=True)
    assert (prompt.read_bytes(), postrun.read_bytes()) == before
    assert cache.read_bytes() == b"unrecognized competing write"


def test_cli_defaults_to_dry_run(tmp_path):
    home, _, _, _ = fake_home(tmp_path)
    output = subprocess.check_output([sys.executable, str(SPEC.origin), "--home", str(home)])
    assert json.loads(output)["mode"] == "dry-run"


def add_family_renderers(home, claude_renderer):
    for family in ("cursor", "muse"):
        script = home / f".codex/bin/render-{family}-prompts.sh"
        script.write_text(
            claude_renderer.read_text()
            .replace("claude-{lane}.md", f"{family}-{{lane}}.md")
            .replace('"HANDOFF_AGENT=claude_code"', f'"HANDOFF_AGENT={family}"')
            .replace("out.write_text(header+p)", "out.write_text(p)")
        )
        script.chmod(0o755)


def test_installed_optional_renderers_refresh_all_six_source_bound_caches(tmp_path):
    home, prompt, _, renderer = fake_home(tmp_path)
    add_family_renderers(home, renderer)
    receipt = installer.install(home, apply=True)
    assert receipt["rendered_families"] == ["claude", "cursor", "muse"]
    value = tomllib.loads(prompt.read_text())["prompt"]
    for family in ("cursor", "muse"):
        actual = (home / f".codex/handoff/prompts/{family}-closer.md").read_text()
        assert actual == value.replace("HANDOFF_AGENT=codex", f"HANDOFF_AGENT={family}")
        assert not actual.startswith("<!-- AUTO-GENERATED")


def test_failure_in_last_renderer_rolls_back_all_source_and_cache_families(tmp_path):
    home, prompt, postrun, renderer = fake_home(tmp_path)
    add_family_renderers(home, renderer)
    originals = {p: p.read_bytes() for p in (prompt, postrun)}
    caches = []
    for family in ("claude", "cursor", "muse"):
        for lane in ("opener", "closer"):
            cache = home / f".codex/handoff/prompts/{family}-{lane}.md"
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_bytes(f"old-{family}-{lane}".encode())
            caches.append(cache)
    before = {p: p.read_bytes() for p in caches}
    last = home / ".codex/bin/render-muse-prompts.sh"
    last.write_text(last.read_text() + "\nraise SystemExit(42)\n")
    with pytest.raises(subprocess.CalledProcessError):
        installer.install(home, apply=True)
    assert all(p.read_bytes() == b for p, b in originals.items())
    assert all(p.read_bytes() == b for p, b in before.items())


def test_missing_required_renderer_rejects_without_source_changes(tmp_path):
    home, prompt, postrun, renderer = fake_home(tmp_path)
    before = prompt.read_bytes(), postrun.read_bytes()
    renderer.unlink()
    with pytest.raises(ValueError, match="claude prompt renderer unavailable"):
        installer.install(home, apply=True)
    assert (prompt.read_bytes(), postrun.read_bytes()) == before


def test_unchanged_sources_still_repair_a_stale_optional_cache(tmp_path):
    home, prompt, _, renderer = fake_home(tmp_path)
    add_family_renderers(home, renderer)
    installer.install(home, apply=True)
    source = prompt.read_bytes()
    cache = home / ".codex/handoff/prompts/cursor-closer.md"
    cache.write_bytes(b"stale")
    receipt = installer.install(home, apply=True)
    assert receipt["changes"] == [] and receipt["rendered"]
    assert prompt.read_bytes() == source
    assert cache.read_text() == tomllib.loads(source.decode())["prompt"].replace(
        "HANDOFF_AGENT=codex", "HANDOFF_AGENT=cursor"
    )
