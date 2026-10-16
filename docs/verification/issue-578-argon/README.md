# Gemini Argon successor qualification (#578; deployment prerequisite #584)

Owner direction authorizes replacing the Gemini mappings with Argon. The tier contract is
cheap → gemini-4-argon-low, mid → gemini-4-argon-medium, full → gemini-4-argon-high.
The authenticated installed CLI advertises all three identities. Catalog advertisement and
requested argv are not provider-resolved identity, quality, billing, or a replacement profile trial.
The original three live guards remain unchanged. Historical Flash/Pro profile definitions stay
immutable; the existing production retirement map directs selection to the corresponding Argon rung.
Explicit offload tiers retain their requested rung; ordinary advisory offloads default to medium.
Known absence of Argon never silently selects Pro or Flash. An unread catalog retains the existing
UNKNOWN/unverified-pin policy rather than claiming absence or qualification.

The original three live guards fail before repair and pass after the real catalog cache refresh
and mapping repair. Focused two-module validation: 93 passed. Three touched module selftests pass.
Three new nodes raise collection from 3466 to3469; every skip ceiling is unchanged.

`mutations.json` binds three one-at-a-time production breaks to raw RED/GREEN files here:
ignore explicit offload tier; remove the historical low-profile retirement; permit fallback from
an absent Argon family to legacy models. Each named test fails on an assertion under its break,
then passes after byte-identical source restoration. These are controlled code demonstrations,
not provider results or production outcomes.

After ordinary exact-head review/CI and actual verifier disposition, deploy only the merged tree
through the unchanged guarded mirror helper. #584 separately requires deployed observer usage to
show verdict n/a and no new observer_output_change outcome. A merge alone does not establish that.
