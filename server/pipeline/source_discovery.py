"""list_raw_sources / get_schema Tool 구현 (DataEngineer).

Tool 시그니처: Doc/01_개발정의서.md §3.4.
"""
from __future__ import annotations

from typing import Any, Optional

from server.database.base import BaseAdapter, Capability
from server.utils import to_serializable


def list_raw_sources(adapter: BaseAdapter, scope: str) -> dict[str, Any]:
    """Pipeline Source 후보(Table/Collection/Index/Key 패턴)를 나열한다.

    Args:
        scope: 예) "schema:raw_landing", "database:appdb", 또는 스키마명 그대로

    Returns: { "sources": [{"type": str, "name": str, "row_count": int | None,
                              "size_bytes": int | None}] }
    """
    if not adapter.supports(Capability.SCHEMA):
        return {"status": "CAPABILITY_NOT_SUPPORTED", "capability": "schema"}

    namespace = parse_scope(scope)
    schema_objects = adapter.get_schema({"schema": namespace} if namespace else {})

    sources = []
    for obj in schema_objects:
        row_count = obj.row_count_estimate
        if row_count is None and adapter.supports(Capability.STATS):
            try:
                stats = adapter.get_table_statistics(obj.namespace, obj.container)
                row_count = stats.row_count_estimate
            except Exception:  # noqa: BLE001 — 통계 조회 실패는 치명적이지 않음
                row_count = None

        name = f"{obj.namespace}.{obj.container}" if obj.namespace else obj.container
        sources.append({"type": "table", "name": name, "row_count": row_count, "size_bytes": None})

    return {"sources": sources}


def get_schema(adapter: BaseAdapter, scope: str) -> dict[str, Any]:
    """Namespace/Container/Field 목록(comment 포함)을 조회한다.

    ontology_builder.build_ontology_index가 쓰는 내부 함수(adapter.get_schema)와
    동일하지만, Skill 외에서 직접 "이 스키마 구조 보여줘"에 응답하기 위한
    독립 Tool이다.

    Args:
        scope: list_raw_sources와 동일한 형식. 예) "schema:raw_landing"

    Returns: { "objects": SchemaObject[] }
    """
    if not adapter.supports(Capability.SCHEMA):
        return {"status": "CAPABILITY_NOT_SUPPORTED", "capability": "schema"}

    namespace = parse_scope(scope)
    schema_objects = adapter.get_schema({"schema": namespace} if namespace else {})
    return {"objects": to_serializable(schema_objects)}


def parse_scope(scope: str) -> Optional[str]:
    if ":" in scope:
        prefix, _, value = scope.partition(":")
        if prefix.lower() in {"schema", "database", "namespace"}:
            return value or None
    return scope or None
