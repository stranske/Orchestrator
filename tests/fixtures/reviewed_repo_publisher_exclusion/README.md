# Reviewed publisher static capture

These four files are inert test fixtures captured from the reviewed local
publisher on 2026-10-30. They are never imported from their original path,
never execute a controller, and are copied into a pytest temporary directory
under their canonical filenames before the staging scripts inspect them.

| Fixture | SHA-256 |
| --- | --- |
| `controller.py.txt` | `9b8b9541165522f00bef9f7493107de8c1ea3a0a53c0486a4a042192ddb89c08` |
| `demand_router.py.txt` | `4e0ff69db75f61d2d1d899669e4c6497e0b9e7ad8b0c8c09b87091a2b4bbe7f9` |
| `accountability.py.txt` | `75dceccba965dfe29ec04e0949695b7c754543ab36f0728e9507b3e88c71011b` |
| `delivery-continuity-a1720a1d8e922529db556ecd989792d5410e85d6ef627e385584f20b9b1cd40e.py.txt` | `a1720a1d8e922529db556ecd989792d5410e85d6ef627e385584f20b9b1cd40e` |

The tests rewrite only the copied router's absolute continuity-helper path to
the temporary directory so the pinned helper remains local to the fixture.
