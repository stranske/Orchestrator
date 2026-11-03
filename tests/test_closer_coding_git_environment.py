"""Fresh private Git-environment data stays inert and closed."""

import hashlib
import os
import shutil
import stat
import tempfile
from pathlib import Path

import pytest

import closer_coding_git_environment as environment
import closer_coding_publication as publication
import closer_coding_session_v2 as coding_session


@pytest.fixture
def portable_gh(monkeypatch):
    """Bind test-only policy selectors to the real local GH helper."""
    located = shutil.which("gh")
    assert located is not None, "portable Git-environment test requires installed gh"
    helper = Path(located).resolve(strict=True)
    monkeypatch.setattr(environment, "REVIEWED_GH_PATH", str(helper))
    monkeypatch.setattr(
        environment, "REVIEWED_GH_SHA256", hashlib.sha256(helper.read_bytes()).hexdigest()
    )
    return helper


def _policy(tmp_path, *, base=None):
    gh = Path(environment.REVIEWED_GH_PATH)
    git = Path(shutil.which("git")).resolve(strict=True)
    controller_private = (tmp_path if base is None else base) / "controller-private"
    controller_private.mkdir(mode=0o700)
    root = controller_private / "publication-environment"
    consumer = {
        "runner": "existing-local-controller",
        "agent": "codex",
        "owner_login": "stranske",
        "account_sha256": environment.REVIEWED_ACCOUNT_SHA256,
        "profile_id": environment.REVIEWED_PROFILE_ID,
        "profile_sha256": environment.REVIEWED_PROFILE_SHA256,
        "native_bootstrap": {
            "gh_path": str(gh),
            "gh_sha256": environment.REVIEWED_GH_SHA256,
            "home": str(tmp_path.resolve()),
        },
        "coding_session_v2": {
            "schema": coding_session.POLICY_SCHEMA,
            "consumer_id": environment.REVIEWED_CONSUMER_ID,
            "agent": "codex",
            "profile_id": environment.REVIEWED_PROFILE_ID,
            "profile_sha256": environment.REVIEWED_PROFILE_SHA256,
            "budget": {
                "schema": coding_session.BUDGET_SCHEMA,
                "max_runtime_ms": 900000,
                "max_cleanup_ms": 10000,
                "max_prompt_utf8_bytes": 65536,
                "max_captured_output_bytes": 65536,
                "max_retained_final_utf8_bytes": 65536,
                "max_retained_receipt_bytes": 65536,
                "max_outer_submissions": 1,
                "native_model_attempts": "UNKNOWN",
                "native_input_tokens": "UNKNOWN",
                "native_output_tokens": "UNKNOWN",
            },
        },
        "publication_v2": {
            "schema": publication.ANCHOR_POLICY_SCHEMA,
            "mode": "owned_existing_pr",
            "consumer_id": environment.REVIEWED_CONSUMER_ID,
            "git_executable": {
                "path": str(git),
                "sha256": hashlib.sha256(git.read_bytes()).hexdigest(),
            },
            "allowed_path_prefixes": [
                *environment.REVIEWED_ALLOWED_PATHS,
            ],
            "max_files": 2,
            "max_file_bytes": 65536,
            "max_total_bytes": 131072,
            "max_ack_bytes": 65536,
            "max_push_submissions": 1,
            "request_timeout_seconds": 120,
            "max_native_io_seconds": 15,
            "trusted_context_sha256": environment.REVIEWED_TRUSTED_CONTEXT_SHA256,
            "baseline_limits": {
                "schema": "closer-publication-baseline-limits/v1",
                "max_files": 4096,
                "max_file_bytes": 80 * 1048576,
                "max_total_bytes": 128 * 1048576,
                "max_inventory_bytes": 1048576,
            },
            "git_environment_v1": {
                "schema": "closer-publication-private-git-environment/v1",
                "home": environment.ORIGINAL_GIT_HOME,
                "config_sha256": environment.ORIGINAL_CONFIG_SHA256,
            },
        },
    }
    return (
        {
            "correction_controller_installation": {
                "config": {
                    "source_sha": environment.REVIEWED_SOURCE_SHA,
                    "repository": environment.REVIEWED_REPOSITORY,
                    "repository_id": environment.REVIEWED_REPOSITORY_ID,
                    "writer_id": environment.REVIEWED_WRITER_ID,
                    "writer_login": environment.REVIEWED_WRITER_LOGIN,
                    "writer_type": environment.REVIEWED_WRITER_TYPE,
                    "consumers": {environment.REVIEWED_CONSUMER_ID: consumer},
                }
            }
        },
        root,
        gh,
    )


def test_projection_changes_only_fresh_git_environment(tmp_path):
    policy, root, gh = _policy(tmp_path)
    projected = environment.rebase_private_git_environment(
        policy,
        old_source_sha=environment.REVIEWED_SOURCE_SHA,
        consumer_id=environment.REVIEWED_CONSUMER_ID,
        private_root=root,
        gh_path=gh,
    )
    before = policy["correction_controller_installation"]["config"]["consumers"][
        environment.REVIEWED_CONSUMER_ID
    ]
    after = projected["correction_controller_installation"]["config"]["consumers"][
        environment.REVIEWED_CONSUMER_ID
    ]
    assert policy != projected
    assert {key: value for key, value in before.items() if key != "publication_v2"} == {
        key: value for key, value in after.items() if key != "publication_v2"
    }
    assert {
        key: value for key, value in before["publication_v2"].items() if key != "git_environment_v1"
    } == {
        key: value for key, value in after["publication_v2"].items() if key != "git_environment_v1"
    }
    assert after["publication_v2"]["git_environment_v1"] == {
        "schema": "closer-publication-private-git-environment/v1",
        "home": str(root / "git-home"),
        "config_sha256": hashlib.sha256(environment._config_bytes(gh)).hexdigest(),
    }


@pytest.mark.parametrize(
    "mutator",
    [
        lambda policy, root, gh: policy["correction_controller_installation"]["config"].update(
            source_sha="f" * 40
        ),
        lambda policy, root, gh: policy["correction_controller_installation"]["config"].update(
            repository_id=0
        ),
        lambda policy, root, gh: policy["correction_controller_installation"]["config"][
            "consumers"
        ][environment.REVIEWED_CONSUMER_ID].update(profile_id="foreign"),
        lambda policy, root, gh: policy["correction_controller_installation"]["config"][
            "consumers"
        ][environment.REVIEWED_CONSUMER_ID].update(account_sha256="a" * 64),
        lambda policy, root, gh: policy["correction_controller_installation"]["config"][
            "consumers"
        ][environment.REVIEWED_CONSUMER_ID]["native_bootstrap"].update(gh_sha256="a" * 64),
        lambda policy, root, gh: policy["correction_controller_installation"]["config"][
            "consumers"
        ][environment.REVIEWED_CONSUMER_ID]["publication_v2"].update(
            trusted_context_sha256="a" * 64
        ),
        lambda policy, root, gh: policy["correction_controller_installation"]["config"][
            "consumers"
        ][environment.REVIEWED_CONSUMER_ID]["publication_v2"].update(
            allowed_path_prefixes=["foreign.py"]
        ),
        lambda policy, root, gh: policy["correction_controller_installation"]["config"][
            "consumers"
        ][environment.REVIEWED_CONSUMER_ID]["publication_v2"]["git_environment_v1"].update(
            config_sha256="a" * 64
        ),
        lambda policy, root, gh: root.parent.joinpath("linked").symlink_to(
            root.parent, target_is_directory=True
        ),
    ],
)
def test_projection_refuses_unreviewed_or_aliased_inputs(tmp_path, mutator):
    policy, root, gh = _policy(tmp_path)
    mutator(policy, root, gh)
    selected_root = root.parent / "linked" if (root.parent / "linked").is_symlink() else root
    with pytest.raises(environment.GitEnvironmentHeld):
        environment.rebase_private_git_environment(
            policy,
            old_source_sha=environment.REVIEWED_SOURCE_SHA,
            consumer_id=environment.REVIEWED_CONSUMER_ID,
            private_root=selected_root,
            gh_path=gh,
        )


def test_provision_is_fresh_closed_and_inert(tmp_path, portable_gh):
    policy, root, gh = _policy(tmp_path)
    projected = environment.rebase_private_git_environment(
        policy,
        old_source_sha=environment.REVIEWED_SOURCE_SHA,
        consumer_id=environment.REVIEWED_CONSUMER_ID,
        private_root=root,
        gh_path=gh,
    )
    result = environment.provision_private_git_environment(
        projected, consumer_id=environment.REVIEWED_CONSUMER_ID, private_root=root
    )
    home = root / "git-home"
    assert result == {
        "schema": "closer-fresh-private-git-environment-data/v1",
        "home": str(home),
        "config_sha256": hashlib.sha256(environment._config_bytes(gh)).hexdigest(),
        "credential_authenticated": False,
        "execution_authority": False,
    }
    assert set(path.name for path in home.iterdir()) == {".gitconfig", ".config"}
    assert set(path.name for path in (home / ".config").iterdir()) == {"git"}
    assert list((home / ".config" / "git").iterdir()) == []
    assert all(
        (path.stat().st_mode & 0o777) == 0o700
        for path in (root, home, home / ".config", home / ".config" / "git")
    )
    assert (home / ".gitconfig").read_bytes() == environment._config_bytes(gh)
    assert b"hooksPath" not in (home / ".gitconfig").read_bytes()
    assert b"credential_authenticated" not in (home / ".gitconfig").read_bytes()
    assert (
        environment.rebase_private_git_environment(
            policy,
            old_source_sha=environment.REVIEWED_SOURCE_SHA,
            consumer_id=environment.REVIEWED_CONSUMER_ID,
            private_root=root,
            gh_path=gh,
        )
        == projected
    )


def test_native_publication_rejects_explicit_hook_configuration():
    raw = b"global\0file:/closed/.gitconfig\0core.hookspath\0"
    with pytest.raises(publication.PublicationHeld):
        publication._configuration_names(raw, len(raw))


def _existing_root(root):
    root.mkdir()


def _foreign_root(root):
    root.mkdir()
    (root / "foreign").write_text("foreign")


@pytest.mark.parametrize("setup", [_existing_root, _foreign_root])
def test_provision_refuses_existing_or_foreign_root(tmp_path, setup):
    policy, root, gh = _policy(tmp_path)
    projected = environment.rebase_private_git_environment(
        policy,
        old_source_sha=environment.REVIEWED_SOURCE_SHA,
        consumer_id=environment.REVIEWED_CONSUMER_ID,
        private_root=root,
        gh_path=gh,
    )
    setup(root)
    with pytest.raises(environment.GitEnvironmentHeld):
        environment.provision_private_git_environment(
            projected, consumer_id=environment.REVIEWED_CONSUMER_ID, private_root=root
        )


def test_provision_refuses_root_symlink(tmp_path):
    policy, root, gh = _policy(tmp_path)
    target = tmp_path / "target"
    target.mkdir()
    root.symlink_to(target, target_is_directory=True)
    with pytest.raises(environment.GitEnvironmentHeld):
        environment.rebase_private_git_environment(
            policy,
            old_source_sha=environment.REVIEWED_SOURCE_SHA,
            consumer_id=environment.REVIEWED_CONSUMER_ID,
            private_root=root,
            gh_path=gh,
        )


@pytest.mark.parametrize("mode", [0o777, 0o755])
def test_projection_refuses_unprotected_or_foreign_private_parent(tmp_path, mode):
    policy, root, gh = _policy(tmp_path)
    projected = environment.rebase_private_git_environment(
        policy,
        old_source_sha=environment.REVIEWED_SOURCE_SHA,
        consumer_id=environment.REVIEWED_CONSUMER_ID,
        private_root=root,
        gh_path=gh,
    )
    root.parent.chmod(mode)
    try:
        with pytest.raises(environment.GitEnvironmentHeld):
            environment.provision_private_git_environment(
                projected,
                consumer_id=environment.REVIEWED_CONSUMER_ID,
                private_root=root,
            )
    finally:
        root.parent.chmod(0o700)


def test_projection_refuses_non_dedicated_private_root(tmp_path):
    policy, root, gh = _policy(tmp_path)
    with pytest.raises(environment.GitEnvironmentHeld):
        environment.rebase_private_git_environment(
            policy,
            old_source_sha=environment.REVIEWED_SOURCE_SHA,
            consumer_id=environment.REVIEWED_CONSUMER_ID,
            private_root=root.parent / "other",
            gh_path=gh,
        )


def test_provision_accepts_root_owned_sticky_temporary_ancestor(tmp_path, portable_gh):
    temporary_root = Path("/tmp").resolve(strict=True)
    metadata = temporary_root.stat(follow_symlinks=False)
    if metadata.st_uid != 0 or not stat.S_IMODE(metadata.st_mode) & stat.S_ISVTX:
        pytest.skip("platform temporary root is not root-owned sticky")
    with tempfile.TemporaryDirectory(dir=temporary_root) as raw:
        policy, root, gh = _policy(tmp_path, base=Path(raw))
        projected = environment.rebase_private_git_environment(
            policy,
            old_source_sha=environment.REVIEWED_SOURCE_SHA,
            consumer_id=environment.REVIEWED_CONSUMER_ID,
            private_root=root,
            gh_path=gh,
        )
        result = environment.provision_private_git_environment(
            projected, consumer_id=environment.REVIEWED_CONSUMER_ID, private_root=root
        )
    assert result["execution_authority"] is False


def test_provision_refuses_nonsticky_world_writable_ancestor(tmp_path):
    policy, root, gh = _policy(tmp_path)
    projected = environment.rebase_private_git_environment(
        policy,
        old_source_sha=environment.REVIEWED_SOURCE_SHA,
        consumer_id=environment.REVIEWED_CONSUMER_ID,
        private_root=root,
        gh_path=gh,
    )
    ancestor = root.parent.parent
    original_mode = stat.S_IMODE(ancestor.stat(follow_symlinks=False).st_mode)
    ancestor.chmod(0o777)
    try:
        with pytest.raises(environment.GitEnvironmentHeld):
            environment.provision_private_git_environment(
                projected, consumer_id=environment.REVIEWED_CONSUMER_ID, private_root=root
            )
    finally:
        os.chmod(ancestor, original_mode)


@pytest.mark.parametrize("relative", [".", "git-home", "git-home/.config", "git-home/.config/git"])
def test_provision_removes_created_directory_when_immediate_validation_holds(
    tmp_path, monkeypatch, relative
):
    policy, root, gh = _policy(tmp_path)
    projected = environment.rebase_private_git_environment(
        policy,
        old_source_sha=environment.REVIEWED_SOURCE_SHA,
        consumer_id=environment.REVIEWED_CONSUMER_ID,
        private_root=root,
        gh_path=gh,
    )
    original_mkdir = os.mkdir
    failing_directory = root / relative

    def create_with_invalid_mode(path, mode=0o777, *, dir_fd=None):
        original_mkdir(path, mode, dir_fd=dir_fd)
        if Path(path) == failing_directory:
            os.chmod(path, 0o755)

    monkeypatch.setattr(environment.os, "mkdir", create_with_invalid_mode)
    with pytest.raises(environment.GitEnvironmentHeld, match="fresh private Git directory differs"):
        environment.provision_private_git_environment(
            projected,
            consumer_id=environment.REVIEWED_CONSUMER_ID,
            private_root=root,
        )
    assert not root.exists()
    assert root.parent.is_dir()
    assert stat.S_IMODE(root.parent.stat().st_mode) == 0o700


def test_provision_removes_its_partial_tree_when_publication_validation_holds(
    tmp_path, monkeypatch
):
    policy, root, gh = _policy(tmp_path)
    projected = environment.rebase_private_git_environment(
        policy,
        old_source_sha=environment.REVIEWED_SOURCE_SHA,
        consumer_id=environment.REVIEWED_CONSUMER_ID,
        private_root=root,
        gh_path=gh,
    )

    def held(*_args, **_kwargs):
        raise publication.PublicationHeld("synthetic validation hold")

    monkeypatch.setattr(publication, "private_git_environment_data", held)
    with pytest.raises(publication.PublicationHeld):
        environment.provision_private_git_environment(
            projected,
            consumer_id=environment.REVIEWED_CONSUMER_ID,
            private_root=root,
        )
    assert not root.exists()
