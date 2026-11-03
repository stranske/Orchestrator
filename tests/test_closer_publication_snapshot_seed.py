"""Bounded exact H/tree reconstruction and local-only shallow Git mechanics."""

import hashlib
import subprocess
import zlib

import pytest
from test_closer_coding_publication import TEST_POLICY
from test_closer_coding_publication import custody as custody
from test_closer_publication_private_staging import framed, seed_inputs

import closer_coding_publication as pub


def parented_seed():
    baseline, candidate, pr, objects, config = seed_inputs()
    old_head = pr["head_oid"]
    old_raw = objects.pop(old_head).split(b"\0", 1)[1]
    raw = old_raw.replace(b"\nauthor ", b"\nparent " + old_head.encode() + b"\nauthor ", 1)
    head = hashlib.sha1(framed("commit", raw)).hexdigest()
    baseline["head_oid"] = pr["head_oid"] = candidate["old_head_oid"] = head
    return baseline, candidate, pr, objects, config, raw, old_head, old_raw


def test_snapshot_reconstructs_binary_blobs_and_exact_trees_without_history():
    baseline, _, pr, original, _, raw, old_head, _ = parented_seed()
    seeded = pub._seed_snapshot_data(baseline, raw, TEST_POLICY)
    assert old_head not in seeded
    assert seeded[pr["head_oid"]] == framed("commit", raw)
    assert {k: v for k, v in seeded.items() if k != pr["head_oid"]} == original
    assert any(b"changed" not in value and b"\0" in value for value in seeded.values())


@pytest.mark.parametrize("mutation", ["commit", "tree", "blob", "extra_history"])
def test_snapshot_requires_exact_native_commit_and_bounded_baseline(mutation):
    baseline, _, pr, objects, _, raw, old_head, old_raw = parented_seed()
    if mutation == "commit":
        raw += b"changed"
    elif mutation == "tree":
        baseline["tree_oid"] = "f" * 40
    elif mutation == "blob":
        baseline["files"][0]["blob"]["sha256"] = "f" * 64
    else:
        seeded = pub._seed_snapshot_data(baseline, raw, TEST_POLICY)
        seeded[old_head] = framed("commit", old_raw)
        with pytest.raises(pub.PublicationHeld, match="unbound"):
            pub._validate_seed_data(seeded, pr["head_oid"], TEST_POLICY)
        return
    with pytest.raises(pub.PublicationHeld):
        pub._seed_snapshot_data(baseline, raw, TEST_POLICY)


def test_private_shallow_control_is_bound_and_cannot_change(custody):
    baseline, candidate, pr, _, config, raw, _, _ = parented_seed()
    seeded = pub._seed_snapshot_data(baseline, raw, TEST_POLICY)
    stage = pub._build_staging_data(
        custody, "e" * 64, baseline, candidate, pr, TEST_POLICY, seeded, config, {}
    )
    try:
        shallow = stage.path / "repo.git/shallow"
        assert shallow.read_text() == pr["head_oid"] + "\n"
        shallow.write_text("f" * 40 + "\n")
        with pytest.raises(pub.PublicationHeld):
            stage.check()
    finally:
        stage.close()


def test_local_only_git_push_from_shallow_h_preserves_existing_remote_history(custody, tmp_path):
    """New local fixture bare remote only; no real repo, network, or provider."""
    baseline, candidate, pr, objects, config, raw, parent, parent_raw = parented_seed()
    remote = tmp_path / "synthetic-remote.git"
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_AUTHOR_NAME": "Synthetic",
        "GIT_AUTHOR_EMAIL": "synthetic@example.invalid",
        "GIT_COMMITTER_NAME": "Synthetic",
        "GIT_COMMITTER_EMAIL": "synthetic@example.invalid",
        "GIT_TERMINAL_PROMPT": "0",
    }

    def git(*args, input=None):
        return subprocess.run(
            ["/usr/bin/git", *map(str, args)],
            input=input,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
            timeout=10,
        ).stdout

    git("init", "--bare", remote)
    remote_objects = {
        **objects,
        parent: framed("commit", parent_raw),
        pr["head_oid"]: framed("commit", raw),
    }
    for oid, value in remote_objects.items():
        path = remote / "objects" / oid[:2] / oid[2:]
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(zlib.compress(value))
    git("--git-dir", remote, "update-ref", "refs/heads/" + pr["head_ref"], pr["head_oid"])
    # Pack only this newly created synthetic remote. This proves the seed path
    # never depends on loose object files in the selected source repository.
    git("--git-dir", remote, "gc", "--prune=now")
    assert list((remote / "objects/pack").glob("*.pack"))
    assert not (remote / "objects" / pr["head_oid"][:2] / pr["head_oid"][2:]).exists()
    assert git("--git-dir", remote, "cat-file", "commit", pr["head_oid"]) == raw
    before = {
        str(p.relative_to(remote)): p.read_bytes()
        for p in (remote / "objects").rglob("*")
        if p.is_file()
    }
    seeded = pub._seed_snapshot_data(baseline, raw, TEST_POLICY)
    stage = pub._build_staging_data(
        custody, "d" * 64, baseline, candidate, pr, TEST_POLICY, seeded, config, {}
    )
    try:
        private = stage.path / "repo.git"
        assert not (private / "objects" / parent[:2] / parent[2:]).exists()
        new = (
            git(
                "--git-dir",
                private,
                "commit-tree",
                baseline["tree_oid"],
                "-p",
                pr["head_oid"],
                input=b"Local synthetic correction\n",
            )
            .strip()
            .decode()
        )
        git("--git-dir", private, "update-ref", "refs/heads/" + pr["head_ref"], new, pr["head_oid"])
        git(
            "--git-dir",
            private,
            "push",
            "--porcelain",
            "--force-with-lease=refs/heads/" + pr["head_ref"] + ":" + pr["head_oid"],
            remote,
            "HEAD:refs/heads/" + pr["head_ref"],
        )
        assert (
            git("--git-dir", remote, "rev-parse", "refs/heads/" + pr["head_ref"]).strip().decode()
            == new
        )
        assert git("--git-dir", remote, "rev-list", "--count", new).strip() == b"3"
        assert not (remote / "shallow").exists()
        for path, value in before.items():
            assert (remote / path).read_bytes() == value
    finally:
        stage.close()
