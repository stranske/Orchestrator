# Packet member validation and evidence manifest repair

Source428 remains OPEN. The real build_packet boundary previously accepted
nonempty lists whose members were blank, null, numeric or malformed mappings.
It now rejects every incomplete member before dispatch, while preserving
nonblank textual references and complete GitHub diff/CheckRun/StatusContext
shapes. Twelve parameterized negative cases fail on the actual old production
source and pass after byte-identical restoration of the repaired source.
A positive case preserves both supported structured gate shapes.

Actual phase argv/cwd/exits, complete lossless console and JUnit, and source
hashes are in validation.json and the phase archives. The complete related
Python suite has57PASS plus23PASS subtests; the Node CLI suite is independently
captured in node-cli.txt.gz. No private Brain record, semantic model result,
provider verdict, native per-case cost, live mirror or production publication
was changed or claimed. Broad inventory/adjudication/later-truth acceptance
remains separately outstanding.

The prior named-acceptance manifest's README hash predated its readability
repair. Its original bytes are preserved; its replacement binds all three
entries to immutable evaluated47156fb2e640b6f6421190293a17b4bb60f66b75, with
explicit revision, length and SHA256. It intentionally describes that revision's
README rather than the README with the later explanation appended. All original
compressed mutation proof is retained byte-for-byte.
