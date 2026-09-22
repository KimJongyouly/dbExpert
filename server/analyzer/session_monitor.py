"""get_locks / get_sessions Tool 구현 (Maintenance, Phase 8).

Tool 시그니처: Doc/01_개발정의서.md §3.2. db_error_analyze가 내부적으로
adapter.get_locks()/get_sessions()를 호출해 에러 상황과 함께 보여주는 것과
별개로, 에러 유무와 무관하게 "지금 Lock/Session 상태 보여줘"에 직접
응답하기 위한 독립 Tool이다.
"""
from __future__ import annotations

from typing import Any

from server.database.base import BaseAdapter, Capability, Session
from server.utils import to_serializable

# Engine마다 "쉬고 있는 세션"을 나타내는 state 문자열 어휘가 달라
# (MySQL "Sleep", PostgreSQL "idle"/"idle in transaction",
#  MSSQL "sleeping"/"background", Tibero "INACTIVE") 부분 문자열로 매칭한다.
_IDLE_STATE_KEYWORDS = ("sleep", "idle", "inactive", "background")


def _is_idle(session: Session) -> bool:
    state = (session.state or "").lower()
    return any(keyword in state for keyword in _IDLE_STATE_KEYWORDS)


def get_locks(adapter: BaseAdapter) -> dict[str, Any]:
    """현재 Lock 목록을 조회한다.

    Returns: { "locks": Lock[] }
    """
    if not adapter.supports(Capability.ACTIVITY):
        return {"status": "CAPABILITY_NOT_SUPPORTED", "capability": "activity"}

    return {"locks": to_serializable(adapter.get_locks())}


def get_sessions(adapter: BaseAdapter, status: str = "all") -> dict[str, Any]:
    """현재 Session 목록을 조회한다.

    Args:
        status: "running" | "idle" | "all"(기본값)

    Returns: { "sessions": Session[] }
    """
    if not adapter.supports(Capability.ACTIVITY):
        return {"status": "CAPABILITY_NOT_SUPPORTED", "capability": "activity"}

    sessions = adapter.get_sessions()
    if status == "running":
        sessions = [s for s in sessions if not _is_idle(s)]
    elif status == "idle":
        sessions = [s for s in sessions if _is_idle(s)]
    # "all"(또는 알 수 없는 값)은 필터링하지 않는다.

    return {"sessions": to_serializable(sessions)}
