"""get_table_statistics / get_index_statistics Tool 구현 (Tuner, Phase 10).

Tool 시그니처: Doc/01_개발정의서.md §3.3. index_recommend가 내부적으로
adapter.get_index_statistics()를 호출해 추천 근거로 쓰는 것과 별개로,
"이 테이블 통계/인덱스 보여줘"에 직접 응답하기 위한 독립 Tool이다.
"""
from __future__ import annotations

from typing import Any, Optional

from server.database.base import BaseAdapter, Capability
from server.utils import to_serializable


def get_table_statistics(
    adapter: BaseAdapter, table: str, schema: Optional[str] = None
) -> dict[str, Any]:
    """Table/Container 단위 통계(행수 추정치, 크기 등)를 조회한다.

    Returns: ContainerStatistics 필드를 그대로 담은 dict
    """
    if not adapter.supports(Capability.STATS):
        return {"status": "CAPABILITY_NOT_SUPPORTED", "capability": "stats"}

    return to_serializable(adapter.get_table_statistics(schema, table))


def get_index_statistics(
    adapter: BaseAdapter, table: str, schema: Optional[str] = None
) -> dict[str, Any]:
    """Table/Container의 Index 목록과 통계를 조회한다.

    Returns: { "indexes": IndexStat[] }
    """
    if not adapter.supports(Capability.STATS):
        return {"status": "CAPABILITY_NOT_SUPPORTED", "capability": "stats"}

    return {"indexes": to_serializable(adapter.get_index_statistics(schema, table))}
