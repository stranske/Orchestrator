"""Normal Mac name settings and local-only credential reset semantics."""

import subprocess
from pathlib import Path

import pytest

import closer_coding_publication as pub


def test_normal_mac_filename_settings_are_names_not_executable_configuration():
    for name in ["core.ignorecase", "core.precomposeunicode"]:
        raw = b"local\0file:/synthetic/config\0" + name.encode() + b"\0"
        assert pub._configuration_names(raw, 1000) == [("local", "/synthetic/config", name)]
    for name in [
        "core.hookspath",
        "core.fsmonitor",
        "filter.foo.smudge",
        "include.path",
    ]:
        with pytest.raises(pub.PublicationHeld):
            pub._configuration_names(
                b"local\0file:/synthetic/config\0" + name.encode() + b"\0", 1000
            )
    with pytest.raises(pub.PublicationHeld):
        pub._configuration_names(b"unknown\0file:/synthetic/config\0core.ignorecase\0", 1000)


def helper_env(tmp_path):
    return {"GH_CONFIG_DIR": str(tmp_path), "GH_TOKEN": "SYNTHETIC_ONLY"}


def test_only_one_fixed_helper_after_last_explicit_reset_is_accepted(tmp_path):
    env = helper_env(tmp_path)
    fixed = b"!gh auth git-credential"
    for prefix in [b"osxkeychain\0\0", b"cleared\0\0discarded-again\0\0"]:
        result = pub._fixed_helper_route(prefix + fixed + b"\0", "/synthetic/gh", env)
        assert result["native_principal_proven"] is False
        assert "cleared" not in repr(result) and "osxkeychain" not in repr(result)
    for raw in [
        b"osxkeychain\0" + fixed + b"\0",
        b"\0" + fixed + b"\0extra\0",
        fixed + b"\0\0",
    ]:
        with pytest.raises(pub.PublicationHeld):
            pub._fixed_helper_route(raw, "/synthetic/gh", env)


def test_actual_local_git_empty_reset_never_runs_cleared_helper(tmp_path):
    """Only synthetic credentials/scripts in new temp repo; no network/accounts."""
    binary = Path("/opt/homebrew/Cellar/git/2.49.0/bin/git")
    if not binary.is_file():
        binary = Path("/usr/bin/git")
    repo = tmp_path / "repo"
    repo.mkdir()
    cleared, selected = tmp_path / "CLEARED_MUST_NOT_EXECUTE", tmp_path / "selected-ran"
    helper = tmp_path / "gh"
    helper.write_text(
        "#!/bin/sh\ncat >/dev/null\nprintf selected > "
        + str(selected)
        + '\nprintf "username=synthetic\\npassword=synthetic\\n"\n'
    )
    helper.chmod(0o700)
    system = tmp_path / "synthetic-system-config"
    system.write_text("[credential]\n\thelper = !printf BAD > " + str(cleared) + "\n")
    env = {
        "HOME": str(tmp_path),
        "PATH": "/usr/bin:/bin",
        "GIT_CONFIG_SYSTEM": str(system),
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
    }

    def git(*args, input=None):
        return subprocess.run(
            [str(binary), "-C", str(repo), *args],
            input=input,
            env=env,
            capture_output=True,
            check=True,
            timeout=5,
        ).stdout

    git("init")
    git("config", "--add", "credential.https://github.com.helper", "!printf BAD > " + str(cleared))
    git("config", "--add", "credential.helper", "")
    git(
        "config",
        "--add",
        "credential.helper",
        "!" + str(helper) + " auth git-credential",
    )
    raw = git("config", "--null", "--get-all", "credential.helper")
    pub._fixed_helper_route(raw, str(helper), helper_env(tmp_path))
    result = git("credential", "fill", input=b"protocol=https\nhost=github.com\n\n")
    assert b"username=synthetic" in result and b"password=synthetic" in result
    assert selected.read_text() == "selected" and not cleared.exists()


@pytest.mark.parametrize("mode", ["home", "xdg", "explicit"])
def test_existing_stored_auth_route_needs_no_token_read_or_fake_principal(tmp_path, mode):
    env = {"HOME": str(tmp_path)}
    expected = tmp_path / ".config/gh"
    if mode == "xdg":
        env["XDG_CONFIG_HOME"] = str(tmp_path / "xdg")
        expected = tmp_path / "xdg/gh"
    elif mode == "explicit":
        env["GH_CONFIG_DIR"] = str(tmp_path / "explicit")
        expected = tmp_path / "explicit"
    result = pub._fixed_helper_route(b"\0!gh auth git-credential\0", "/synthetic/gh", env)
    assert result["config_directory"] == str(expected)
    assert result["credential_slot"] == "stored_github_cli"
    assert result["native_principal_proven"] is False


@pytest.mark.parametrize(
    "update",
    [
        {"GH_HOST": "foreign.invalid"},
        {"GH_ENTERPRISE_TOKEN": "synthetic"},
        {"GH_PATH": "/foreign/gh"},
        {"XDG_CONFIG_HOME": "relative"},
        {"GH_TOKEN": "one", "GITHUB_TOKEN": "two"},
        {"GH_DEBUG": "api"},
    ],
)
def test_foreign_or_ambiguous_stored_auth_environment_holds(tmp_path, update):
    with pytest.raises(pub.PublicationHeld):
        pub._fixed_helper_route(
            b"\0!gh auth git-credential\0", "/synthetic/gh", {"HOME": str(tmp_path), **update}
        )


def test_old_private_stage_without_fixed_helper_policy_is_not_adopted(tmp_path):
    with pytest.raises(pub.PublicationHeld, match="source-selected helper policy"):
        pub._StagingData(tmp_path, {"schema": "closer-publication-staging-intent-data/v1"})
