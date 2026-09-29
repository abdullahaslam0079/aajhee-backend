from __future__ import annotations

from typing import Any

from .models import AuditLog


def write_audit(
    *,
    actor,
    action: str,
    target_type: str,
    target_id: Any = "",
    metadata: dict | None = None,
) -> AuditLog:
    return AuditLog.objects.create(
        actor=actor if getattr(actor, "is_authenticated", False) else None,
        action=action,
        target_type=target_type,
        target_id=str(target_id) if target_id is not None else "",
        metadata=metadata or {},
    )
