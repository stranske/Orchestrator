"""Fixture-only scheduled interpreter regressions; no launchd or production scripts run."""

from __future__ import annotations

import hashlib
import json
import os
import plistlib
import subprocess
import sys
from pathlib import Path

import pytest
from test_closer_native_observer import host as host

import python_standard_audit as audit
import switch_review


def test_cursor_closer_native_entry_not_refused_line(tmp_path):
    closer = tmp_path / "imi-merge-verify-closer" / "automation.toml"
    controller = tmp_path / "reviewed-repo-automation-controller" / "automation.toml"
    closer.parent.mkdir()
    controller.parent.mkdir()
    closer.write_text('model = "gpt-6.1-sol"\n')
    controller.write_text('model = "gpt-6-luna"\n')
    manifest = tmp_path / "host-manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "policy": {
                    "toml_sha256": {
                        str(closer): hashlib.sha256(closer.read_bytes()).hexdigest(),
                        str(controller): hashlib.sha256(controller.read_bytes()).hexdigest(),
                    }
                }
            }
        )
    )
    log = tmp_path / "cursor-closer.out"
    log.write_text(
        "\n".join(
            [
                "unrelated output",
                '{"launcher": "refused"}',
                '{"launcher": "refused"}',
                '{"launcher": "refused"}',
            ]
        )
        + "\n"
    )

    refused = switch_review.cursor_closer_native_entry_snapshot(log, manifest)
    assert switch_review.format_cursor_closer_native_entry(refused) == (
        "Cursor closer native entry: refused 3/3 launcher results; "
        "pinned-vs-current automation.toml hash mismatches 0 (none)"
    )
    original_closer = closer.read_bytes()
    original_controller = controller.read_bytes()
    closer.write_text('model = "changed-closer"\n')
    controller.write_text('model = "changed-controller"\n')
    mismatched = switch_review.cursor_closer_native_entry_snapshot(log, manifest)
    mismatch_line = switch_review.format_cursor_closer_native_entry(mismatched)
    assert "refused 3/3 launcher results" in mismatch_line
    assert "hash mismatches 2" in mismatch_line
    assert "imi-merge-verify-closer/automation.toml" in mismatch_line
    assert "reviewed-repo-automation-controller/automation.toml" in mismatch_line
    rendered = switch_review.format_report(
        {
            "review_days": 7,
            "raise_count": 0,
            "held_off": [],
            "on_but_idle": [],
            "unconditioned": [],
            "mirror_drift": {"status": "ok"},
            "python_standard": {},
            "cursor_closer_native_entry": mismatched,
        }
    )
    assert rendered.count("Cursor closer native entry:") == 1
    assert rendered.index("Python standard:") < rendered.index("Cursor closer native entry:")
    assert mismatch_line in rendered

    # This concrete M>0 input is the latched-gate Q4 proof for the distinct drained wording.
    log.write_text('{"launcher": "completed"}\n{"launcher": "completed"}\n')
    still_mismatched = switch_review.cursor_closer_native_entry_snapshot(log, manifest)
    mismatch_line = switch_review.format_cursor_closer_native_entry(still_mismatched)
    assert "refused 0/2 launcher results" in mismatch_line
    assert "hash mismatches 2" in mismatch_line
    assert "drained" not in mismatch_line

    closer.write_bytes(original_closer)
    controller.write_bytes(original_controller)
    drained = switch_review.cursor_closer_native_entry_snapshot(log, manifest)
    drained_line = switch_review.format_cursor_closer_native_entry(drained)
    assert drained_line == (
        "Cursor closer native entry: drained: refused 0/2 launcher results; "
        "pinned-vs-current automation.toml hash mismatches 0 (none)"
    )

    log.unlink()
    unreadable = switch_review.cursor_closer_native_entry_snapshot(log, manifest)
    unreadable_line = switch_review.format_cursor_closer_native_entry(unreadable)
    assert unreadable_line == (
        "Cursor closer native entry: cannot read launcher log (FileNotFoundError); "
        "pinned-vs-current automation.toml hash mismatches 0 (none)"
    )
    assert "refused 0/" not in unreadable_line


@pytest.mark.parametrize("manifest_contents", [None, "invalid json", "[]", "{}"])
def test_cursor_closer_cannot_drain_without_readable_policy_pins(tmp_path, manifest_contents):
    log = tmp_path / "cursor-closer.out"
    log.write_text('{"launcher": "completed"}\n')
    manifest = tmp_path / "host-manifest.json"
    if manifest_contents is not None:
        manifest.write_text(manifest_contents)
    snapshot = switch_review.cursor_closer_native_entry_snapshot(log, manifest)
    line = switch_review.format_cursor_closer_native_entry(snapshot)
    assert "refused 0/1 launcher results" in line
    assert "hashes cannot read" in line
    assert "drained" not in line


def test_cursor_closer_native_status_refuses_changed_pin_then_recovers(host, tmp_path):
    manifest, manifest_path, _ = host
    original = manifest_path.read_bytes()
    log = tmp_path / "cursor-closer.out"
    log.write_text('{"launcher": "completed"}\n')
    command = [
        manifest["runtime"]["python"],
        manifest["launcher"]["path"],
        "native-status",
        "--manifest",
        str(manifest_path),
    ]

    healthy = subprocess.run(command, capture_output=True, text=True, timeout=30)
    assert healthy.returncode == 0, healthy.stdout + healthy.stderr
    assert json.loads(healthy.stdout).get("launcher") != "refused"
    assert "drained" in switch_review.format_cursor_closer_native_entry(
        switch_review.cursor_closer_native_entry_snapshot(log, manifest_path)
    )

    pinned_path = next(path for path in manifest["policy"]["toml_sha256"] if "imi-merge" in path)
    try:
        manifest["policy"]["toml_sha256"][pinned_path] = "0" * 64
        manifest_path.write_text(json.dumps(manifest))
        broken = subprocess.run(command, capture_output=True, text=True, timeout=30)
        assert broken.returncode == 78, broken.stdout + broken.stderr
        result = json.loads(broken.stdout)
        assert result["launcher"] == "refused"
        assert "configured policy changed since host pin" in str(result["problems"])
        line = switch_review.format_cursor_closer_native_entry(
            switch_review.cursor_closer_native_entry_snapshot(log, manifest_path)
        )
        assert "hash mismatches 1 (imi-merge-verify-closer/automation.toml)" in line
        assert "drained" not in line
    finally:
        manifest_path.write_bytes(original)

    restored = subprocess.run(command, capture_output=True, text=True, timeout=30)
    assert restored.returncode == 0, restored.stdout + restored.stderr
    assert json.loads(restored.stdout).get("launcher") != "refused"
    line = switch_review.format_cursor_closer_native_entry(
        switch_review.cursor_closer_native_entry_snapshot(log, manifest_path)
    )
    assert "drained: refused 0/1" in line
    assert "hash mismatches 0 (none)" in line


@pytest.fixture
def fleet(tmp_path):
    directory = tmp_path / "LaunchAgents"
    directory.mkdir()
    calls = []

    def resolver(command, path, login, home):
        calls.append((command, path, login, home))
        if "/" not in command and audit.PYTHON.fullmatch(command):
            command = path.split(":")[0] + "/" + command
        version = "3.9.6" if command.startswith("/usr/bin/") else "3.14.3"
        if "anaconda3" in command:
            version = "3.12.2"
        if command.startswith("/other/"):
            version = "3.11.9"
        return audit.Resolution(command, "Python " + version)

    def job(name, args, **extra):
        label = "com.stranske." + name
        (directory / (name + ".plist")).write_bytes(
            plistlib.dumps({"Label": label, "ProgramArguments": args, **extra})
        )
        return label

    def script(body, name="entry.sh"):
        file = tmp_path / name
        file.write_text(body)
        return str(file)

    def measure(**kwargs):
        return audit.audit(directory, home=tmp_path, resolver=resolver, **kwargs)

    return job, script, measure, calls, directory


def test_expand_leaves_longer_variable_names_alone(fleet):
    job, script, measure, calls, directory = fleet
    home = directory.parent
    for value in ("$HOMEBREW_PREFIX/bin", "$PATH_EXTRA", "${HOMEBREW_PREFIX}", "${PATH_EXTRA}"):
        assert audit.expand(value, home, audit.DEFAULT_PATH) == value
    assert audit.expand("$HOME:${HOME}:$PATH:${PATH}", home, "/bin") == f"{home}:{home}:/bin:/bin"
    # Replacement values are not expanded again in the same call.
    assert audit.expand("$HOME", Path("/literal/$PATH"), "/bin") == "/literal/$PATH"
    entry = script('export PATH="$HOMEBREW_PREFIX/bin:$PATH"\nexec python3 -c pass\n')
    job("dynamic-path", ["/bin/bash", entry])
    report = measure()
    assert report["status"] == "unmeasured"
    assert report["rows"][0]["class"] == "unmeasured"
    assert "dynamic PATH" in " ".join(report["needs"])
    assert calls == []


@pytest.mark.parametrize("first_line", ['"""Run python tests."""', "'''Don't'''"])
def test_python_entry_without_shebang_is_not_scanned_as_shell(fleet, first_line):
    job, script, measure, calls, _ = fleet
    entry = script(first_line + "\n", "task.py")
    job("no-shebang", [entry])
    report = measure()
    assert report["status"] == "measured"
    assert report["rows"][0]["class"] == "no-python"
    assert report["rows"][0]["evidence"] == []
    assert calls == []


def test_non_utf8_locale_does_not_lose_measurement(fleet):
    job, _, _, _, directory = fleet
    home = directory.parent
    entry = home / "task.py"
    entry.write_text('"""Café."""\n', encoding="utf-8")
    shim = home / ".codex/bin/python3"
    shim.parent.mkdir(parents=True)
    shim.write_text('#!/bin/sh\n# Café\nexec "$(standard-python)" "$@"\n', encoding="utf-8")
    job("utf8", [str(shim), str(entry)])
    # Use a fresh process: changing LC_ALL after Python starts does not change its encoding.
    # Disable UTF-8 mode and locale coercion so implicit reads really use the C locale.
    code = """
import json
import locale
import subprocess
import sys
from pathlib import Path
import python_standard_audit as audit

assert not sys.flags.utf8_mode
assert locale.getpreferredencoding(False).lower() not in ('utf-8', 'utf8')

def run(argv, **kwargs):
    output = 'Python 3.14.3' if argv[-1] == '--version' else '/standard/python3'
    return subprocess.CompletedProcess(argv, 0, output, '')

# Only interpreter lookup/version responses are faked; both UTF-8 file reads are real.
audit.subprocess.run = run
print(json.dumps(audit.audit(Path(sys.argv[1]), home=Path(sys.argv[2]))))
"""
    result = subprocess.run(
        [sys.executable, "-X", "utf8=0", "-c", code, str(directory), str(home)],
        env={
            **os.environ,
            "LC_ALL": "C",
            "PYTHONUTF8": "0",
            "PYTHONCOERCECLOCALE": "0",
            "PYTHONPATH": str(Path(audit.__file__).parent),
        },
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        timeout=10,
    )
    report = json.loads(result.stdout)
    assert report["status"] == "measured", report["needs"]
    assert report["rows"][0]["class"] == "standard"
    assert report["rows"][0]["evidence"][0]["executable"] == "/standard/python3"


@pytest.mark.parametrize(
    ("interpreter", "category", "bad"),
    [
        ("/usr/bin/python3", "system-3.9", 1),
        ("/opt/anaconda3/bin/python3", "anaconda-3.12", 1),
        ("/opt/anaconda3/bin/python", "anaconda-3.12", 1),
        ("/standard/python3", "standard", 0),
        ("/other/python3", "other", 1),
    ],
)
def test_direct_jobs_use_interpreters_own_versions(fleet, interpreter, category, bad):
    job, _, measure, _, _ = fleet
    job("direct", [interpreter, "-c", "print('not executed')"])
    report = measure()
    assert report["status"] == "measured"
    assert report["bad"] == bad
    assert report["rows"][0]["class"] == category
    assert bool("(drained)" in audit.format_line(report)) == (category == "standard")


@pytest.mark.parametrize("command", ["python", "python3"])
def test_bare_command_uses_plist_path(fleet, command):
    job, _, measure, calls, _ = fleet
    job("bare", [command, "-c", "pass"], EnvironmentVariables={"PATH": "/standard:/bin"})
    assert measure()["bad"] == 0
    assert calls[0][:3] == (command, "/standard:/bin", False)


def test_missing_path_uses_launchd_default_and_login_flag(fleet):
    job, _, measure, calls, _ = fleet
    job("login", ["/bin/bash", "-lc", "exec python3 -c pass"])
    assert measure()["bad"] == 1
    assert calls[0][:3] == ("python3", audit.DEFAULT_PATH, True)


def test_script_hardcoded_pin_wins_over_standard_reference(fleet):
    job, script, measure, _, _ = fleet
    entry = script('source "$HOME/.codex/bin/py-standard.sh"\n/usr/bin/python3 task.py\n')
    job("pin", ["/bin/bash", entry])
    report = measure()
    assert report["bad"] == 1
    assert {e["class"] for e in report["rows"][0]["evidence"]} == {"standard", "system-3.9"}


def test_standard_selector_cannot_mask_independent_bare_python(fleet):
    job, script, measure, calls, _ = fleet
    entry = script('source "$HOME/.codex/bin/py-standard.sh"\npython3 -c pass\n')
    job("bare-beside-selector", ["/bin/bash", entry])
    report = measure()
    assert report["bad"] == 1
    assert calls[0][0] == "python3"
    assert {e["class"] for e in report["rows"][0]["evidence"]} == {"standard", "system-3.9"}


@pytest.mark.parametrize("marker", ["py-standard.sh", "standard-python", "STANDARD_PYTHON"])
def test_standard_selector_reference_counts_as_standard(fleet, marker):
    job, script, measure, _, _ = fleet
    job("selector", ["/bin/bash", script(f'"{marker}" task.py\n')])
    assert measure()["rows"][0]["class"] == "standard"


def test_script_comments_and_heredoc_mentions_ignored(fleet):
    job, script, measure, calls, _ = fleet
    entry = script(
        "#!/bin/bash\n# /usr/bin/python3\ncat <<'TEXT'\n"
        "/usr/bin/python3\n/opt/anaconda3/bin/python\nSTANDARD_PYTHON\nTEXT\n"
        "cat <<-DOC\n\t/usr/bin/python3\n\tDOC\ntrue # /usr/bin/python3\n"
    )
    job("documentation", ["/bin/bash", entry])
    assert measure()["rows"][0]["class"] == "no-python"
    assert calls == []


def test_env_shebang_uses_job_path(fleet):
    job, script, measure, calls, _ = fleet
    entry = script("#!/usr/bin/env python3\nprint('hello')\n", "task.py")
    job("shebang", [entry])
    assert measure()["bad"] == 1
    assert calls[0][0] == "python3"


@pytest.mark.parametrize("options", [[], ["-o", "pipefail", "--"]])
def test_shell_entry_script_needs_no_filename_extension(fleet, options):
    job, script, measure, _, _ = fleet
    entry = script("/usr/bin/python3 -c pass\n", "runner")
    job("extensionless-shell", ["/bin/bash", *options, entry, "--config"])
    report = measure()
    assert report["status"] == "measured" and report["bad"] == 1


def test_direct_extensionless_python_script_uses_its_shebang(fleet):
    job, script, measure, calls, _ = fleet
    entry = script(
        '#!/usr/bin/env python3\n"""Don\'t parse this as shell."""\nprint(1)\n', "runner"
    )
    job("extensionless-python", [entry])
    report = measure()
    assert report["status"] == "measured" and report["bad"] == 1
    assert calls[0][0] == "python3"


def test_explicit_python_ignores_unused_entry_shebang(fleet):
    job, script, measure, _, _ = fleet
    entry = script("#!/usr/bin/python3\nprint('hello')\n", "task.py")
    job("explicit", ["/standard/python3", entry])
    assert measure()["rows"][0]["class"] == "standard"


def test_script_simple_path_assignment_changes_bare_resolution(fleet):
    job, script, measure, calls, directory = fleet
    entry = script('export PATH="$HOME/standard:$PATH"\nexec python3 task.py\n')
    job("script-path", ["/bin/bash", entry])
    assert measure()["bad"] == 0
    assert calls[0][1] == str(directory.parent / "standard") + ":" + audit.DEFAULT_PATH


def test_dynamic_script_path_is_unmeasured(fleet):
    job, script, measure, _, _ = fleet
    job("dynamic", ["/bin/bash", script('PATH="$UNKNOWN/bin:$PATH"\npython3 task.py')])
    assert "unmeasured: com.stranske.dynamic:" in audit.format_line(measure())


def test_offender_names_truncate_with_remainder(fleet):
    job, _, measure, _, _ = fleet
    for number in range(8):
        job(str(number), ["/usr/bin/python3", "-c", "pass"])
    line = audit.format_line(measure())
    assert "8/8 scheduled entrypoints" in line
    assert "com.stranske.4, +3 more" in line
    assert "com.stranske.5" not in line
    assert "drainable: 8" in line


def test_missing_directory_is_not_measured_zero(tmp_path):
    report = audit.audit(tmp_path / "missing")
    assert report["jobs"] is None and report["bad"] is None
    assert "unmeasured — would need LaunchAgents directory" in audit.format_line(report)
    assert "(drained)" not in audit.format_line(report)


def test_empty_existing_directory_is_unmeasured(fleet):
    report = fleet[2]()
    assert report["status"] == "unmeasured" and report["jobs"] == 0
    assert audit.format_line(report) == "Python standard: unmeasured — would need >=1 matching job"


def test_other_remains_drainable_after_legacy_jobs_fixed(fleet, monkeypatch, capsys):
    job, _, measure, _, _ = fleet
    for number in range(8):
        job(str(number), ["/usr/bin/python3", "-c", "pass"])
    other = job("other", ["/other/python3", "-c", "pass"])
    before = measure()
    assert before["bad"] == before["drainable"] == 9
    assert "9/9 scheduled entrypoints" in audit.format_line(before)
    for number in range(8):
        job(str(number), ["/standard/python3", "-c", "pass"])
    report = measure()
    assert report["bad"] == report["drainable"] == 1
    assert report["offenders"] == []
    line = audit.format_line(report)
    assert "1/9 scheduled entrypoints on unsupported Python (blocking)" in line
    assert f"other: 1 ({other})" in line
    assert "drainable: 1" in line
    with monkeypatch.context() as patch:
        patch.setattr(audit, "audit", lambda: report)
        assert audit.main([]) == 0
        assert capsys.readouterr().out.strip() == line
        assert audit.main(["--json"]) == 0
        assert json.loads(capsys.readouterr().out)["bad"] == 1
    job("other", ["/standard/python3", "-c", "pass"])
    drained = measure()
    assert drained["bad"] == drained["drainable"] == 0
    assert "(drained)" in audit.format_line(drained)
    assert "blocking" not in audit.format_line(drained)


@pytest.mark.parametrize("bad", [0, 1])
def test_unreadable_plist_preserves_known_offenders(fleet, monkeypatch, bad):
    job, _, measure, _, directory = fleet
    interpreter = "/usr/bin/python3" if bad else "/standard/python3"
    offender = job("known", [interpreter, "-c", "pass"])
    job("unreadable", ["/standard/python3", "-c", "pass"])
    original = audit.Path.read_bytes

    def read_bytes(path):
        if path == directory / "unreadable.plist":
            raise PermissionError("fixture unreadable plist")
        return original(path)

    monkeypatch.setattr(audit.Path, "read_bytes", read_bytes)
    report = measure()
    assert report["status"] == "unmeasured"
    assert report["bad"] == report["drainable"] == bad
    line = audit.format_line(report)
    assert f"{bad}/1 scheduled entrypoints" in line
    assert f"offenders: {offender if bad else 'none'}" in line
    assert f"drainable: {bad}" in line
    assert ("blocking" in line) == bool(bad)
    assert "unmeasured: readable plist unreadable.plist (PermissionError)" in line
    assert "(drained)" not in line


def test_resolver_failure_preserves_job_and_reports_unmeasured(fleet):
    job, _, _, _, directory = fleet
    job("failed", ["python3", "-c", "pass"])

    def failed(*_):
        raise subprocess.TimeoutExpired("resolver", 10)

    report = audit.audit(directory, resolver=failed)
    assert report["jobs"] == 1
    assert report["rows"][0]["class"] == "unmeasured"
    assert "com.stranske.failed" in audit.format_line(report)
    assert "(drained)" not in audit.format_line(report)


def test_unreadable_entry_is_unmeasured(fleet):
    job, _, measure, _, directory = fleet
    job("missing-script", ["/bin/bash", str(directory / "missing.sh")])
    assert measure()["status"] == "unmeasured"


def test_label_prefixes_are_injectable(fleet):
    job, _, measure, _, directory = fleet
    job("ignored", ["/usr/bin/python3"])
    (directory / "custom.plist").write_bytes(
        plistlib.dumps({"Label": "custom.job", "ProgramArguments": ["/standard/python3"]})
    )
    assert measure(prefixes=("custom.",))["jobs"] == 1
    assert measure(prefixes=("custom.",))["bad"] == 0


def test_malformed_plist_never_drains(fleet):
    _, _, measure, _, directory = fleet
    (directory / "broken.plist").write_text("broken")
    assert measure()["status"] == "unmeasured"


def test_default_resolver_controls_environment_and_reports_version(monkeypatch, tmp_path):
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        output = "/actual/python3\n" if argv[0] == "/bin/bash" else "Python 3.13.11\n"
        return subprocess.CompletedProcess(argv, 0, output, "")

    monkeypatch.setattr(audit.subprocess, "run", run)
    resolved = audit.resolve_python("python3", "/job/bin", True, tmp_path)
    assert resolved == audit.Resolution("/actual/python3", "Python 3.13.11")
    assert calls[0][0] == ["/bin/bash", "-lc", "command -v python3"]
    assert calls[0][1]["env"] == {"HOME": str(tmp_path), "PATH": "/job/bin"}
    assert calls[1][0] == ["/actual/python3", "--version"]


@pytest.mark.parametrize("shim_dir", ["python-bin", "bin"])
def test_shared_shim_resolves_actual_exec_target(monkeypatch, tmp_path, shim_dir):
    shim = tmp_path / ".codex" / shim_dir / "python3"
    shim.parent.mkdir(parents=True)
    shim.write_text('#!/bin/zsh\npy="$(standard-python)"\nexec "$py" "$@"\n')
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if argv[0] == "/bin/bash":
            output = str(shim)
        elif argv[0].endswith("standard-python"):
            output = "/standard/python3.14"
        else:
            output = "Python 3.14.3"
        return subprocess.CompletedProcess(argv, 0, output, "")

    monkeypatch.setattr(audit.subprocess, "run", run)
    resolved = audit.resolve_python("python3", str(shim.parent), False, tmp_path)
    assert audit.classify(resolved) == "standard"
    assert resolved.executable == "/standard/python3.14"
    assert calls[-1] == ["/standard/python3.14", "--version"]
    assert [str(tmp_path / ".codex/bin/standard-python")] in calls


def test_sourced_standard_helper_is_not_scanned_as_job_body(fleet):
    job, script, measure, _, _ = fleet
    entry = script("print('hello')", "driver.py")
    job(
        "inline",
        [
            "/bin/bash",
            "-lc",
            f'source "$HOME/.codex/bin/py-standard.sh" && "$STANDARD_PYTHON" {entry}',
        ],
    )
    assert measure()["rows"][0]["class"] == "standard"


def test_sweep_prints_snapshot_once_by_capacity_health(monkeypatch, fleet):
    job, _, measure, _, _ = fleet
    job("healthy", ["/standard/python3"])
    monkeypatch.setattr(switch_review, "experiment_producer_line", lambda: "producer")
    monkeypatch.setattr(switch_review, "comparative_routing_line", lambda _: "routing")
    report = {
        "review_days": 7,
        "raise_count": 0,
        "held_off": [],
        "on_but_idle": [],
        "unconditioned": [],
        "mirror_drift": {"status": "ok"},
        "weekly_pacing": "Capacity pacing: fixture",
        "python_standard": measure(),
    }
    text = switch_review.format_report(report)
    assert text.count("Python standard:") == 1
    assert text.index("Capacity pacing:") < text.index("Python standard:")
    assert "all 1 scheduled entrypoints resolve to 3.14/3.13 (drained)" in text


def test_cli_json_and_text_share_snapshot(monkeypatch, capsys, fleet):
    report = fleet[2]()
    monkeypatch.setattr(audit, "audit", lambda: report)
    assert audit.main(["--json"]) == 0
    assert json.loads(capsys.readouterr().out) == report
    assert audit.main([]) == 0
    assert capsys.readouterr().out.strip() == audit.format_line(report)


def test_multiline_quotes_continuations_and_substitutions(fleet):
    job, script, measure, calls, _ = fleet
    entry = script(
        "#!/bin/bash\nresult=$(python3 -c '\nprint(1)\n')\n"
        "exec /opt/anaconda3/bin/python3.12 \\\n task.py\n"
    )
    job("multiline", ["/bin/bash", entry])
    report = measure()
    assert report["status"] == "measured"
    assert report["bad"] == 1
    assert {c[0] for c in calls} == {"python3", "/opt/anaconda3/bin/python3.12"}


def test_home_reference_expands_before_interpreter_extraction(fleet):
    job, script, measure, calls, directory = fleet
    job("home", ["/bin/bash", script('exec "$HOME/.codex/bin/python3" task.py')])
    assert measure()["bad"] == 0
    assert calls[0][0] == str(directory.parent / ".codex/bin/python3")


def test_deep_non_python_paths_do_not_backtrack_or_become_interpreters(tmp_path):
    deep = "/" + "/".join(["directory"] * 60) + "/entry.sh"
    assert audit.references(f'exec "{deep}"', audit.DEFAULT_PATH, tmp_path) == []


def test_quoted_interpreter_path_preserves_spaces(fleet):
    job, script, measure, calls, directory = fleet
    interpreter = str(directory.parent / "My Environment/bin/python3")
    job("spaces", ["/bin/bash", script(f'exec "{interpreter}" task.py')])
    assert measure()["rows"][0]["class"] == "standard"
    assert calls[0][0] == interpreter


@pytest.mark.parametrize("interpreter", ["/standard/bin/python3", "/My Environment/bin/python3"])
def test_interpreter_assignment_keeps_only_executable_path(fleet, interpreter):
    job, script, measure, calls, _ = fleet
    job(
        "assignment",
        ["/bin/bash", script(f'PYTHON_BIN="{interpreter}"\n"$PYTHON_BIN" task.py')],
    )
    assert measure()["rows"][0]["class"] == "standard"
    assert calls[0][0] == interpreter


def test_here_string_is_not_a_heredoc(fleet):
    job, script, measure, _, _ = fleet
    job("here-string", ["/bin/bash", script('cat <<< "hello"\n/usr/bin/python3 task.py')])
    assert measure()["bad"] == 1


def test_invalid_python_source_is_unmeasured(fleet):
    job, script, measure, _, _ = fleet
    entry = script("if True:\n    pass\n  bad_indent\n", "invalid.py")
    job("invalid", ["/standard/python3", entry])
    assert measure()["status"] == "unmeasured"


def test_script_path_assignment_after_login_is_not_reset(fleet):
    job, script, measure, calls, _ = fleet
    entry = script("export PATH=/standard:/bin\nexec python3 task.py")
    job("login-path", ["/bin/bash", "-lc", entry])
    assert measure()["bad"] == 0
    assert calls[0][:3] == ("python3", "/standard:/bin", False)


def test_python_source_pin_and_comments(fleet):
    job, script, measure, _, _ = fleet
    entry = script(
        "# comment /usr/bin/python3\nimport subprocess\n"
        'subprocess.run(["/opt/anaconda3/bin/python3.12", "--version"])\n',
        "child.py",
    )
    job("child-pin", ["/standard/python3", entry])
    assert measure()["rows"][0]["class"] == "anaconda-3.12"


@pytest.mark.parametrize("output", ["", "Python unknown", "Python 3.14.3\nextra"])
def test_invalid_version_is_unmeasurable(monkeypatch, tmp_path, output):
    monkeypatch.setattr(
        audit.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 0, output, "")
    )
    with pytest.raises(ValueError, match="no Python version"):
        audit.resolve_python("/absolute/python3", audit.DEFAULT_PATH, False, tmp_path)


def test_default_lookup_failure_is_unmeasurable(monkeypatch, tmp_path):
    monkeypatch.setattr(
        audit.subprocess,
        "run",
        lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "missing"),
    )
    with pytest.raises(ValueError, match="exited 1"):
        audit.resolve_python("python3", audit.DEFAULT_PATH, False, tmp_path)


def test_weekly_cli_collects_audit_once_and_carries_json_and_text(
    monkeypatch, tmp_path, fleet, capsys
):
    import adversarial

    snapshot = fleet[2]()
    calls = []
    monkeypatch.setenv("ORCH_VALUE_CHAIN_MONITOR", "0")
    monkeypatch.setenv("ORCH_DISABLE_STEPS", "issue-size-quality")
    monkeypatch.setattr(switch_review, "credential_exposure", lambda: {})
    monkeypatch.setattr(adversarial, "record_shape_measurement", lambda *a, **kw: False)

    def collect():
        calls.append("audit")
        return snapshot

    def review(**kwargs):
        assert kwargs["python_standard_report"] is snapshot
        return {"python_standard": kwargs["python_standard_report"]}

    monkeypatch.setattr(audit, "audit", collect)
    monkeypatch.setattr(switch_review, "review", review)
    monkeypatch.setattr(
        switch_review, "format_report", lambda r: audit.format_line(r["python_standard"])
    )
    destination = tmp_path / "weekly.txt"
    assert switch_review.main(["--env", "process", "--json", "--text-out", str(destination)]) == 0
    assert json.loads(capsys.readouterr().out)["python_standard"] == snapshot
    assert destination.read_text() == audit.format_line(snapshot)
    assert calls == ["audit"]


def test_spaced_heredoc_ignores_legacy_prose_and_apostrophes(fleet):
    job, script, measure, calls, _ = fleet
    entry = script("cat << EOF\nDon't execute /usr/bin/python3\nEOF\ntrue\n")
    job("spaced-heredoc", ["/bin/bash", entry])
    report = measure()
    assert report["status"] == "measured"
    assert report["rows"][0]["class"] == "no-python"
    assert calls == []


@pytest.mark.parametrize(
    ("word", "delimiter"),
    [
        ("'E.O.F'", "E.O.F"),
        ('"END TEXT"', "END TEXT"),
        ("E\\ OF", "E OF"),
        ("$END", "$END"),
        ("''", ""),
        ("'E''OF'", "EOF"),
    ],
)
def test_quoted_and_escaped_heredoc_words_cannot_mask_bare_python(fleet, word, delimiter):
    job, script, measure, _, _ = fleet
    entry = script(f"cat <<{word}\nSTANDARD_PYTHON\n{delimiter}\npython3 -c pass\n")
    job("quoted-heredoc", ["/bin/bash", entry])
    report = measure()
    assert report["status"] == "measured" and report["bad"] == 1
    assert report["rows"][0]["class"] == "system-3.9"


def test_multiple_heredoc_bodies_are_both_ignored(fleet):
    job, script, measure, _, _ = fleet
    entry = script(
        "cat <<ONE <<'T.W.O'\nSTANDARD_PYTHON\nONE\n" "/usr/bin/python3\nT.W.O\npython3 -c pass\n"
    )
    job("multiple-heredocs", ["/bin/bash", entry])
    assert measure()["bad"] == 1


@pytest.mark.parametrize("line", ['echo "<< EOF"', 'echo "$(( 1 << 2 ))"', "true;# << EOF"])
def test_quoted_operators_arithmetic_and_comments_do_not_open_heredocs(fleet, line):
    job, script, measure, _, _ = fleet
    job("not-heredoc", ["/bin/bash", script(line + "\n/usr/bin/python3 task.py")])
    assert measure()["bad"] == 1


@pytest.mark.parametrize("inline", [False, True])
def test_bare_versioned_python_cannot_print_drained(fleet, inline):
    job, script, measure, calls, _ = fleet
    task = script("print('hello')\n", "task.py")
    body = f"python3.9 {task}"
    args = ["/bin/bash", "-c", body] if inline else ["/bin/bash", script(body)]
    job("versioned", args)
    report = measure()
    assert report["bad"] == 1
    assert report["rows"][0]["class"] == "system-3.9"
    assert calls[0][0] == "python3.9"
    assert "(drained)" not in audit.format_line(report)


@pytest.mark.parametrize("argument", ["--check", "--config"])
def test_script_options_cannot_hide_the_entry_script(fleet, argument):
    job, script, measure, _, _ = fleet
    entry = script("/usr/bin/python3 task.py")
    job("script-option", ["/bin/bash", entry, argument])
    assert measure()["bad"] == 1


@pytest.mark.parametrize("options", [["-c"], ["-o", "pipefail", "-c"], ["-lc"]])
def test_shell_command_precedes_arg0(fleet, options):
    job, _, measure, calls, _ = fleet
    job("arg0", ["/bin/bash", *options, "/usr/bin/python3 -c pass", "arg0"])
    assert measure()["bad"] == 1
    assert calls[0][0] == "/usr/bin/python3"
    assert calls[0][2] == (options == ["-lc"])


def test_truncated_xml_plist_is_unmeasured_and_retains_other_jobs(fleet):
    job, _, measure, _, directory = fleet
    job("valid", ["/standard/python3"])
    (directory / "truncated.plist").write_bytes(b'<?xml version="1.0"?><plist><dict>')
    report = measure()
    assert report["status"] == "unmeasured"
    assert report["jobs"] == 1
    assert "ExpatError" in audit.format_line(report)
    assert "(drained)" not in audit.format_line(report)


def test_unexpected_audit_failure_keeps_cli_unmeasured(monkeypatch, capsys):
    def failed():
        raise RuntimeError("future reader failed")

    monkeypatch.setattr(audit, "audit", failed)
    assert audit.main([]) == 0
    line = capsys.readouterr().out
    assert "unmeasured — would need" in line and "RuntimeError" in line
    assert "(drained)" not in line
