"""generate_pipeline_plan Tool 구현 (DataEngineer).

Tool 시그니처: Doc/01_개발정의서.md §3.4. 결과는 항상 "설계안"이며, 실제
Pipeline 실행/배포는 이 함수의 책임이 아니다 (Doc/00_개발요건사항.md §22).

주의: 반환값에 `source_container`/`target_container`를 추가로 포함한다
(Tool 명세의 표준 필드는 아니지만, validate_pipeline이 이 값 없이는
어떤 Container를 Dry-run 해야 할지 알 수 없어 파이프라인 연결을 위해 둔다).
"""
from __future__ import annotations

from typing import Any

from server.database.base import BaseAdapter, Capability

_INCREMENTAL_COLUMN_HINTS = ("updated_at", "modified_at", "created_at", "created_time", "update_time")


def generate_pipeline_plan(
    source_adapter: BaseAdapter,
    source: dict[str, str],
    target_adapter: BaseAdapter,
    target: dict[str, str],
) -> dict[str, Any]:
    """Source→Target 컬럼 매핑과 변환/적재 방식 초안을 생성한다.

    Args:
        source: {"type": "table"|"file", "name": str}
        target: {"type": "table", "name": str}
    """
    if not source_adapter.supports(Capability.SCHEMA) or not target_adapter.supports(
        Capability.SCHEMA
    ):
        return {"status": "CAPABILITY_NOT_SUPPORTED", "capability": "schema"}

    source_container = _container_name(source["name"])
    target_container = _container_name(target["name"])

    source_objects = source_adapter.get_schema({"container": source_container})
    target_objects = target_adapter.get_schema({"container": target_container})
    if not source_objects or not target_objects:
        return {"status": "OBJECT_NOT_FOUND"}

    source_fields = {f.name.lower(): f for f in source_objects[0].fields}
    target_fields = {f.name.lower(): f for f in target_objects[0].fields}

    column_mapping = []
    unmapped_columns = []
    for name_lower, field in source_fields.items():
        target_field = target_fields.get(name_lower)
        if target_field is None:
            unmapped_columns.append(field.name)
            continue
        transform = None
        if field.type and target_field.type and field.type.lower() != target_field.type.lower():
            transform = f"CAST({field.name} AS {target_field.type})"
        column_mapping.append(
            {
                "source_column": field.name,
                "target_column": target_field.name,
                "transform": transform,
            }
        )

    incremental_column = next(
        (
            field.name
            for name_lower, field in source_fields.items()
            if any(hint in name_lower for hint in _INCREMENTAL_COLUMN_HINTS)
        ),
        None,
    )
    load_mode = "incremental" if incremental_column else "full"

    return {
        "source_container": source_container,
        "target_container": target_container,
        "column_mapping": column_mapping,
        "unmapped_columns": unmapped_columns,
        "load_mode": load_mode,
        "incremental_column": incremental_column,
        "schedule_suggestion": (
            "매일 02:00 배치 (Incremental)"
            if load_mode == "incremental"
            else "1회성 또는 필요 시 수동 실행 (Full Load)"
        ),
        "error_handling": "매핑 실패/제약조건 위반 레코드는 별도 quarantine 대상으로 격리 후 알림",
    }


def _container_name(name: str) -> str:
    return name.rsplit(".", 1)[-1]
