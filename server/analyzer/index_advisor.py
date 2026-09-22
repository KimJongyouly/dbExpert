"""index_recommend Tool 구현 (Tuner).

Tool 시그니처: Doc/01_개발정의서.md §3.3. 실제 판단(추천 이유의 서술,
Trade-off 설명)은 Claude가 수행하며, 이 함수는 근거 데이터(기존 Index,
SQL에서 추출한 후보 컬럼, 실행계획)와 기계적으로 도출 가능한 후보 목록만
구조화한다(Doc/00_개발요건사항.md §6).
"""
from __future__ import annotations

import re
from typing import Any, Optional

from server.database.base import BaseAdapter, Capability, IndexStatistics
from server.utils import to_serializable

_WHERE_CLAUSE_RE = re.compile(
    r"\bWHERE\b(.*?)(?:\bGROUP BY\b|\bORDER BY\b|\bLIMIT\b|$)", re.IGNORECASE | re.DOTALL
)
_ORDER_BY_CLAUSE_RE = re.compile(
    r"\bORDER BY\b(.*?)(?:\bLIMIT\b|$)", re.IGNORECASE | re.DOTALL
)
_JOIN_ON_CLAUSE_RE = re.compile(
    r"\bON\b(.*?)(?=\bWHERE\b|\bJOIN\b|\bGROUP BY\b|\bORDER BY\b|\bLIMIT\b|$)",
    re.IGNORECASE | re.DOTALL,
)
_CONDITION_COLUMN_RE = re.compile(
    r"([A-Za-z_][A-Za-z0-9_]*)\s*(?:=|>=|<=|<>|!=|>|<|\bLIKE\b|\bIN\b)", re.IGNORECASE
)
_QUALIFIED_COLUMN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\.([A-Za-z_][A-Za-z0-9_]*)")
_ORDER_BY_KEYWORDS = {"ASC", "DESC"}


def _extract_candidate_columns(sql: str) -> list[str]:
    """WHERE/JOIN ON/ORDER BY 절에서 Index 후보가 될 만한 컬럼명을 뽑는다.

    완전한 SQL Parser가 아니라 실용적인 정규식 기반 추출이다 — 오탐/누락이
    있을 수 있으므로 최종 판단은 Claude가 explain_plan과 함께 교차 검증한다.
    """
    candidates: set[str] = set()

    where_match = _WHERE_CLAUSE_RE.search(sql)
    if where_match:
        candidates.update(
            c.lower() for c in _CONDITION_COLUMN_RE.findall(where_match.group(1))
        )

    for on_clause in _JOIN_ON_CLAUSE_RE.findall(sql):
        candidates.update(c.lower() for c in _QUALIFIED_COLUMN_RE.findall(on_clause))
        candidates.update(c.lower() for c in _CONDITION_COLUMN_RE.findall(on_clause))

    order_match = _ORDER_BY_CLAUSE_RE.search(sql)
    if order_match:
        for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", order_match.group(1)):
            if token.upper() not in _ORDER_BY_KEYWORDS:
                candidates.add(token.lower())

    return sorted(candidates)


def _build_ddl_suggestion(target_table: str, columns: list[str]) -> str:
    index_name = f"idx_{target_table}_{'_'.join(columns)}"[:63]
    return (
        "-- 제안일 뿐이며 이 Plugin은 절대 실행하지 않는다. "
        "검토 후 별도 절차로 적용할 것.\n"
        f"CREATE INDEX {index_name} ON {target_table} ({', '.join(columns)});"
    )


def index_recommend(
    adapter: BaseAdapter, target_table: str, sql: Optional[str] = None
) -> dict[str, Any]:
    """Missing/Unused Index 후보와 DDL 제안을 근거 데이터와 함께 반환한다.

    Returns:
        {
          "existing_indexes": IndexStatistics[],
          "missing_index_candidates": [{"columns": str[], "reason": str}],
          "unused_index_candidates": str[],
          "ddl_suggestion": str,
          "explain_plan": dict | None,   # sql이 주어졌을 때만 채워지는 참고 정보
        }
    """
    if not adapter.supports(Capability.STATS):
        return {"status": "CAPABILITY_NOT_SUPPORTED", "capability": "stats"}

    existing_indexes: list[IndexStatistics] = adapter.get_index_statistics(
        None, target_table
    )

    candidate_columns: list[str] = []
    explain_plan = None
    if sql and adapter.supports(Capability.SQL):
        candidate_columns = _extract_candidate_columns(sql)
        try:
            explain_plan = to_serializable(adapter.explain(sql))
        except Exception as exc:  # noqa: BLE001 — EXPLAIN 실패는 치명적이지 않음
            explain_plan = {"error": str(exc)}

    covered_leading_columns = {
        idx.columns[0].lower() for idx in existing_indexes if idx.columns
    }
    missing_index_candidates = [
        {
            "columns": [column],
            "reason": (
                f"'{column}' 컬럼이 WHERE/JOIN/ORDER BY에서 사용되지만, "
                "이 컬럼을 선두로 하는 Index가 없습니다."
            ),
        }
        for column in candidate_columns
        if column not in covered_leading_columns
    ]

    unused_index_candidates = [
        idx.index_name
        for idx in existing_indexes
        if idx.usage_count is not None and idx.usage_count == 0
    ]

    ddl_suggestion = ""
    if missing_index_candidates:
        missing_columns = [c["columns"][0] for c in missing_index_candidates]
        ddl_suggestion = _build_ddl_suggestion(target_table, missing_columns)

    return {
        "existing_indexes": to_serializable(existing_indexes),
        "missing_index_candidates": missing_index_candidates,
        "unused_index_candidates": unused_index_candidates,
        "ddl_suggestion": ddl_suggestion,
        "explain_plan": explain_plan,
    }
