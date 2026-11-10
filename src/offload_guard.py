"""Shared provider-offload restriction for source-owned maintenance contexts."""

import threading

_CURRENT = threading.local()
_REFUSAL = "deterministic correction maintenance forbids provider/creation/POST"


class MaintenanceUnknown(ValueError):
    pass


def assert_offload_allowed():
    """Earliest offload guard: no-context is unchanged, issued scope forbids it.

    Deliberately does not accept a caller parameter, import auth/provider code,
    inspect output JSON, or offer an alternate provider. A damaged/expired active
    context also refuses rather than falling back to ordinary execution.
    """
    if getattr(_CURRENT, "scope", None) is not None:
        raise MaintenanceUnknown(_REFUSAL)
