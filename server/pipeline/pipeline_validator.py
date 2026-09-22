"""validate_pipeline Tool 구현 (DataEngineer).

Tool 시그니처: Doc/01_개발정의서.md §3.4. Dry-run/Sample 검증만 수행하며
Target에 실제로 데이터를 적재하지 않는다 (Doc/00_개발요건사항.md §23).

plan은 generate_pipeline_plan의 출력을 그대로 받는다고 가정한다
(source_container/target_container/column_mapping 필드 필요).
"""
from __future__ import annotations

from typing import Any

from server.database.base import BaseAdapter, Capability

DEFAULT_SAMPLE_LIMIT = 100
MAX_SAMPLE_LIMIT = 1000


def validate_pipeline(
    source_adapter: BaseAdapter,
    target_adapter: BaseAdapter,
    plan: dict[str, Any],
    sample_limit: int = DEFAULT_SAMPLE_LIMIT,
) -> dict[str, Any]:
    """generate_pipeline_plan의 결과(plan)를 Sample 데이터로 Dry-run 검증한다.

    Returns:
        {
          "constraint_violations": [{"column": str, "constraint": str,
                                       "sample_value": Any}],
          "cast_failures": [{"column": str, "count": int}],
          "predicted_target_row_count": int,
        }
    """
    if not source_adapter.supports(Capability.SAMPLE):
        return {"status": "CAPABILITY_NOT_SUPPORTED", "capability": "sample"}

    source_container = plan.get("source_container")
    if not source_container:
        return {"status": "INVALID_PLAN", "reason": "plan.source_container 가 없습니다."}

    sample_limit = min(sample_limit, MAX_SAMPLE_LIMIT)
    sample_rows = source_adapter.sample_rows(None, source_container, sample_limit)

    target_fields_by_name: dict[str, Any] = {}
    target_container = plan.get("target_container")
    if target_container and target_adapter.supports(Capability.SCHEMA):
        target_objects = target_adapter.get_schema({"container": target_container})
        if target_objects:
            target_fields_by_name = {f.name: f for f in target_objects[0].fields}

    constraint_violations: list[dict[str, Any]] = []
    cast_failure_counts: dict[str, int] = {}

    for mapping in plan.get("column_mapping", []):
        source_col = mapping["source_column"]
        target_col = mapping.get("target_column")
        if not target_col:
            continue
        target_field = target_fields_by_name.get(target_col)
        transform = mapping.get("transform") or ""

        for row in sample_rows:
            value = row.get(source_col)

            if value is None and target_field is not None and target_field.is_pk:
                constraint_violations.append(
                    {"column": target_col, "constraint": "NOT NULL (PK)", "sample_value": value}
                )

            if transform.startswith("CAST") and value is not None:
                target_type = target_field.type if target_field else ""
                if not _castable(value, target_type):
                    cast_failure_counts[target_col] = cast_failure_counts.get(target_col, 0) + 1

    predicted_target_row_count = len(sample_rows)
    if source_adapter.supports(Capability.STATS):
        try:
            stats = source_adapter.get_table_statistics(None, source_container)
            if stats.row_count_estimate is not None:
                predicted_target_row_count = stats.row_count_estimate
        except Exception:  # noqa: BLE001 — 통계 조회 실패는 치명적이지 않음
            pass

    return {
        "constraint_violations": constraint_violations,
        "cast_failures": [
            {"column": column, "count": count} for column, count in cast_failure_counts.items()
        ],
        "predicted_target_row_count": predicted_target_row_count,
    }


def _castable(value: Any, target_type: str) -> bool:
    target_type = (target_type or "").lower()
    try:
        if "int" in target_type:
            int(value)
        elif any(t in target_type for t in ("float", "double", "decimal", "numeric")):
            float(value)
        return True
    except (TypeError, ValueError):
        return False
