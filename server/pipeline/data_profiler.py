"""profile_data Tool 구현 (DataEngineer).

Tool 시그니처: Doc/01_개발정의서.md §3.4. Sample 데이터는 최소 건수 제한과
필요 시 마스킹을 반드시 적용한다 (Doc/00_개발요건사항.md §29/§34).
"""
from __future__ import annotations

from typing import Any, Optional

from server.database.base import BaseAdapter, Capability

MAX_SAMPLE_LIMIT = 1000
DEFAULT_SAMPLE_LIMIT = 100

# 컬럼명에 이 문자열이 포함되면 민감 정보로 간주해 sample_rows에서 마스킹한다.
# mask 대상을 사용자가 직접 지정하는 파라미터가 없으므로(Tool 시그니처 §3.4),
# 이름 기반 자동 판별을 1차 정책으로 둔다.
_SENSITIVE_NAME_HINTS = (
    "password", "passwd", "secret", "token", "ssn", "resident", "card_no", "email", "phone",
)


def profile_data(
    adapter: BaseAdapter,
    container: str,
    namespace: Optional[str] = None,
    sample_limit: int = DEFAULT_SAMPLE_LIMIT,
) -> dict[str, Any]:
    """Null 비율/Cardinality/마스킹된 Sample을 반환한다.

    Returns:
        {
          "null_ratio": [{"column": str, "ratio": float}],
          "cardinality": [{"column": str, "distinct_count": int}],
          "sample_rows": dict[],
        }
    """
    if not adapter.supports(Capability.SAMPLE):
        return {"status": "CAPABILITY_NOT_SUPPORTED", "capability": "sample"}

    sample_limit = min(sample_limit, MAX_SAMPLE_LIMIT)
    rows = adapter.sample_rows(namespace, container, sample_limit)

    null_ratio: list[dict[str, Any]] = []
    cardinality: list[dict[str, Any]] = []
    if rows:
        columns: set[str] = set()
        for row in rows:
            columns.update(row.keys())

        total = len(rows)
        for column in sorted(columns):
            values = [row.get(column) for row in rows]
            null_count = sum(1 for v in values if v is None)
            null_ratio.append({"column": column, "ratio": round(null_count / total, 4)})
            cardinality.append(
                {"column": column, "distinct_count": len({_hashable(v) for v in values})}
            )

        masked_columns = {c for c in columns if any(hint in c.lower() for hint in _SENSITIVE_NAME_HINTS)}
        for row in rows:
            for column in masked_columns:
                if column in row:
                    row[column] = "***"

    return {"null_ratio": null_ratio, "cardinality": cardinality, "sample_rows": rows}


def _hashable(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return str(value)
    return value
