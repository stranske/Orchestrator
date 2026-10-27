# Inactive coding publication hooks

This source slice adds an optional `publication_v1` admission policy and protected baseline/handoff validation to the existing coding worker. Without the optional policy, the existing admission snapshot and worker result remain unchanged. A selected policy must pass the pinned source checks and obtain a valid protected baseline before the coding provider can run.

The baseline and handoff source producers are unavailable in this slice. Selecting the mode therefore holds before the provider; these hooks do not activate publication. A handoff refusal after a known coding terminal result preserves that result, records the delivery hold, and does not repeat provider work.

The module validates bounded publication data and receipts. It grants no merge, verification, completion, provider, installation, or account authority. Installed route qualification and later producer integration are separate changes. The default installed route reader remains unsupported.

The source is based on the reviewed native/controller dependency slice. The standalone v2 policy and its existing tests are already part of canonical main. Production semantic assessment API integration is a separate slice.
