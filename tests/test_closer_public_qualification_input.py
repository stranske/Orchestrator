"""Source-only public qualification input controls; no model or enrollment call."""

import hashlib
import json
import time
from types import SimpleNamespace

import pytest

import closer_claude_role_admission as admission
import closer_dispatcher
import closer_evidence
import closer_public_qualification_input as public_input


@pytest.fixture
def seed(tmp_path, monkeypatch):
    body = "## Tasks\n- [ ] " + admission.PUBLIC_CRITERION
    snapshot = {
        "target": admission.PUBLIC_TARGET,
        "kind": "pr",
        "state": "MERGED",
        "head": sorted(admission.PUBLIC_HEADS)[0],
        "base": "b" * 40,
        "merge": "d" * 40,
        "revision": "public-canary-seed",
        "review_revision": "none",
        "sources": [{"target": admission.PUBLIC_SOURCE, "kind": "issue", "body": body}],
    }
    patch = b'+    raise NotImplementedError("incomplete CSV public canary")\n'
    original = {
        "target": snapshot["target"],
        "head": snapshot["head"],
        "base": snapshot["base"],
        "classification": "uncertain",
        "substantive_needs": [],
        "findings": [],
        "decision": {
            "criteria": [
                {
                    "source": admission.PUBLIC_SOURCE,
                    "criterion": text,
                    "result": "UNKNOWN",
                    "evidence": [],
                    "proofs": [],
                }
                for text in (body, admission.PUBLIC_CRITERION)
            ]
        },
    }
    root = tmp_path / "seed"
    root.mkdir()
    snapshot_path = root / "snapshot.json"
    patch_path = root / "patch.patch"
    report_path = root / "report.json"
    snapshot_path.write_text(json.dumps(snapshot, sort_keys=True))
    patch_path.write_bytes(patch)
    report_path.write_text(
        json.dumps(
            {
                "schema": admission.SCHEMA,
                "capability_pass": True,
                "authority": False,
                "activation": False,
                "proposal": {"schema": admission.PROPOSAL_SCHEMA, "original": original},
                "quality_ref": {"sha256": "quality-only"},
            },
            sort_keys=True,
        )
    )

    def pin(path, prefix):
        raw = path.read_bytes()
        monkeypatch.setattr(public_input, prefix + "_PATH", path)
        monkeypatch.setattr(public_input, prefix + "_SHA256", hashlib.sha256(raw).hexdigest())
        monkeypatch.setattr(public_input, prefix + "_BYTES", len(raw))

    pin(snapshot_path, "SNAPSHOT")
    pin(patch_path, "PATCH")
    pin(report_path, "ORIGINAL_REPORT")
    monkeypatch.setattr(public_input, "TARGET", snapshot["target"])
    monkeypatch.setattr(public_input, "HEAD", snapshot["head"])
    monkeypatch.setattr(public_input, "MERGE", snapshot["merge"])
    monkeypatch.setattr(public_input, "SOURCE", admission.PUBLIC_SOURCE)
    monkeypatch.setattr(public_input, "SOURCE_SHA256", hashlib.sha256(body.encode()).hexdigest())
    monkeypatch.setattr(public_input, "CSV_FILE", admission.PUBLIC_FILE)
    monkeypatch.setattr(public_input, "CSV_CRITERION", admission.PUBLIC_CRITERION)
    monkeypatch.setattr(public_input, "QUESTION", admission.QUESTION)
    monkeypatch.setattr(
        admission, "PUBLIC_SOURCE_SHA256", hashlib.sha256(body.encode()).hexdigest()
    )
    monkeypatch.setattr(admission, "PUBLIC_HEADS", frozenset({snapshot["head"]}))
    parent = tmp_path / "private-parent"
    parent.mkdir(mode=0o700)
    parent.chmod(0o700)
    return SimpleNamespace(
        snapshot=snapshot, patch=patch, parent=parent, snapshot_path=snapshot_path
    )


def _collector(snapshot, _gh, root, **_kwargs):
    def api(endpoint, **_kw):
        if "/compare/" in endpoint:
            return {
                "status": "ahead",
                "files": [
                    {
                        "filename": admission.PUBLIC_FILE,
                        "status": "added",
                        "patch": _collector.patch.decode(),
                        "sha": "c" * 40,
                    }
                ],
            }
        if "/actions/runs?" in endpoint:
            return {"workflow_runs": []}
        raise AssertionError(endpoint)

    return closer_evidence.collect_evidence(snapshot, api, root, action="collect_evidence")


def test_collects_exact_seed_then_replays_admission(seed, monkeypatch):
    _collector.patch = seed.patch
    calls = []

    def collect(*args, **kwargs):
        calls.append(args)
        return _collector(*args, **kwargs)

    monkeypatch.setattr(closer_dispatcher, "collect_native_evidence", collect)
    result = public_input.prepare_public_qualification_input(
        gh=object(), private_root=seed.parent / "fresh", deadline=time.monotonic() + 5
    )
    assert len(calls) == 1 and calls[0][0] == seed.snapshot
    packet, _root, request, _prompt, _descriptor = admission._prepare(result["proposal"])
    assert packet["target"] == seed.snapshot["target"]
    assert request["criterion"] == admission.PUBLIC_CRITERION
    assert result["authority"] is result["activation"] is result["provider_invoked"] is False
    assert result["enrollment_performed"] is False


@pytest.mark.parametrize("field", ["head", "merge", "source", "packet", "ref"])
def test_seed_corruption_refuses_before_collector(seed, monkeypatch, field):
    _collector.patch = seed.patch
    called = []
    monkeypatch.setattr(
        closer_dispatcher, "collect_native_evidence", lambda *_a, **_k: called.append(1)
    )
    if field in {"head", "merge", "source"}:
        value = json.loads(seed.snapshot_path.read_text())
        if field == "source":
            value["sources"][0]["body"] += " changed"
        else:
            value[field] = "0" * 40
        seed.snapshot_path.write_text(json.dumps(value, sort_keys=True))
    elif field == "packet":
        monkeypatch.setattr(public_input, "PATCH_SHA256", "0" * 64)
    else:
        monkeypatch.setattr(
            public_input, "SNAPSHOT_PATH", seed.snapshot_path.parent / "missing.json"
        )
    with pytest.raises(public_input.PublicQualificationInputUnavailable):
        public_input.prepare_public_qualification_input(
            gh=object(),
            private_root=seed.parent / ("fresh-" + field),
            deadline=time.monotonic() + 5,
        )
    assert called == []
