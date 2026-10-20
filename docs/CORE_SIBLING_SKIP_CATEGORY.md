# Independent Core test skips

Issue #817 uses option (b): the Workflows Closer Core is an independently pinned,
external source, not a small snapshot tracked in this repository. The mirror copy
contract therefore remains unchanged.

`core_sibling_skipped_max` is **19**, measured with Core absent in a clean flat
mirror of `addb19d`: 10 binding, 7 drain, 1 mechanisms, and 1 campaign integration
test. All other `.verify-floor.json` fields retain origin/main's values, including
`mirror_skipped_max: 21`. The Core maximum applies in checkouts and both mirror
shapes because the missing prerequisite is the same in each.

The verifier partitions JUnit skip verdicts using both the module and its exact
Core-absence reason. Other skips in those modules and identical reasons in other
modules remain general. Missing or incomplete per-test skip evidence fails the
category ceilings. A missing category agreement grants zero Core allowance.

Every summary prints both category counts and maxima. The existing pytest total
line retains its receipt-compatible format and shows their combined maximum;
each category is enforced separately, so unused allowance cannot cross categories.
Ceiling breaches name both categories and their remedies. Providing
`CLOSER_CORE_SOURCE` with the independent source's scripts and schemas executes
the affected tests; the category count then falls. Waiting does not clear a
breach, and a Core absence never justifies raising the general skip ceiling.

`python3 src/verify.py --selftest` includes a real pytest/JUnit regression in a
temporary flat tree: 19 Core plus 21 general skips pass; adding one unrelated
skip with the same Core reason trips `mirror_skipped_max` at 22; removing it
restores the original counts. It also checks Core overflow, incomplete evidence,
and all three tree shapes. This extends the existing selftest without changing
the repository's pytest collection floor.
