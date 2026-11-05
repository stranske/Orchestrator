# Test-only semantic API reference

`optional_accounting_api.py.txt` is the exact reviewed optional-accounting API snapshot
from source commit `a99539887ebb80261a693a2b5f5677835fe1e949` (package head
`7bd1e23689cb0b12446754e577a412d6662b7d84`). Its SHA-256 is
`bf0b96f7cf0d0655d021d5e72a09df2e36798616cc110e16ce676dea1c74cf68`.

It is test data, loaded explicitly with Python's source loader only by migration-parity tests. Its text extension keeps the immutable historical snapshot outside active-code formatting; all active test/helper Python files follow current formatting. Production imports do not
select this module, and it grants no qualification, process or execution authority.
Keep this historical reference immutable; future production changes must update
the test's expected compatibility deliberately rather than rewrite this snapshot.

## Historical guard audits

`historical_guard_audits.json` replaces runtime `git show` lookups in four
pre-existing semantic audit tests. Its two baseline commits are recorded in full,
with exact source-file SHA256 and Git blob identities. Unchanged-source assertions
compare complete byte hashes. AST assertions retain the original top-level and
nested-function allowlists, the outer pipeline comparison, and the exact one-site
ACT merge restoration check. `legacy_act_merge.py.txt` retains only that original
assignment, represented with `ast.unparse` and verified against its original AST.
No full legacy runtime tree is copied or imported.

The helper pins the manifest's physical SHA256. AST fingerprints retain node types,
all nonempty fields, constant types/values and list order; they omit empty optional
parser metadata using the existing portable fixture audit convention. Byte and
complex literals receive explicit type tags. This permits the same source audit
on supported Python minors without weakening executable guard comparisons.

These are exact historical assertions for the reviewed semantic source composition,
not a statement that future integrations must never change unrelated code. A later
actor-base projection needs its own independently reviewed source parity and current
binding; it must preserve these historical receipts and must not silently label
changed host/security code as historically unchanged. Reference data supplies no
runtime authentication, acceptance, model qualification or execution authority.
