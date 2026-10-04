# PR407 inherited import-path generation pin

The original review finding at `a39c6f2973f95125205ea385275f2e9eb4cc604b` was valid. Author repair `fe35ead6f9d460f0d559f5a73b7eb3559d3b03d0` pins simple inherited paths; a fresh production regression still fails for logical paths such as `mirror/../mirror`, which otherwise re-enter the live publication link. This follow-through preserves the author repair and its test. Although the wrapper prepends its pinned module directory, inherited absolute paths through the logical mirror can expose modules that only exist after a later publication. Both an already-started reader and a Python child then mix generations.

The production argument-path pin now also rebases inherited absolute `PYTHONPATH` entries beneath the logical mirror, with dot segments normalized lexically. It never resolves the live publication symlink when mapping entries. Unrelated absolute paths, relative paths, duplicates and their ordering remain unchanged; existing empty-entry filtering remains unchanged. The wrapper's pinned module directory remains first.

## Executable evidence

A new production-publisher/wrapper regression starts on an old generation, publishes a new generation containing a previously absent `later_only` module, then probes that module in the reader and child. The same gate asserts exact inherited path ordering, nested mirror paths, logical dot segments, outside-root dot segments, sibling prefix collisions, relative entries and duplicated external paths.

```
PYTHONPATH=src python3 -m pytest tests/test_mirror_generations.py::MirrorGenerationTests::test_inherited_pythonpath_cannot_import_a_later_generation_module -q
```

Before the production change, the gate fails:
```
AssertionError: Lists differ: [True, True] != [False, False]
1 failed in 0.46s (on fe35ead)
```
Both processes discovered the new-generation-only module. After the production repair and added path-boundary cases, the same named gate passes:
```
Named gate included in the full restored suite below
```

```
PYTHONPATH=src python3 -m pytest tests/test_mirror_generations.py tests/test_install_verified_snapshot.py -q
102 passed, 4 subtests passed in 13.96s
```

Ruff, Black100, both publisher shell syntax checks and `git diff --check` pass. No installed mirror or wrapper was modified. Exact-head fresh CI, complete review pagination, the seven-minute review floor, guarded squash and postmerge deployment/verification remain separate gates. Source#389 stays open until its acceptance and deployment evidence are complete.
