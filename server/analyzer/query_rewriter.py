"""query_rewrite_suggest Tool 구현 (Tuner).

Tool 시그니처: Doc/01_개발정의서.md §3.3. SQL Capability가 없는 Engine
(MongoDB/Elasticsearch/Redis)에서는 CAPABILITY_NOT_SUPPORTED를 반환한다.

이 함수는 기계적으로 탐지 가능한 패턴과 실행계획까지만 구조화한다.
"suggested_sql"/"expected_effect"의 실제 문구는 Claude가 explain_plan과
issues를 종합해서 작성한다(Doc/00_개발요건사항.md §17).
"""
from __future__ import annotations

import re
from typing import Any

from server.database.base import BaseAdapter, Capability
from server.utils import to_serializable

# 탐지 대상 패턴 (Doc/00_개발요건사항.md §17)
_PATTERN_CHECKS: list[tuple[str, re.Pattern[str]]] = [
    ("select_star", re.compile(r"\bSELECT\s+\*", re.IGNORECASE)),
    (
        "function_on_column",
        re.compile(r"\b(?:WHERE|AND|OR)\s+\w+\s*\(", re.IGNORECASE),
    ),
    ("or_condition", re.compile(r"\bWHERE\b.*\bOR\b", re.IGNORECASE | re.DOTALL)),
    ("unnecessary_distinct", re.compile(r"\bDISTINCT\b", re.IGNORECASE)),
]

_PATTERN_DESCRIPTIONS: dict[str, str] = {
    "select_star": (
        "SELECT * 를 사용하고 있습니다 — 필요한 컬럼만 명시하면 "
        "I/O와 네트워크 전송량을 줄일 수 있습니다."
    ),
    "function_on_column": (
        "컬럼에 함수를 적용한 조건이 있습니다 — 해당 컬럼의 Index를 "
        "타지 못할 수 있으니 함수 없이 비교하는 형태로 바꿀 수 있는지 검토가 필요합니다."
    ),
    "or_condition": (
        "WHERE 절에 OR 조건이 있습니다 — UNION으로 분리하면 각 조건에 대해 "
        "Index를 더 잘 활용할 수 있는 경우가 있습니다."
    ),
    "unnecessary_distinct": (
        "DISTINCT가 사용되었습니다 — 실제로 중복 제거가 필요한 상황인지, "
        "혹은 JOIN 구조상 불필요하게 중복이 생겨 DISTINCT로 덮고 있는 것은 아닌지 확인이 필요합니다."
    ),
}


def query_rewrite_suggest(adapter: BaseAdapter, sql: str) -> dict[str, Any]:
    """비효율 SQL 패턴을 탐지하고, 실행계획과 함께 근거 데이터를 반환한다.

    Returns:
        {
          "issues": [{"pattern": str, "description": str}],
          "explain_plan": dict | None,
          "suggested_sql": str,   # Claude가 채워 넣을 자리 — 이 함수는 빈 문자열 반환
          "expected_effect": str, # 위와 동일
        }
    """
    if not adapter.supports(Capability.SQL):
        return {"status": "CAPABILITY_NOT_SUPPORTED", "capability": "sql"}

    issues = [
        {"pattern": name, "description": _PATTERN_DESCRIPTIONS[name]}
        for name, pattern_re in _PATTERN_CHECKS
        if pattern_re.search(sql)
    ]

    try:
        explain_plan = to_serializable(adapter.explain(sql))
    except Exception as exc:  # noqa: BLE001 — EXPLAIN 실패는 치명적이지 않음
        explain_plan = {"error": str(exc)}

    return {
        "issues": issues,
        "explain_plan": explain_plan,
        "suggested_sql": "",
        "expected_effect": "",
    }
