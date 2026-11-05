"""Actual local ODB plus cold IO mechanics; no Root, queue or effect authority."""

import copy
import zlib
from types import SimpleNamespace

import pytest
from test_closer_coding_publication import custody as custody
from test_closer_publication_existing_git_objects import fixture

import closer_coding_publication as publication
import closer_coding_publication_io as io
import closer_correction_controller as controller


@pytest.mark.parametrize("retained_stage", [False, True])
def test_cold_io_keeps_original_object_fingerprint(custody, tmp_path, monkeypatch, retained_stage):
    repo, _git, _env, baseline, candidate, pr, commit, policy = fixture(tmp_path, packed=False)
    # This mechanics fixture deliberately has a tiny changed-file byte cap;
    # default Git sample hooks are unrelated to the retained-object boundary.
    for hook in (repo / ".git/hooks").glob("*.sample"):
        hook.unlink()
    original = publication._ExistingObjects(repo / ".git/objects", baseline, commit, policy)
    proof = original.summary()
    slot = "b" * 64
    reference = {"schema": "inert-data-reference/v1", "sha256": "a" * 64}
    handoff = {
        "staging_link_name": "inert-staging-link.json",
        "operation_sha256": "c" * 64,
        "candidate": candidate,
    }
    scope = SimpleNamespace(
        history={"baseline": {"baseline": baseline, "existing_objects": proof}, "handoff": handoff},
        policy=policy,
        slot=slot,
        handoff_reference=reference,
        original_record={"inert_mechanics": True},
        check=lambda: None,
    )
    if retained_stage:
        stage = publication._build_staging_data(
            custody, slot, baseline, candidate, pr, policy, original, b"[core]\n bare = false\n", {}
        )
        record = {
            "schema": "closer-publication-source-staging/v2",
            "handoff_reference": reference,
            "operation_sha256": handoff["operation_sha256"],
            "admission_sha256": controller.digest(scope.original_record),
            "publication_policy_sha256": publication.digest(policy),
            "candidate_sha256": publication.digest(candidate),
            "private_files": stage.check(),
            **{key: False for key in publication._AUTHORITY},
        }
        digest = publication.digest(record)
        name = "publication-source-staging-" + digest + ".json"
        assert custody.create(name, record)
        assert custody.create(
            handoff["staging_link_name"],
            {
                "schema": "closer-publication-source-staging-link/v1",
                "handoff_reference": reference,
                "staging_reference": {
                    "schema": "closer-publication-source-staging-reference/v1",
                    "name": name,
                    "sha256": digest,
                },
                "stable_slot": slot,
                **{key: False for key in publication._AUTHORITY},
            },
        )
        stage.close()
    original.close()
    oid = next(row["blob"]["git_oid"] for row in baseline["files"] if row["path"] == "src/fix.py")
    path = repo / ".git/objects" / oid[:2] / oid[2:]
    before = path.stat()
    changed = zlib.compress(b"blob 7\0change\n")
    assert len(changed) == before.st_size
    path.chmod(0o600)
    path.write_bytes(changed)
    path.chmod(before.st_mode & 0o777)
    assert path.stat().st_size == before.st_size and path.stat().st_ino == before.st_ino
    # Freshly binding this changed ODB succeeds without prior: the regression
    # therefore exercises IO's retained fingerprint, not a trivial bad fixture.
    fresh = publication._ExistingObjects(repo / ".git/objects", baseline, commit, policy)
    fresh.close()
    captured = []

    def actual_data_seed(host, selected, *, prior=None):
        assert selected == baseline
        captured.append(copy.deepcopy(prior))
        return publication._ExistingObjects(
            repo / ".git/objects", baseline, commit, policy, prior=prior
        )

    monkeypatch.setattr(publication, "_source_snapshot_seed", actual_data_seed)
    config = publication._PhysicalFile(repo / ".git/config", policy["max_total_bytes"])
    host = SimpleNamespace(
        summary={"topology": ["inert", str(repo / ".git")]}, held=[config], pr=pr
    )
    try:
        with pytest.raises(
            publication.PublicationHeld, match="^original loose object content differs$"
        ):
            io._reopen_or_prepare_staging(scope, custody, host)
        assert captured == [proof]
    finally:
        config.close()
