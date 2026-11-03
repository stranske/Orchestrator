"""Node-scoped overlays report the file they actually copy."""

import pytest

from local_verify import uncopied_test_paths


@pytest.mark.parametrize(
    "copied,missing",
    [(["tests/test_math_utils.py"], []), ([], ["tests/test_math_utils.py::TestAdd::test_add"])],
)
def test_node_id_copy_scope_uses_containing_file(copied, missing):
    assert (
        uncopied_test_paths(
            {
                "red": {"ok": False},
                "test_paths": ["tests/test_math_utils.py::TestAdd::test_add"],
                "copied_test_paths": copied,
            }
        )
        == missing
    )
