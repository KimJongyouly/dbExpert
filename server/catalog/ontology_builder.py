"""build_ontology_index Tool 구현.

Tool 시그니처: Doc/01_개발정의서.md §3.5. Ontology Document Schema는
Doc/01_개발정의서.md §4.2 참고.

SchemaCapable이 있는 Engine은 get_schema()로 바로 Container/Field를
얻지만, Redis처럼 SchemaCapable이 없고 SampleCapable만 있는 Engine은
SCAN 샘플에서 얻은 Key를 정규화해 Container에 준하는 단위로 만든다
(Doc/00_개발요건사항.md §10.1 매핑표).
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime, timezone
from typing import Any

from server.catalog.rag_index import RagIndex
from server.database.base import BaseAdapter, Capability, SchemaField, SchemaObject

# Redis Key 패턴 정규화 — 숫자/UUID/24자리 hex(ObjectId류) 세그먼트를 '*'로 치환한다.
_ID_SEGMENT_RE = re.compile(r"^(?:\d+|[0-9a-fA-F]{24}|[0-9a-fA-F-]{8,})$")
_KEY_SAMPLE_LIMIT = 500


def build_ontology_index(
    connection_name: str, adapter: BaseAdapter, mode: str = "incremental"
) -> dict[str, Any]:
    """Server의 Namespace/Container/Field 메타데이터를 수집해 Vector Index를
    구축/갱신한다.

    Returns: { "indexed_objects": int, "updated": int, "removed": int,
               "duration_ms": float }
    """
    start = time.perf_counter()

    if adapter.supports(Capability.SCHEMA):
        schema_objects = adapter.get_schema({})
    elif adapter.supports(Capability.SAMPLE):
        schema_objects = _infer_key_pattern_containers(adapter)
    else:
        return {"status": "CAPABILITY_NOT_SUPPORTED", "capability": "schema/sample"}

    documents = [to_ontology_document(connection_name, obj) for obj in schema_objects]

    index = RagIndex(connection_name)
    existing_ids = index.all_ids()
    new_ids = {doc["id"] for doc in documents}
    # "full"/"incremental" 모두 결과적으로 "지금 없는 건 지운다"는 동일한 규칙을 쓴다.
    # mode 차이는 향후 스캔 범위(전체 재조회 vs 변경분만 조회)를 구분하기 위한
    # 자리이며, 현재 get_schema({})는 항상 전체를 반환하므로 removed_ids 계산은
    # 두 모드에서 같다 — new_ids에 없는 old id만 지운다(방금 새로 쓴 문서를
    # 실수로 같이 지우지 않도록).
    removed_ids = existing_ids - new_ids

    updated = index.upsert(documents)
    removed = index.delete(list(removed_ids))

    return {
        "indexed_objects": len(documents),
        "updated": updated,
        "removed": removed,
        "duration_ms": round((time.perf_counter() - start) * 1000, 2),
    }


def to_ontology_document(connection_name: str, obj: SchemaObject) -> dict[str, Any]:
    doc_id = f"{connection_name}:{obj.namespace or ''}:{obj.container}"
    fields = [
        {
            "name": f.name,
            "type": f.type,
            "comment": f.comment,
            "is_pk": f.is_pk,
            "is_fk": f.is_fk,
            "fk_ref": f.fk_ref,
            "inferred": f.inferred,
        }
        for f in obj.fields
    ]
    return {
        "id": doc_id,
        "connection": connection_name,
        "namespace": obj.namespace,
        "container": obj.container,
        "container_comment": obj.container_comment,
        "row_count_estimate": obj.row_count_estimate,
        "fields": fields,
        "embedding_text": _build_embedding_text(connection_name, obj),
        "source_hash": _compute_source_hash(obj.container_comment, fields),
        "last_indexed_at": datetime.now(timezone.utc).isoformat(),
    }


def _build_embedding_text(connection_name: str, obj: SchemaObject) -> str:
    parts = [f"[{connection_name}].{obj.container}"]
    if obj.container_comment:
        parts.append(obj.container_comment)
    for field in obj.fields:
        parts.append(f"{field.name}: {field.comment}" if field.comment else field.name)
    return " ".join(parts)


def _compute_source_hash(container_comment: str | None, fields: list[dict[str, Any]]) -> str:
    payload = json.dumps({"comment": container_comment, "fields": fields}, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _normalize_key_pattern(key: str) -> str:
    segments = key.split(":")
    normalized = ["*" if _ID_SEGMENT_RE.match(seg) else seg for seg in segments]
    return ":".join(normalized)


def _infer_key_pattern_containers(adapter: BaseAdapter) -> list[SchemaObject]:
    """Redis류 — 전체 Key 공간을 샘플링해 Key 패턴을 정규화하고, 패턴별 개수와
    (Hash 타입인 경우) Field 목록을 Container/Field처럼 취급한다."""
    samples = adapter.sample_rows(None, "*", _KEY_SAMPLE_LIMIT)

    pattern_counts: dict[str, int] = {}
    pattern_fields: dict[str, set[str]] = {}
    for row in samples:
        key = row.get("key", "")
        pattern = _normalize_key_pattern(key)
        pattern_counts[pattern] = pattern_counts.get(pattern, 0) + 1
        if row.get("type") == "hash" and isinstance(row.get("value"), dict):
            pattern_fields.setdefault(pattern, set()).update(row["value"].keys())

    return [
        SchemaObject(
            namespace=None,
            container=pattern,
            container_comment=None,
            row_count_estimate=count,
            fields=[
                SchemaField(name=name, type="hash_field", inferred=True)
                for name in sorted(pattern_fields.get(pattern, []))
            ],
        )
        for pattern, count in pattern_counts.items()
    ]
