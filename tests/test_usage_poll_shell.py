"""Hermetic bash entrypoint checks: fake interpreters and fake headless credentials."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts/usage_poll"


@pytest.fixture
def shell_home(tmp_path):
    home = tmp_path / "home"
    installed = home / ".codex/bin"
    installed.mkdir(parents=True)
    # Redirect only absolute Homebrew install roots so host interpreters cannot
    # affect refusal/backup tests. Candidate ordering and version checks are unchanged.
    helper = (SCRIPTS / "py-standard.sh").read_text()
    for root in ("/opt/homebrew/bin", "/usr/local/bin"):
        helper = helper.replace(root, str(tmp_path / root.lstrip("/")))
    (installed / "py-standard.sh").write_text(helper)
    shutil.copyfile(SCRIPTS / "usage-poll.sh", installed / "usage-poll.sh")
    return home, installed


def interpreter(path, version, body="exit 0\n"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '#!/bin/bash\nif [[ "$1" == -c ]]; then\n'
        f"  printf '%s\\n' '{version}'\n  exit 0\nfi\n" + body
    )
    path.chmod(0o755)
    return path


def uv_python(home, version):
    return home / f".local/share/uv/python/cpython-{version}-fixture/bin/python{version}"


def run_shell(home, script, **extra):
    # No inherited OAuth or interpreter overrides, even on an authenticated machine.
    return subprocess.run(
        ["/bin/bash", "-c", script],
        env={"HOME": str(home), "PATH": str(home / "path"), **extra},
        text=True,
        capture_output=True,
        timeout=10,
    )


def selected(home):
    return 'source "$HOME/.codex/bin/py-standard.sh" && printf "%s\\n" "$STANDARD_PYTHON"'


@pytest.mark.parametrize("primary_root", ["uv", "homebrew", "local"])
def test_standard_prefers_314_over_313(shell_home, tmp_path, primary_root):
    home, _ = shell_home
    interpreter(uv_python(home, "3.13"), "3.13")
    roots = {
        "uv": uv_python(home, "3.14"),
        "homebrew": tmp_path / "opt/homebrew/bin/python3.14",
        "local": tmp_path / "usr/local/bin/python3.14",
    }
    expected = interpreter(roots[primary_root], "3.14")
    result = run_shell(home, selected(home))
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(expected)


def test_standard_uses_313_backup(shell_home):
    home, _ = shell_home
    expected = interpreter(uv_python(home, "3.13"), "3.13")
    result = run_shell(home, selected(home))
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(expected)


@pytest.mark.parametrize("override_version", ["3.9", "3.12"])
def test_standard_ignores_unsupported_override(shell_home, override_version):
    home, _ = shell_home
    override = interpreter(home / "path/python3", override_version)
    expected = interpreter(uv_python(home, "3.14"), "3.14")
    result = run_shell(home, selected(home), ORCH_STANDARD_PYTHON=str(override))
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(expected)


@pytest.mark.parametrize("version", ["3.13", "3.14"])
def test_standard_honors_supported_override(shell_home, version):
    home, _ = shell_home
    interpreter(uv_python(home, "3.14"), "3.14")
    override = interpreter(home / "override/python3", version)
    result = run_shell(home, selected(home), ORCH_STANDARD_PYTHON=str(override))
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(override)


def test_standard_refuses_system_and_anaconda_versions(shell_home):
    home, _ = shell_home
    override = interpreter(home / "path/python3", "3.9")
    interpreter(home / "path/python3.12", "3.12")
    # Executability and a supported filename do not substitute for the version check.
    interpreter(uv_python(home, "3.14"), "3.12")
    interpreter(uv_python(home, "3.13"), "3.9")
    result = run_shell(home, selected(home), ORCH_STANDARD_PYTHON=str(override))
    assert result.returncode != 0
    assert "need Python 3.14 (backup 3.13)" in result.stderr
    assert "refusing system/anaconda fallback" in result.stderr
    assert not result.stdout


@pytest.mark.parametrize("token_present", [False, True])
@pytest.mark.parametrize("codex_rc", [0, 1])
@pytest.mark.parametrize("inherited_token", [False, True])
def test_token_is_claude_only_and_never_logged(
    shell_home, token_present, codex_rc, inherited_token
):
    home, installed = shell_home
    # The fake interpreter records presence only, never the value of the fake token.
    body = """name="${1##*/}"
if [[ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]]; then present=true; else present=false; fi
printf '{"name":"%s","token":%s}\\n' "$name" "$present" >> "$HOME/observed.jsonl"
if [[ "$name" == codex-usage-poll.py ]]; then exit "$CODEX_RC"; fi
exit 0
"""
    interpreter(uv_python(home, "3.14"), "3.14", body)
    # BSD-compatible date is the only external utility used by the wrapper.
    (home / "path").mkdir()
    (home / "path/date").symlink_to("/bin/date")
    sentinel = "FAKE_ONLY_TOKEN_MUST_NOT_APPEAR"
    if token_present:
        token_file = home / ".codex/handoff/.claude-oauth-token"
        token_file.parent.mkdir(parents=True)
        token_file.write_text(
            f"export CLAUDE_CODE_OAUTH_TOKEN='{sentinel}'\n"
            'printf "%s\\n" "$CLAUDE_CODE_OAUTH_TOKEN"\n'
            'printf "%s\\n" "$CLAUDE_CODE_OAUTH_TOKEN" >&2\n'
        )
    extra = {"CLAUDE_CODE_OAUTH_TOKEN": sentinel} if inherited_token else {}
    result = run_shell(
        home, f'/bin/bash "{installed}/usage-poll.sh"', CODEX_RC=str(codex_rc), **extra
    )
    assert result.returncode == codex_rc, result.stderr
    rows = [json.loads(line) for line in (home / "observed.jsonl").read_text().splitlines()]
    assert rows == [
        {"name": "codex-usage-poll.py", "token": False},
        {"name": "claude-usage-poll.py", "token": token_present},
        {"name": "usage_alerts.py", "token": False},
    ]
    assert sentinel not in result.stdout + result.stderr
    assert f"codex rc={codex_rc}" in result.stdout
    assert "claude rc=0" in result.stdout


@pytest.mark.parametrize("version", ["3.9", "3.12", "3.13", "3.14", "missing"])
def test_wrapper_validates_legacy_interpreter_override(shell_home, version):
    home, installed = shell_home
    interpreter(uv_python(home, "3.14"), "3.14", 'printf "standard-used\\n"\n')
    override = home / "legacy-python"
    if version != "missing":
        interpreter(override, version, 'printf "override-used\\n"\n')
    (home / "path").mkdir()
    (home / "path/date").symlink_to("/bin/date")
    result = run_shell(
        home, f'/bin/bash "{installed}/usage-poll.sh"', ORCH_USAGE_POLL_PYTHON=str(override)
    )
    assert result.returncode == 0, result.stderr
    supported = version in ("3.13", "3.14")
    assert ("override-used" in result.stdout) is supported
    assert ("standard-used" in result.stdout) is not supported
    assert ("ignoring unsupported ORCH_USAGE_POLL_PYTHON" in result.stderr) is not supported


def test_wrapper_stops_when_standard_is_unavailable(shell_home):
    home, installed = shell_home
    result = run_shell(home, f'/bin/bash "{installed}/usage-poll.sh"')
    assert result.returncode != 0
    assert "standard python unavailable" in result.stderr
    assert not result.stdout
