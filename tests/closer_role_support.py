"""Synthetic role verdict for historical capacity/transport fixtures only.

Those tests already replace provider and lifecycle admission boundaries. They
now explicitly supply the role boundary too; they do not qualify any model.
Real role-record and route-selection causality lives in the role test modules.
"""

import closer_role_qualification as roles


def fixture_role_verdict(profile, role):
    # Preserve the historical test cohort, never production qualification.
    effort = profile.get("reasoning_effort")
    return role in roles.ROLES and effort in (
        {"none", "minimal", "low"} if role == roles.BOUNDED_CLASSIFICATION else {"medium", "high"}
    )


def install_synthetic_role_verdict(monkeypatch):
    monkeypatch.setattr(roles, "is_runtime_qualified", fixture_role_verdict)
