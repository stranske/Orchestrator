from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

import model_profile_trial
from scripts import verify_profile_trial_mutations as replay


def test_mutations_require_unique_current_source_anchors():
    source = Path(model_profile_trial.__file__).read_text()
    for name, (anchor, replacement, _, _) in replay.MUTATIONS.items():
        changed = replay.mutate(source, name)
        assert changed != source
        assert replacement in changed
        for invalid in (source.replace(anchor, ""), source + anchor):
            with pytest.raises(ValueError, match="exactly one source anchor"):
                replay.mutate(invalid, name)


def test_transcripts_require_the_exact_assertion_and_one_executed_test(tmp_path):
    xml = tmp_path / "result.xml"
    xml.write_text('<testsuite><testcase name="intended"/></testsuite>')
    replay.verify_result(xml, "intended", None)
    with pytest.raises(ValueError, match="intended assertion"):
        replay.verify_result(xml, "intended", "AssertionError: runs")
    xml.write_text(
        '<testsuite><testcase name="intended">'
        "<failure>AssertionError: runs</failure></testcase></testsuite>"
    )
    replay.verify_result(xml, "intended", "AssertionError: runs")
    with pytest.raises(ValueError, match="Restored test failed"):
        replay.verify_result(xml, "intended", None)
    for invalid in (
        '<testcase name="intended"><failure>unrelated assertion</failure></testcase>',
        '<testcase name="other"><failure>AssertionError: runs</failure></testcase>',
        '<testcase name="intended"><error>AssertionError: runs</error></testcase>',
        '<testcase name="intended"><skipped/></testcase>',
        '<testcase name="intended"/><testcase name="intended"/>',
        "",
    ):
        xml.write_text(f"<testsuite>{invalid}</testsuite>")
        with pytest.raises(ValueError):
            replay.verify_result(xml, "intended", "AssertionError: runs")


def test_capture_uses_copies_and_restores_even_when_verification_fails(tmp_path, monkeypatch):
    repository = tmp_path / "repository"
    (repository / "src").mkdir(parents=True)
    (repository / "tests").mkdir()
    source = Path(model_profile_trial.__file__).read_bytes()
    (repository / replay.TARGET).write_bytes(source)
    for relative in (replay.TEST_MODULE, "tests/conftest.py", "pyproject.toml"):
        (repository / relative).write_text("")

    for fail_verification in (False, True):
        sandbox = tmp_path / f"sandbox-{fail_verification}"
        sandbox.mkdir()
        output = tmp_path / f"output-{fail_verification}"
        observed = []

        @contextmanager
        def temporary_directory(**kwargs):
            yield str(sandbox)

        def fake_pytest(checkout, output, label, test_id, python, failure=None):
            assert checkout == sandbox
            assert (repository / replay.TARGET).read_bytes() == source
            if failure:
                name = label.removesuffix("-failure")
                assert (checkout / replay.TARGET).read_text() == replay.mutate(
                    source.decode(), name
                )
                if fail_verification:
                    raise ValueError("unexpected failure")
            else:
                assert (checkout / replay.TARGET).read_bytes() == source
            observed.append(label)
            return {"exit_status": 1 if failure else 0}

        monkeypatch.setattr(replay.tempfile, "TemporaryDirectory", temporary_directory)
        monkeypatch.setattr(replay, "run_pytest", fake_pytest)
        if fail_verification:
            with pytest.raises(ValueError, match="unexpected failure"):
                replay.capture(repository, output)
            assert not (output / "receipt.json").exists()
        else:
            replay.capture(repository, output)
            receipt = json.loads((output / "receipt.json").read_text())
            assert len(observed) == 6
            assert set(receipt["mutations"]) == set(replay.MUTATIONS)
            for name, result in receipt["mutations"].items():
                assert result["restored_source_sha256"] == receipt["source_sha256"]
                assert result["failure"]["exit_status"] == 1
                assert result["restored"]["exit_status"] == 0
                assert (output / f"{name}.patch").read_text().startswith("--- a/")
        assert (sandbox / replay.TARGET).read_bytes() == source
        assert (repository / replay.TARGET).read_bytes() == source


def test_capture_cannot_overwrite_existing_evidence(tmp_path):
    receipt = tmp_path / "receipt.json"
    receipt.write_text("prior evidence")
    with pytest.raises(ValueError, match="empty evidence directory"):
        replay.capture(tmp_path, tmp_path)
    assert receipt.read_text() == "prior evidence"


def test_subprocess_isolated_state_fresh_bytecode_and_complete_logs(tmp_path, monkeypatch):
    caches = []

    def fake_process(command, **kwargs):
        assert command[:3] == ["/chosen/python", "-m", "pytest"]
        assert command[command.index("-m", 3) + 1] == "not slow"
        assert kwargs["timeout"] == 120
        caches.append(kwargs["env"]["PYTHONPYCACHEPREFIX"])
        assert Path(caches[-1]).parent.is_dir()
        assert kwargs["env"]["ORCH_STATE_DIR"] != "host-state"
        kwargs["stdout"].write(b"complete output\n" * 10000)
        xml = Path(next(arg.split("=", 1)[1] for arg in command if arg.startswith("--junitxml=")))
        xml.write_text('<testsuite><testcase name="intended"/></testsuite>')
        return SimpleNamespace(returncode=0)

    monkeypatch.setenv("ORCH_STATE_DIR", "host-state")
    monkeypatch.setattr(replay.subprocess, "run", fake_process)
    for label in ("first", "second"):
        result = replay.run_pytest(tmp_path, tmp_path, label, "intended", "/chosen/python")
        assert result["exit_status"] == 0
        assert (tmp_path / f"{label}.log").read_bytes() == b"complete output\n" * 10000
    assert caches[0] != caches[1]
    with pytest.raises(ValueError, match="Unexpected pytest exit status"):
        replay.run_pytest(tmp_path, tmp_path, "false-red", "intended", "/chosen/python", "failure")
