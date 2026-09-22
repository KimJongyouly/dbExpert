"""db_error_analyze Tool 구현 (Maintenance).

Tool 시그니처: Doc/01_개발정의서.md §3.2. Adapter가 반환하는 사실(Error/
Session/Lock/Slow Operation)만 구조화하고, "원인이 무엇이다"라는 판단
문장은 만들지 않는다 — 그건 Claude의 몫이다(Doc/00_개발요건사항.md §6).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from server.database.base import BaseAdapter, Capability
from server.utils import to_serializable

# db_error_analyze가 "동시간대 Slow Query"를 함께 보여줄 때 쓰는 기본값.
_RELATED_SLOW_OP_THRESHOLD_SEC = 1.0
_RELATED_SLOW_OP_LIMIT = 20


def db_error_analyze(
    adapter: BaseAdapter,
    error: Optional[str] = None,
    since: Optional[datetime] = None,
) -> dict[str, Any]:
    """DB Error 메시지(또는 최근 시간 범위)를 분석해 원인 추정에 필요한
    구조화된 사실을 반환한다.

    Returns:
        {
          "error_code": str | None,
          "error_message": str,
          "occurred_at": str | None,   # ISO 8601
          "related_sessions": Session[],
          "related_locks": Lock[],
          "running_sql": str[],
          "recent_slow_queries": SlowOperation[],
        }
    """
    if not adapter.supports(Capability.ERROR_LOG) or not adapter.supports(
        Capability.ACTIVITY
    ):
        return {"status": "CAPABILITY_NOT_SUPPORTED", "capability": "error_log/activity"}

    errors = adapter.get_errors(since=since)

    if error:
        # 사용자가 에러 메시지를 직접 준 경우, 그 문구를 포함하는 항목을
        # 우선 사용한다(완전 일치가 아니라 부분 포함 매칭). 못 찾으면
        # 사용자가 준 메시지 자체를 error_message로 그대로 반환한다.
        matched = next(
            (e for e in errors if error.lower() in (e.message or "").lower()), None
        )
        error_code = matched.error_code if matched else None
        occurred_at = matched.occurred_at if matched else None
        error_message = error
    else:
        latest = errors[0] if errors else None
        error_code = latest.error_code if latest else None
        occurred_at = latest.occurred_at if latest else None
        error_message = latest.message if latest else ""

    sessions = adapter.get_sessions()
    locks = adapter.get_locks()
    running_sql = [s.current_operation for s in sessions if s.current_operation]

    recent_slow_queries = []
    if adapter.supports(Capability.SLOW_OP):
        recent_slow_queries = adapter.get_slow_operations(
            _RELATED_SLOW_OP_THRESHOLD_SEC, _RELATED_SLOW_OP_LIMIT
        )

    return {
        "error_code": error_code,
        "error_message": error_message,
        "occurred_at": to_serializable(occurred_at),
        "related_sessions": to_serializable(sessions),
        "related_locks": to_serializable(locks),
        "running_sql": running_sql,
        "recent_slow_queries": to_serializable(recent_slow_queries),
    }
