"""Fixture-only scheduled interpreter regressions; no launchd or production scripts run."""

from __future__ import annotations

import json
import plistlib
import subprocess

import pytest

import python_standard_audit as audit
import switch_review


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


@pytest.mark.parametrize(
    ("interpreter", "category", "bad"),
    [
        ("/usr/bin/python3", "system-3.9", 1),
        ("/opt/anaconda3/bin/python3", "anaconda-3.12", 1),
        ("/opt/anaconda3/bin/python", "anaconda-3.12", 1),
        ("/standard/python3", "standard", 0),
        ("/other/python3", "other", 0),
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
    assert "unmeasured — would need" in audit.format_line(measure())


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


def test_empty_existing_directory_is_measured_zero(fleet):
    report = fleet[2]()
    assert report["status"] == "measured" and report["jobs"] == 0
    assert (
        audit.format_line(report)
        == "Python standard: all 0 scheduled entrypoints resolve to 3.14/3.13 (drained)"
    )


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
