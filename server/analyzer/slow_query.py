"""slow_query_list Tool 구현 (Maintenance).

Tool 시그니처: Doc/01_개발정의서.md §3.2.
"""
from __future__ import annotations

from typing import Any

from server.database.base import BaseAdapter, Capability
from server.utils import to_serializable

_MAX_LIMIT = 100  # Doc/00_개발요건사항.md §34 Performance 원칙


def slow_query_list(
    adapter: BaseAdapter, min_duration: float, limit: int = 20
) -> dict[str, Any]:
    """Slow Operation 목록을 조회한다.

    Returns: { "operations": SlowOperation[] }
    """
    if not adapter.supports(Capability.SLOW_OP):
        return {"status": "CAPABILITY_NOT_SUPPORTED", "capability": "slow_op"}

    limit = min(limit, _MAX_LIMIT)
    operations = adapter.get_slow_operations(min_duration, limit)
    return {"operations": to_serializable(operations)}
