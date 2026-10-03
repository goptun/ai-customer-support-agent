"""Append-only audit log of side effects.

Entries are written in the same transaction as the side effect they record, so one never
exists without the other. The table rejects UPDATE and DELETE (trigger in the migration).
"""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from support_agent.models import ActorType, AuditLogEntry


def record(
    session: AsyncSession,
    *,
    actor_type: ActorType,
    actor_id: str | None,
    action: str,
    inputs: dict[str, Any],
    result: dict[str, Any],
) -> None:
    session.add(
        AuditLogEntry(
            actor_type=actor_type, actor_id=actor_id, action=action, inputs=inputs, result=result
        )
    )
