"""Consumer-owned regressions for the shared model-table drift check (issue #1295).

Keep these here rather than in the synced test_model_table.py. The shared check remains the
live cross-repository regression; this exercises its failure and repair without network access.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

import test_model_table as shared_tests

import model_table


@pytest.mark.parametrize("mismatch", ["content", "whitespace"])
def test_stale_copy_fails_drift_check_until_resynced(tmp_path, monkeypatch, mismatch) -> None:
    source_text = model_table.TABLE_JSON
    stale_text = (
        source_text.replace('"as_of": "', '"as_of": "stale-', 1)
        if mismatch == "content"
        else source_text + "\n"
    )
    assert stale_text != source_text
    # Both copies still parse: the check must catch drift beyond schema validity.
    model_table.load(stale_text)
    source = tmp_path / "model-table.json"
    source.write_text(source_text, encoding="utf-8")
    module = tmp_path / "model_table.py"
    module.write_text(
        Path(model_table.__file__).read_text(encoding="utf-8").replace(source_text, stale_text, 1),
        encoding="utf-8",
    )
    monkeypatch.setattr(model_table, "__file__", str(module))
    monkeypatch.setattr(model_table, "TABLE_JSON", stale_text)

    def workflows_response(url, *, timeout):
        assert url == model_table.WORKFLOWS_SOURCE
        assert timeout == 30
        return io.BytesIO(source.read_bytes())

    monkeypatch.setattr(shared_tests.urllib.request, "urlopen", workflows_response)
    with pytest.raises(AssertionError, match="model_table.TABLE_JSON differs"):
        shared_tests.test_copy_matches_workflows_source()

    assert model_table._sync(source) == 0
    synced = module.read_text(encoding="utf-8")
    start = synced.index("\n" + model_table.BEGIN_MARK) + 1 + len(model_table.BEGIN_MARK)
    end = synced.index("\n" + model_table.END_MARK, start) + 1
    monkeypatch.setattr(model_table, "TABLE_JSON", synced[start:end])
    shared_tests.test_copy_matches_workflows_source()
