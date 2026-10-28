"""Actual local Git causal controls; no API/helper/account/provider operations."""

import copy
import errno
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

import closer_coding_publication as pub
import provision
import worktree_git


@pytest.fixture
def context(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    original = root / "original-home"
    original.mkdir(mode=0o700)
    (original / ".config").mkdir(mode=0o700)
    auth = original / ".config/gh"
    auth.mkdir(mode=0o700)
    # Explicitly synthetic private account file; never read by source inspector.
    (auth / "hosts.yml").write_text("synthetic: no-token\n")
    (auth / "hosts.yml").chmod(0o600)
    (original / ".gitconfig").write_text('[filter "lfs"]\n\tprocess = /must-not-execute\n')
    private = root / "private-git-home"
    private.mkdir(mode=0o700)
    (private / ".config").mkdir(mode=0o700)
    (private / ".config/git").mkdir(mode=0o700)
    config = private / ".gitconfig"
    config.write_text("[user]\n\tname = Synthetic Owner\n\temail = owner@invalid.test\n")
    config.chmod(0o600)
    for name in list(os.environ):
        if name.startswith(("GIT_", "DYLD_", "LD_")) or name in {
            "GH_CONFIG_DIR",
            "XDG_CONFIG_HOME",
            "GH_TOKEN",
            "GITHUB_TOKEN",
            "BASH_ENV",
            "ENV",
            "SHELLOPTS",
            "BASHOPTS",
            "CDPATH",
            "IFS",
        }:
            monkeypatch.delenv(name)
    monkeypatch.setenv("HOME", str(original))
    monkeypatch.setenv(
        "PATH",
        "/opt/homebrew/Cellar/git/2.49.0/bin:/opt/homebrew/Cellar/gh/2.94.0/bin:/usr/bin:/bin",
    )
    git = Path(shutil.which("git")).resolve()
    gh = Path(shutil.which("gh")).resolve()
    assert git.is_file() and gh.is_file()
    repos, worktrees = root / "repos", root / "worktrees"
    repos.mkdir(mode=0o700)
    worktrees.mkdir(mode=0o700)
    monkeypatch.setattr(provision, "REPOS_DIR", repos)
    monkeypatch.setattr(provision, "WORKTREES_DIR", worktrees)
    policy = {
        "schema": pub.ANCHOR_POLICY_SCHEMA,
        "mode": "owned_existing_pr",
        "consumer_id": "synthetic-owner",
        "git_executable": {
            "path": str(git),
            "sha256": hashlib.sha256(git.read_bytes()).hexdigest(),
        },
        "allowed_path_prefixes": ["a.py"],
        "max_files": 2,
        "max_file_bytes": 65536,
        "max_total_bytes": 131072,
        "max_ack_bytes": 65536,
        "max_push_submissions": 1,
        "request_timeout_seconds": 120,
        "max_native_io_seconds": 15,
        "baseline_limits": {
            "schema": "closer-publication-baseline-limits/v1",
            "max_files": 4096,
            "max_file_bytes": 80 * 1048576,
            "max_total_bytes": 128 * 1048576,
            "max_inventory_bytes": 1048576,
        },
        "git_environment_v1": {
            "schema": "closer-publication-private-git-environment/v1",
            "home": str(private),
            "config_sha256": hashlib.sha256(config.read_bytes()).hexdigest(),
        },
    }
    env = pub.private_git_environment_data(policy)
    repo = provision.canonical_path("acme/repo")
    subprocess.run([str(git), "init", str(repo)], env=env, capture_output=True, check=True)
    (repo / "a.py").write_text("before\n")
    for args in [["add", "a.py"], ["-c", "commit.gpgsign=false", "commit", "-m", "original"]]:
        subprocess.run([str(git), "-C", str(repo), *args], env=env, capture_output=True, check=True)
    head = (
        subprocess.check_output([str(git), "-C", str(repo), "rev-parse", "HEAD"], env=env)
        .decode()
        .strip()
    )
    return {
        "policy": policy,
        "private": private,
        "original": original,
        "config": config,
        "git": git,
        "repo": repo,
        "head": head,
        "env": env,
    }


def test_private_environment_ignores_executable_global_but_retains_original_auth(context):
    before = dict(os.environ)
    env = pub.private_git_environment_data(context["policy"])
    assert dict(os.environ) == before
    assert env["HOME"] == str(context["private"]) != before["HOME"]
    assert env["GH_CONFIG_DIR"] == str(context["original"] / ".config/gh")
    assert "GH_TOKEN" not in env and "GITHUB_TOKEN" not in env
    raw = subprocess.check_output(
        [str(context["git"]), "-C", str(context["repo"]), *pub._read_command("config_names")],
        env=env,
    )
    names = pub._configuration_names(raw, 65536)
    assert not any(key.startswith("filter.") for _, _, key in names)
    assert context["original"].joinpath(".gitconfig").read_text().startswith('[filter "lfs"]')


@pytest.mark.parametrize(
    "fault", ["hash", "extra", "redirect", "environment", "system_environment"]
)
def test_private_context_tamper_refuses_before_child(context, monkeypatch, fault):
    policy = copy.deepcopy(context["policy"])
    if fault == "hash":
        policy["git_environment_v1"]["config_sha256"] = "a" * 64
    elif fault == "extra":
        (context["private"] / "extra").write_text("unexpected")
    elif fault == "redirect":
        policy["git_environment_v1"]["home"] = str(context["private"]) + "/.."
    elif fault == "environment":
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/malicious")
    else:
        monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/malicious")
    with pytest.raises(pub.PublicationHeld):
        pub.private_git_environment_data(policy)


def test_actual_create_only_named_worktree_and_inspection_have_no_native_authority(context):
    prepared = provision.provision_absent_owned_head(
        "acme/repo#989",
        context["head"],
        "codex/synthetic-existing-pr",
        git_policy=context["policy"],
    )
    assert worktree_git.is_provisioned(prepared["worktree"])
    assert worktree_git.expected_pins(prepared["worktree"], "acme/repo")
    assert prepared["runtime_admission"] is prepared["native_ownership_proven"] is False
    before = dict(os.environ)
    facts = pub.inspect_git_context_data(prepared["worktree"], "acme/repo", context["policy"])
    assert facts["runtime_authority"] is facts["native_principal_proven"] is False
    policy = {**context["policy"], "trusted_context_sha256": facts["trusted_context_sha256"]}
    assert pub.validate_policy(policy) == policy
    assert dict(os.environ) == before
    assert facts["observed"]["helper_route"]["config_directory"] == str(
        context["original"] / ".config/gh"
    )
    assert all(row["sha256"] is None for row in facts["observed"]["auth_metadata"])
    with pytest.raises(RuntimeError, match="worktree must be absent"):
        provision.provision_absent_owned_head(
            "acme/repo#989",
            context["head"],
            "codex/synthetic-existing-pr",
            git_policy=context["policy"],
        )


@pytest.mark.parametrize(
    "fault,error_type,reason",
    [
        ("branch", RuntimeError, "^owned local branch must be absent; never reset or adopt$"),
        ("head", subprocess.CalledProcessError, None),
        ("target", ValueError, "^exact lowercase owned PR target/head required$"),
    ],
)
def test_absent_owned_provision_never_resets_or_adopts_existing_branch(
    context, fault, error_type, reason
):
    git, repo, env = context["git"], context["repo"], context["env"]
    target, head, branch = "acme/repo#989", context["head"], "codex/synthetic-existing-pr"
    if fault == "branch":
        subprocess.run([str(git), "-C", str(repo), "branch", branch, head], env=env, check=True)
    elif fault == "head":
        head = "a" * 40
    else:
        target = "acme/repo"  # A repository-only request cannot invent an owned PR.
    before = subprocess.check_output([str(git), "-C", str(repo), "show-ref"], env=env)
    with pytest.raises(error_type, match=reason) as held:
        provision.provision_absent_owned_head(target, head, branch, git_policy=context["policy"])
    assert held.value.__cause__ is None
    if fault == "head":
        assert held.value.returncode == 128
        assert held.value.cmd == [str(git), "-C", str(repo), "cat-file", "-e", head + "^{commit}"]
    assert subprocess.check_output([str(git), "-C", str(repo), "show-ref"], env=env) == before
    assert not provision.worktree_path("acme/repo#989", "closer").exists()


def test_unknown_config_and_hooks_remain_refused(context):
    prepared = provision.provision_absent_owned_head(
        "acme/repo#989",
        context["head"],
        "codex/synthetic-existing-pr",
        git_policy=context["policy"],
    )
    hook = context["repo"] / ".git/hooks/pre-push"
    hook.write_text("#!/bin/sh\nexit 0\n")
    hook.chmod(0o700)
    with pytest.raises(pub.PublicationHeld, match="active hooks"):
        pub.inspect_git_context_data(prepared["worktree"], "acme/repo", context["policy"])
    hook.unlink()
    context["config"].write_text('[filter "lfs"]\n\tprocess = /must-not-execute\n')
    context["policy"]["git_environment_v1"]["config_sha256"] = hashlib.sha256(
        context["config"].read_bytes()
    ).hexdigest()
    with pytest.raises(pub.PublicationHeld, match="unsupported config provenance"):
        pub.inspect_git_context_data(prepared["worktree"], "acme/repo", context["policy"])


@pytest.mark.parametrize(
    "fault,error_type,reason",
    [
        (
            "hook",
            RuntimeError,
            "^active canonical hooks require source closure before local provisioning$",
        ),
        (
            "executable_config",
            pub.PublicationHeld,
            "^unsupported config provenance or executable setting$",
        ),
    ],
)
def test_create_only_provision_refuses_controls_before_checkout(context, fault, error_type, reason):
    repo, git, env = context["repo"], context["git"], context["env"]
    if fault == "hook":
        hook = repo / ".git/hooks/post-checkout"
        hook.write_text("#!/bin/sh\nexit 42\n")
        hook.chmod(0o700)
    else:
        with context["config"].open("a") as handle:
            handle.write("[core]\n\thooksPath = /must-not-execute\n")
        context["policy"]["git_environment_v1"]["config_sha256"] = hashlib.sha256(
            context["config"].read_bytes()
        ).hexdigest()
        env = pub.private_git_environment_data(context["policy"])
    before = subprocess.check_output([str(git), "-C", str(repo), "show-ref"], env=env)
    with pytest.raises(error_type, match=reason) as held:
        provision.provision_absent_owned_head(
            "acme/repo#989",
            context["head"],
            "codex/synthetic-existing-pr",
            git_policy=context["policy"],
        )
    assert held.value.__cause__ is None
    assert subprocess.check_output([str(git), "-C", str(repo), "show-ref"], env=env) == before
    assert not provision.worktree_path("acme/repo#989", "closer").exists()


def test_cleanup_failure_does_not_leave_other_original_files_open(tmp_path, monkeypatch):
    files = []
    for name in ["first", "second"]:
        path = tmp_path / name
        path.write_text("control")
        files.append(pub._PhysicalFile(path.resolve(), 100))
    real_close = files[1].close

    def failed_close():
        real_close()
        raise OSError("synthetic close outcome")

    monkeypatch.setattr(files[1], "close", failed_close)
    with pytest.raises(pub.PublicationHeld, match="cleanup unknown"):
        pub._close_physical_controls(files)
    assert all(item.fd is None for item in files)


def test_system_lfs_configuration_is_causally_excluded_not_allowlisted(
    context, tmp_path, record_property
):
    """Use this platform's actual Git (hosted runner2.55, localMac2.49)."""
    git = str(context["git"])
    version = subprocess.check_output([git, "--version"]).decode().strip()
    record_property("actual_git_version", version)
    native_shell = subprocess.check_output([git, "var", "GIT_SHELL_PATH"]).decode().strip()
    known_shells = {
        "/bin/sh",
        "/usr/bin/sh",
        "/bin/bash",
        "/usr/bin/bash",
        "/bin/dash",
        "/usr/bin/dash",
    }
    record_property(
        "actual_git_native_shell_path",
        native_shell if native_shell in known_shells else "unclassified",
    )
    canonical_shell = str(Path(native_shell).resolve(strict=True))
    record_property(
        "actual_git_canonical_shell_path",
        canonical_shell if canonical_shell in known_shells else "unclassified",
    )
    before = dict(os.environ)
    system = tmp_path / "synthetic-system-gitconfig"
    system.write_text(
        '[filter "lfs"]\n\tclean = /must-not-execute\n\tsmudge = /must-not-execute\n\tprocess = /must-not-execute\n\trequired = true\n'
    )
    child = pub.private_git_environment_data(context["policy"])
    inherited_system = {**child, "GIT_CONFIG_SYSTEM": str(system)}
    command = [git, "-C", str(context["repo"]), *pub._read_command("config_names")]
    raw = subprocess.check_output(command, env=inherited_system)
    parts = raw.rstrip(b"\0").decode().split("\0")
    system_rows = [tuple(parts[i : i + 3]) for i in range(0, len(parts), 3) if parts[i] == "system"]
    assert any(
        row == ("system", "file:" + str(system), "filter.lfs.process") for row in system_rows
    )
    record_property("controlled_raw_system_name_inventory", json.dumps(system_rows))
    with pytest.raises(pub.PublicationHeld, match="unsupported config provenance"):
        pub._configuration_names(raw, 65536)
    repaired_raw = subprocess.check_output(command, env=child)
    repaired = pub._configuration_names(repaired_raw, 65536)
    assert not any(key.startswith("filter.") for _, _, key in repaired)
    assert child["GIT_CONFIG_SYSTEM"] == child["GIT_CONFIG_GLOBAL"] == str(context["config"])
    assert dict(os.environ) == before
    # Safe diagnostic for actual CI: finite known names only, never values or
    # arbitrary unknown section/key strings that could contain private data.
    ambient = subprocess.run(
        [git, "config", "--system", "--null", "--name-only", "--list"],
        env=before,
        capture_output=True,
        timeout=10,
    )
    keys = ambient.stdout.rstrip(b"\0").decode().split("\0") if ambient.returncode == 0 else []
    known = {"filter.lfs.clean", "filter.lfs.smudge", "filter.lfs.process", "filter.lfs.required"}
    record_property(
        "actual_ambient_system_lfs_names", json.dumps([key for key in keys if key in known])
    )
    record_property(
        "actual_ambient_system_other_names_count", sum(key not in known for key in keys)
    )


def test_runtime_git_reads_use_the_same_pinned_system_and_global_context(context):
    import time
    from types import SimpleNamespace

    observed = SimpleNamespace(
        policy=context["policy"], operation={"deadline": time.time() + 15}, check=lambda: None
    )
    # DATA transport exercise only, never an original Root/operation permit.
    for kind in ("system_paths", "global_paths"):
        raw = pub._fixed_git_read(observed, context["repo"], kind)
        assert raw.decode().strip() == str(context["config"])


@pytest.mark.parametrize("fault", ["missing_home", "unreadable_config"])
def test_private_filesystem_errors_are_controlled_holds_with_cleanup(context, monkeypatch, fault):
    opened = set()
    actual_open = os.open

    def observe_open(path, flags, *args, **kwargs):
        if fault == "unreadable_config" and Path(path) == context["config"]:
            raise PermissionError("synthetic sensitive path must not appear in hold")
        fd = actual_open(path, flags, *args, **kwargs)
        opened.add(fd)
        return fd

    if fault == "missing_home":
        context["private"].rename(context["private"].with_name("moved-private-git-home"))
    monkeypatch.setattr(pub.os, "open", observe_open)
    caller_held = []
    with pytest.raises(pub.PublicationHeld, match="^private Git environment unavailable$") as held:
        pub.private_git_environment_data(context["policy"], held=caller_held)
    assert held.value.__cause__ is None and held.value.__suppress_context__ is True
    assert str(context["private"]) not in str(held.value)
    assert "synthetic sensitive" not in str(held.value)
    assert caller_held == []
    for fd in opened:
        with pytest.raises(OSError) as closed:
            os.fstat(fd)
        assert closed.value.errno == errno.EBADF


@pytest.mark.parametrize("tamper", [None, "file_alias", "parent_alias", "foreign_target"])
def test_native_shell_route_pins_canonical_file_and_every_alias(tmp_path, tamper):
    root = tmp_path.resolve()
    actual = root / "usr/bin"
    actual.mkdir(parents=True)
    (actual / "dash").write_bytes(b"synthetic native shell payload")
    (actual / "other").write_bytes(b"foreign payload")
    parent = root / "bin"
    parent.symlink_to("usr/bin", target_is_directory=True)
    shell = parent / "sh"
    shell.symlink_to("other" if tamper == "foreign_target" else "dash")
    held = []
    try:
        if tamper == "foreign_target":
            with pytest.raises(
                pub.PublicationHeld, match="^native shell alias target unsupported$"
            ):
                pub._pin_native_git_shell(shell, held)
            return
        physical, aliases = pub._pin_native_git_shell(shell, held)
        assert physical.path == actual / "dash"
        assert physical.sha256 == hashlib.sha256(b"synthetic native shell payload").hexdigest()
        assert [row["path"] for row in aliases] == [str(parent), str(shell)]
        if tamper == "file_alias":
            shell.unlink()
            shell.symlink_to("other")
        elif tamper == "parent_alias":
            replacement = root / "replacement"
            replacement.mkdir()
            (replacement / "sh").symlink_to(actual / "dash")
            parent.unlink()
            parent.symlink_to(replacement, target_is_directory=True)
        if tamper:
            with pytest.raises(pub.PublicationHeld, match="^native shell alias changed$"):
                for item in held:
                    item.check()
        else:
            for item in held:
                item.check()
    finally:
        pub._close_physical_controls(held)


def test_root_directory_owns_real_ancestor_descriptor_and_close_still_refuses():
    item = pub._PhysicalDirectory(Path("/"))
    owned = [item.fd, *[fd for _path, fd, _identity in item.ancestors.held]]
    try:
        assert [path for path, _fd, _identity in item.ancestors.held] == [Path("/")]
        for fd in owned:
            assert os.fstat(fd).st_ino == Path("/").stat().st_ino
        item.check()
    finally:
        item.close()
    for fd in owned:
        with pytest.raises(OSError) as closed:
            os.fstat(fd)
        assert closed.value.errno == errno.EBADF
    with pytest.raises(pub.PublicationHeld, match="^publication ancestor descriptors closed$"):
        item.ancestors.check()
    with pytest.raises(pub.PublicationHeld, match="^publication ancestor descriptors closed$"):
        item.check()


def test_actual_root_level_alias_retains_live_root_parent_and_close_refuses():
    alias_path = next(
        Path(value)
        for value in ("/bin", "/var", "/tmp", "/etc", "/sbin", "/lib")
        if Path(value).is_symlink() and Path(value).resolve(strict=True).is_dir()
    )
    item = pub._PhysicalShellAlias(alias_path)
    try:
        assert item.parent.path == Path("/")
        assert item.parent.ancestors.held[0][0] == Path("/")
        assert os.fstat(item.parent.ancestors.held[0][1]).st_ino == Path("/").stat().st_ino
        assert item.check()["path"] == str(alias_path)
    finally:
        item.close()
    with pytest.raises(pub.PublicationHeld, match="^publication ancestor descriptors closed$"):
        item.check()


def test_production_collector_pins_root_parent_of_native_shell_alias(context, tmp_path):
    prepared = provision.provision_absent_owned_head(
        "acme/repo#989",
        context["head"],
        "codex/synthetic-existing-pr",
        git_policy=context["policy"],
    )
    # Mac's real root /var alias can express this exact private shell fixture;
    # Linux uses its actual Git-native /bin/sh fact through the same collector.
    alias = next(
        (
            Path(value)
            for value in ("/var", "/tmp")
            if Path(value).is_symlink()
            and tmp_path.resolve().is_relative_to(Path(value).resolve(strict=True))
        ),
        None,
    )
    selected_shell = None
    if alias is not None:
        physical = tmp_path.resolve() / "native-shell-root-entry"
        physical.mkdir()
        (physical / "dash").write_bytes(b"synthetic native shell payload")
        (physical / "sh").symlink_to("dash")
        selected_shell = alias / (physical / "sh").relative_to(alias.resolve(strict=True))
    environment = pub.private_git_environment_data(context["policy"])

    def read(kind, *, files=(), missing=()):
        for item in files:
            item.check()
        assert not any(Path(path).exists() for path in missing)
        if kind == "helper_shell" and selected_shell is not None:
            return (str(selected_shell) + "\n").encode()
        return subprocess.check_output(
            [str(context["git"]), "-C", prepared["worktree"], *pub._read_command(kind)],
            env=environment,
            timeout=15,
        )

    facts, held, _missing, _route, _gh = pub._collect_git_context_data(
        prepared["worktree"], "acme/repo", context["policy"], read, dict(os.environ)
    )
    try:
        root_aliases = [
            item
            for item in held
            if isinstance(item, pub._PhysicalShellAlias) and item.path.parent == Path("/")
        ]
        assert root_aliases, "The exact production collector must exercise a real root-level alias"
        for item in root_aliases:
            assert item.parent.path == Path("/")
            assert item.parent.ancestors.held[0][0] == Path("/")
            item.check()
        assert facts["helper_shell_route"]["aliases"]
        assert facts["helper_shell"]["sha256"]
    finally:
        pub._close_physical_controls(held)
