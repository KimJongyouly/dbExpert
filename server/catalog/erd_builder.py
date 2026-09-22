"""get_table_relationships Tool 구현.

ERD 생성은 3단계로 이뤄진다 (Doc/00_개발요건사항.md §10.3/§10.4).
  1. 주제어(키워드)가 주어지면 Table/Column comment를 참고해 대상 Table을
     찾는다 — `scope.related_to` → search_object_catalog로 위임.
  1.1. 이미 확정된 Table 목록이 주어지면(`scope.tables`) 1번을 건너뛰고
       바로 2번으로 간다.
  2. PK/FK가 선언되어 있으면(RELATIONSHIP Capability) 그걸로 관계를 그린다.
  3. FK가 없으면(또는 declared가 일부 컬럼을 못 덮으면) 컬럼명(PK 이름 일치,
     `_id`/`_cd`류 접미사)과 컬럼/테이블 comment를 근거로 관계를 추정해서
     declared에 병합(merge)한다.

Mermaid 변환 자체는 이 모듈이 아니라 Claude가 수행한다(§10.4) — 이 모듈은
관계 데이터(tables/relationships)만 만든다.

정책 (declared + inferred 병합):
  - RELATIONSHIP Capability가 있는 Adapter(RDB 계열)의 선언 FK는 항상
    confidence="declared"로 포함된다.
  - declared가 커버하지 못하는 (from_table, from_column)에 대해서는,
    engine_family가 {rdb, dw, document}인 경우 3번 방식으로 confidence=
    "inferred" 관계를 추가로 추정해 declared 뒤에 병합한다. RDB인데 일부
    컬럼에만 FK 제약이 걸려 있는 경우에도 나머지 컬럼은 inferred로 보강된다.
  - RELATIONSHIP Capability 자체가 없는 Engine(ClickHouse/MongoDB 등)은
    declared가 항상 빈 배열이므로 결과적으로 inferred만 남는다.
  - engine_family가 search/keyvalue인 경우는 추정도 하지 않고 declared만
    (보통 비어 있음) 반환한다.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from server.catalog.object_search import search_object_catalog
from server.database.base import BaseAdapter, Capability

_INFERENCE_ELIGIBLE_FAMILIES = {"rdb", "dw", "document"}
# 흔한 FK 접미사. `_cd`(코드)는 국내 스키마에서 `_id`만큼 흔히 쓰인다
# (예: complex_cd가 tb_complex_master의 PK이자 다른 테이블의 FK로 그대로 재사용됨).
_FK_NAME_SUFFIXES = ("_id", "_cd", "_code", "_no")
_TOKEN_RE = re.compile(r"[A-Za-z0-9가-힣_]+")
# 컬럼 comment와 대상 Table comment 간 겹치는 토큰이 이 값 미만이면 매칭하지
# 않는다 — 단어 1개만 겹치는 경우 오탐이 많다(예: "총 세대수" vs "세대수").
_MIN_COMMENT_OVERLAP = 2
_DEFAULT_SCHEMA_SCAN_LIMIT = 20
_RELATED_TO_EXPANSION_TOP_K = 10


def get_table_relationships(
    adapter: BaseAdapter, engine_family: str, scope: dict[str, Any]
) -> dict[str, Any]:
    """대상 Table/Container 집합의 컬럼 구조와 관계(declared/inferred)를 반환한다.

    Args:
        scope: {"tables": [...]} | {"related_to": "..."} | {"schema": "...", "limit": 20}

    Returns:
        {
          "tables": [{"schema": str, "table": str, "comment": str | None,
                       "columns": [{"name": str, "type": str, "is_pk": bool,
                                     "comment": str | None}]}],
          "relationships": [{"from_table": str, "from_column": str,
                               "to_table": str, "to_column": str,
                               "cardinality": str, "confidence": str}],
        }
    """
    if not adapter.supports(Capability.SCHEMA):
        return {"status": "CAPABILITY_NOT_SUPPORTED", "capability": "schema"}

    containers = _resolve_scope(adapter, scope)
    if not containers:
        return {"tables": [], "relationships": []}

    tables: list[dict[str, Any]] = []
    for container in containers:
        for obj in adapter.get_schema({"container": container}):
            tables.append(
                {
                    "schema": obj.namespace,
                    "table": obj.container,
                    "comment": obj.container_comment,
                    "columns": [
                        {
                            "name": f.name,
                            "type": f.type,
                            "is_pk": f.is_pk,
                            "comment": f.comment,
                        }
                        for f in obj.fields
                    ],
                }
            )

    declared: list[dict[str, Any]] = []
    if adapter.supports(Capability.RELATIONSHIP):
        declared = [
            {
                "from_table": r.from_container,
                "from_column": r.from_field,
                "to_table": r.to_container,
                "to_column": r.to_field,
                "cardinality": r.cardinality,
                "confidence": r.confidence,
            }
            for r in adapter.get_relationships(containers)
        ]

    # 병합: declared가 있어도 "그 Table 집합에 대해 선언된 FK가 전부"라는
    # 보장은 없다(예: RDB인데 특정 컬럼엔 FK 제약을 걸지 않은 경우) — declared로
    # 이미 커버된 (from_table, from_column)만 제외하고, 나머지는 inferred로
    # 보강한다. RELATIONSHIP Capability 자체가 없는 Engine(dw/document)은
    # declared가 항상 비어 있으므로 결과적으로 inferred만 남는다.
    inferred: list[dict[str, Any]] = []
    if engine_family in _INFERENCE_ELIGIBLE_FAMILIES:
        declared_columns = {(r["from_table"], r["from_column"]) for r in declared}
        inferred = [
            r
            for r in _infer_relationships(tables)
            if (r["from_table"], r["from_column"]) not in declared_columns
        ]

    return {"tables": tables, "relationships": declared + inferred}


def _resolve_scope(adapter: BaseAdapter, scope: dict[str, Any]) -> list[str]:
    if scope.get("tables"):
        return list(scope["tables"])

    if scope.get("related_to"):
        connection_name = adapter.connection_profile.get("name", "")
        result = search_object_catalog(
            connection_name, scope["related_to"], top_k=_RELATED_TO_EXPANSION_TOP_K
        )
        return [m["table"] for m in result["matches"] if m.get("table")]

    if scope.get("schema"):
        limit = scope.get("limit", _DEFAULT_SCHEMA_SCAN_LIMIT)
        objects = adapter.get_schema({"schema": scope["schema"]})
        return [obj.container for obj in objects[:limit]]

    return []


def _infer_relationships(tables: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """FK가 없을 때 컬럼명과 comment를 근거로 관계를 추정한다 (3단계).

    우선순위:
      1. 다른 Table의 PK 컬럼과 이름이 완전히 같은 컬럼
         (예: `complex_cd`가 tb_complex_master의 PK이면서 다른 테이블에도
         그대로 재사용되는, 국내 스키마에서 흔한 "PK명 그대로 재사용" 패턴)
      2. 흔한 FK 접미사(`_id`/`_cd`/`_code`/`_no`)를 뗀 이름이 다른 Table명과
         맞아떨어지는 경우
      3. 위 둘 다 실패하고 컬럼에 comment가 있으면, 그 comment와 다른
         Table의 comment(테이블+컬럼)를 토큰 단위로 비교해 겹치는 게 가장
         많은 Table을 약하게 추정한다

    모든 결과는 confidence="inferred"로 표시한다 — 이름/코멘트 기반 추정일
    뿐이므로, 실제 관계가 맞는지는 Claude가 사용자에게 확인을 권장해야 한다.
    """
    table_names = {t["table"] for t in tables}
    pk_column_index: dict[str, str] = {}  # 컬럼명(소문자) -> 그 컬럼이 PK인 Table명
    pk_column_by_table: dict[str, Optional[str]] = {}
    comment_tokens_by_table: dict[str, set[str]] = {}

    for table in tables:
        pk_name = None
        comment_text = [table.get("comment") or ""]
        for column in table["columns"]:
            if column["is_pk"]:
                pk_column_index[column["name"].lower()] = table["table"]
                pk_name = column["name"]
            if column.get("comment"):
                comment_text.append(column["comment"])
        pk_column_by_table[table["table"]] = pk_name
        comment_tokens_by_table[table["table"]] = _tokenize(" ".join(comment_text))

    relationships: list[dict[str, Any]] = []
    seen_pairs: set[tuple[str, str, str]] = set()

    for table in tables:
        for column in table["columns"]:
            if column["is_pk"]:
                continue
            name_lower = column["name"].lower()

            target = pk_column_index.get(name_lower)  # 1) PK명과 완전 일치
            if not target:
                target = _match_by_suffix(name_lower, table["table"], table_names)  # 2) 접미사 제거 매칭
            confidence_basis = "name"

            if not target and column.get("comment"):
                target = _match_by_comment(
                    column["comment"], table["table"], comment_tokens_by_table
                )
                confidence_basis = "comment"

            if not target or target == table["table"]:
                continue

            key = (table["table"], column["name"], target)
            if key in seen_pairs:
                continue
            seen_pairs.add(key)

            relationships.append(
                {
                    "from_table": table["table"],
                    "from_column": column["name"],
                    "to_table": target,
                    "to_column": pk_column_by_table.get(target) or "id",
                    "cardinality": "1:N",
                    "confidence": "inferred",
                    "inferred_by": confidence_basis,  # 참고용 부가 정보 (name | comment)
                }
            )
    return relationships


def _match_by_suffix(name_lower: str, from_table: str, table_names: set[str]) -> Optional[str]:
    for suffix in _FK_NAME_SUFFIXES:
        if not name_lower.endswith(suffix):
            continue
        candidate = name_lower[: -len(suffix)]
        target = _find_matching_table(candidate, table_names)
        if target and target != from_table:
            return target
    return None


def _find_matching_table(candidate: str, table_names: set[str]) -> Optional[str]:
    if candidate in table_names:
        return candidate
    for name in table_names:
        if name == f"{candidate}s" or name.endswith(f"_{candidate}"):
            return name
    return None


def _match_by_comment(
    column_comment: str, from_table: str, comment_tokens_by_table: dict[str, set[str]]
) -> Optional[str]:
    """comment 토큰 overlap이 `_MIN_COMMENT_OVERLAP`개 이상일 때만 추정한다.

    1개 단어(예: 컬럼 comment "세대수"가 우연히 다른 테이블 comment에도
    등장)만으로 매칭하면 관계없는 컬럼끼리 잘못 엮이는 오탐이 많이
    나온다 — 실제로 tot_hh_cnt("총 세대수")가 hh_cnt("세대수")를 가진
    테이블과 잘못 엮인 사례로 확인되어 임계값을 2로 올렸다.
    """
    column_tokens = _tokenize(column_comment)
    if len(column_tokens) < _MIN_COMMENT_OVERLAP:
        return None

    best_table, best_overlap = None, _MIN_COMMENT_OVERLAP - 1
    for table_name, table_tokens in comment_tokens_by_table.items():
        if table_name == from_table:
            continue
        overlap = len(column_tokens & table_tokens)
        if overlap > best_overlap:
            best_overlap, best_table = overlap, table_name
    return best_table


def _tokenize(text: str) -> set[str]:
    return {t.lower() for t in _TOKEN_RE.findall(text or "")}
