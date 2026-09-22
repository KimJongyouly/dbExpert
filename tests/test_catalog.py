"""catalog/*.py(Ontology/RAG/ERD)에 대한 단위 테스트.

RagIndex는 LanceDB(파일 기반) 위에서 동작하므로 DATA_DIR을 tmp_path로
monkeypatch해서 실제 data/ 디렉터리를 건드리지 않는다. lancedb가 설치되어
있지 않은 환경(ontology extras 미설치)에서는 이 파일 전체를 건너뛴다.

Embedding은 기본적으로 결정적인 fake 함수로 대체한다(무거운
sentence-transformers 모델을 매번 로드하지 않기 위해) — 실제 모델을 써서
의미 검색 품질까지 검증하는 테스트는 @pytest.mark.real_embeddings 로 표시하고
sentence_transformers가 없으면 건너뛴다.
"""
from __future__ import annotations

import hashlib
import re

import pytest

pytest.importorskip("lancedb")

import server.catalog.rag_index as rag_index_module
from server.catalog.embeddings import EMBEDDING_DIM
from server.catalog.erd_builder import get_table_relationships
from server.catalog.object_search import search_object_catalog
from server.catalog.ontology_builder import build_ontology_index
from server.catalog.rag_index import RagIndex
from server.database.base import (
    BaseAdapter,
    Capability,
    Relationship,
    RelationshipCapable,
    SampleCapable,
    SchemaCapable,
    SchemaField,
    SchemaObject,
)

_TOKEN_RE = re.compile(r"[A-Za-z0-9가-힣_]+")


def _fake_embed(texts: list[str]) -> list[list[float]]:
    """실제 모델과 인터페이스(차원/L2 정규화)는 같지만, 토큰 해싱만으로
    만드는 가벼운 대역 임베딩 — 공유 토큰이 많을수록 코사인 유사도가
    높아지므로 기존 어휘 기반 테스트 기대값을 그대로 재사용할 수 있다."""
    vectors = []
    for text in texts:
        vec = [0.0] * EMBEDDING_DIM
        for token in _TOKEN_RE.findall(text.lower()):
            idx = int(hashlib.md5(token.encode("utf-8")).hexdigest(), 16) % EMBEDDING_DIM
            vec[idx] += 1.0
        norm = sum(v * v for v in vec) ** 0.5
        if norm > 0:
            vec = [v / norm for v in vec]
        vectors.append(vec)
    return vectors


@pytest.fixture(autouse=True)
def _use_fake_embeddings(request, monkeypatch):
    if "real_embeddings" in request.keywords:
        return  # 이 테스트는 실제 sentence-transformers 모델을 그대로 쓴다
    monkeypatch.setattr(rag_index_module, "embed", _fake_embed)


class _FakeSchemaAdapter(BaseAdapter, SchemaCapable):
    capabilities = {Capability.SCHEMA}

    def __init__(self, schema_objects, name="test-conn"):
        super().__init__({"connection": {}, "name": name})
        self._schema_objects = schema_objects

    def ping(self):
        return {"connected": True}

    def get_schema(self, scope):
        container = scope.get("container")
        schema = scope.get("schema")
        objects = self._schema_objects
        if container:
            objects = [o for o in objects if o.container == container]
        if schema:
            objects = [o for o in objects if o.namespace == schema]
        return objects


class _FakeRdbAdapter(_FakeSchemaAdapter, RelationshipCapable):
    capabilities = {Capability.SCHEMA, Capability.RELATIONSHIP}

    def __init__(self, schema_objects, relationships):
        super().__init__(schema_objects)
        self._relationships = relationships

    def get_relationships(self, containers):
        return [r for r in self._relationships if r.from_container in containers]


class _FakeRedisAdapter(BaseAdapter, SampleCapable):
    capabilities = {Capability.SAMPLE}

    def __init__(self, rows):
        super().__init__({"connection": {}})
        self._rows = rows

    def ping(self):
        return {"connected": True}

    def sample_rows(self, namespace, container, limit, mask=None):
        return self._rows[:limit]


def _orders_schema():
    return [
        SchemaObject(
            namespace="public",
            container="orders",
            container_comment="주문 정보",
            fields=[
                SchemaField(name="id", type="bigint", is_pk=True),
                SchemaField(name="customer_id", type="bigint", comment="고객 ID"),
            ],
        ),
        SchemaObject(
            namespace="public",
            container="customers",
            container_comment="고객 정보",
            fields=[SchemaField(name="id", type="bigint", is_pk=True)],
        ),
    ]


# ---------------------------------------------------------------------------
# RagIndex
# ---------------------------------------------------------------------------


def test_rag_index_upsert_search_delete(tmp_path, monkeypatch):
    monkeypatch.setattr(rag_index_module, "DATA_DIR", tmp_path)
    index = RagIndex("test-conn")

    upserted = index.upsert(
        [
            {
                "id": "test-conn:public:orders",
                "container": "orders",
                "namespace": "public",
                "container_comment": "주문 정보",
                "fields": [{"name": "customer_id", "comment": "고객 ID"}],
                "embedding_text": "[test-conn].orders 주문 정보 customer_id: 고객 ID",
                "source_hash": "abc",
            }
        ]
    )
    assert upserted == 1
    assert index.all_ids() == {"test-conn:public:orders"}

    # 동일 source_hash로 내용까지 동일하게 재upsert하면 "변경"으로 세지 않는다.
    reupserted = index.upsert(
        [
            {
                "id": "test-conn:public:orders",
                "container": "orders",
                "namespace": "public",
                "container_comment": "주문 정보",
                "fields": [{"name": "customer_id", "comment": "고객 ID"}],
                "embedding_text": "[test-conn].orders 주문 정보 customer_id: 고객 ID",
                "source_hash": "abc",
            }
        ]
    )
    assert reupserted == 0

    matches = index.search("주문")
    assert matches and matches[0]["container"] == "orders"

    removed = index.delete(["test-conn:public:orders"])
    assert removed == 1
    assert index.all_ids() == set()


# ---------------------------------------------------------------------------
# build_ontology_index + search_object_catalog (연결해서 검증)
# ---------------------------------------------------------------------------


def test_build_ontology_index_then_search(tmp_path, monkeypatch):
    monkeypatch.setattr(rag_index_module, "DATA_DIR", tmp_path)
    adapter = _FakeSchemaAdapter(_orders_schema())

    result = build_ontology_index("test-conn", adapter)
    assert result["indexed_objects"] == 2
    assert result["updated"] == 2
    assert result["removed"] == 0

    search_result = search_object_catalog("test-conn", "고객 주문")
    tables = {m["table"] for m in search_result["matches"]}
    assert "orders" in tables


def test_search_object_catalog_fills_index_first_when_empty(tmp_path, monkeypatch):
    """build_ontology_index를 아직 한 번도 안 돌린 Connection(최초 사용,
    Daily 배치 전)에서 search_object_catalog을 호출하면, 빈 인덱스로 검색을
    시도하는 대신 먼저 채우고 나서 검색해야 한다."""
    monkeypatch.setattr(rag_index_module, "DATA_DIR", tmp_path)
    adapter = _FakeSchemaAdapter(_orders_schema())

    assert RagIndex("test-conn").is_empty() is True

    result = search_object_catalog("test-conn", "고객 주문", adapter=adapter)

    assert result["index_was_empty_and_rebuilt"] is True
    tables = {m["table"] for m in result["matches"]}
    assert "orders" in tables
    assert RagIndex("test-conn").is_empty() is False


def test_search_object_catalog_does_not_refill_when_index_already_has_documents(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(rag_index_module, "DATA_DIR", tmp_path)
    adapter = _FakeSchemaAdapter(_orders_schema())
    build_ontology_index("test-conn", adapter)

    # adapter가 더 이상 스키마를 제공하지 못해도(예: 연결 끊김) 인덱스가
    # 이미 채워져 있으면 재빌드를 시도하지 않아야 한다.
    class _BrokenAdapter(BaseAdapter, SchemaCapable):
        capabilities = {Capability.SCHEMA}

        def ping(self):
            return {"connected": True}

        def get_schema(self, scope):
            raise RuntimeError("should not be called")

    result = search_object_catalog("test-conn", "고객 주문", adapter=_BrokenAdapter({}))
    assert result["index_was_empty_and_rebuilt"] is False
    tables = {m["table"] for m in result["matches"]}
    assert "orders" in tables


def test_search_object_catalog_without_adapter_skips_auto_fill(tmp_path, monkeypatch):
    monkeypatch.setattr(rag_index_module, "DATA_DIR", tmp_path)

    result = search_object_catalog("test-conn", "고객 주문")  # adapter 생략

    assert result["index_was_empty_and_rebuilt"] is False
    assert result["matches"] == []


def test_build_ontology_index_removes_stale_documents_in_full_mode(tmp_path, monkeypatch):
    monkeypatch.setattr(rag_index_module, "DATA_DIR", tmp_path)
    adapter = _FakeSchemaAdapter(_orders_schema())
    build_ontology_index("test-conn", adapter, mode="full")

    adapter_after_drop = _FakeSchemaAdapter(_orders_schema()[:1])  # customers 테이블 삭제됨
    result = build_ontology_index("test-conn", adapter_after_drop, mode="full")
    assert result["removed"] == 1


def test_build_ontology_index_capability_not_supported():
    class _Empty(BaseAdapter):
        capabilities: set[Capability] = set()

        def ping(self):
            return {"connected": True}

    result = build_ontology_index("test-conn", _Empty({}))
    assert result["status"] == "CAPABILITY_NOT_SUPPORTED"


def test_build_ontology_index_infers_redis_key_patterns(tmp_path, monkeypatch):
    monkeypatch.setattr(rag_index_module, "DATA_DIR", tmp_path)
    adapter = _FakeRedisAdapter(
        rows=[
            {"key": "user:123:session", "type": "string", "value": "x"},
            {"key": "user:456:session", "type": "string", "value": "y"},
            {"key": "user:789:profile", "type": "hash", "value": {"name": "a", "age": "20"}},
        ]
    )
    result = build_ontology_index("redis-conn", adapter)
    assert result["indexed_objects"] == 2  # user:*:session, user:*:profile


# ---------------------------------------------------------------------------
# get_table_relationships
# ---------------------------------------------------------------------------


def test_get_table_relationships_merges_declared_and_inferred():
    """RDB인데 일부 컬럼에만 FK 제약이 걸려 있는 경우 — declared로 커버되지
    않는 컬럼(warehouse_cd)은 inferred로 보강되고, 이미 declared로 커버된
    컬럼(customer_id)은 중복으로 다시 추정되지 않아야 한다."""
    tables = [
        SchemaObject(
            container="orders",
            fields=[
                SchemaField(name="id", type="bigint", is_pk=True),
                SchemaField(name="customer_id", type="bigint"),
                SchemaField(name="warehouse_cd", type="varchar"),  # FK 미선언
            ],
        ),
        SchemaObject(
            container="customers",
            fields=[SchemaField(name="id", type="bigint", is_pk=True)],
        ),
        SchemaObject(
            container="warehouses",
            fields=[SchemaField(name="warehouse_cd", type="varchar", is_pk=True)],
        ),
    ]
    adapter = _FakeRdbAdapter(
        tables,
        relationships=[
            Relationship(
                from_container="orders",
                from_field="customer_id",
                to_container="customers",
                to_field="id",
                confidence="declared",
            )
        ],
    )
    result = get_table_relationships(
        adapter, "rdb", {"tables": ["orders", "customers", "warehouses"]}
    )

    by_column = {r["from_column"]: r for r in result["relationships"]}
    assert len(result["relationships"]) == 2
    assert by_column["customer_id"]["confidence"] == "declared"
    assert by_column["warehouse_cd"]["confidence"] == "inferred"
    assert by_column["warehouse_cd"]["to_table"] == "warehouses"


def test_get_table_relationships_declared_fk():
    adapter = _FakeRdbAdapter(
        _orders_schema(),
        relationships=[
            Relationship(
                from_container="orders",
                from_field="customer_id",
                to_container="customers",
                to_field="id",
                confidence="declared",
            )
        ],
    )
    result = get_table_relationships(adapter, "rdb", {"tables": ["orders", "customers"]})
    assert len(result["tables"]) == 2
    assert result["relationships"][0]["confidence"] == "declared"


def test_get_table_relationships_infers_by_naming_when_no_relationship_capability():
    adapter = _FakeSchemaAdapter(_orders_schema())  # RELATIONSHIP Capability 없음
    result = get_table_relationships(adapter, "document", {"tables": ["orders", "customers"]})
    assert result["relationships"] == [
        {
            "from_table": "orders",
            "from_column": "customer_id",
            "to_table": "customers",
            "to_column": "id",
            "cardinality": "1:N",
            "confidence": "inferred",
            "inferred_by": "name",
        }
    ]


def test_get_table_relationships_search_family_skips_inference():
    adapter = _FakeSchemaAdapter(_orders_schema())
    result = get_table_relationships(adapter, "search", {"tables": ["orders", "customers"]})
    assert result["relationships"] == []


def test_get_table_relationships_infers_via_reused_pk_name():
    """국내 스키마에서 흔한 패턴 — FK 컬럼이 참조 대상 Table의 PK와 이름이
    완전히 같은 경우(`complex_cd`가 tb_complex_master의 PK이자
    tb_complex_area에도 그대로 쓰이는 패턴). 접미사 매칭보다 우선한다."""
    tables = [
        SchemaObject(
            container="tb_complex_master",
            container_comment="아파트 단지 마스터",
            fields=[
                SchemaField(name="complex_cd", type="varchar", is_pk=True, comment="단지 코드"),
                SchemaField(name="complex_nm", type="varchar", comment="단지명"),
            ],
        ),
        SchemaObject(
            container="tb_complex_area",
            container_comment="단지 평형 정보",
            fields=[
                SchemaField(name="complex_cd", type="varchar", comment="단지 코드"),
                SchemaField(name="hh_cnt", type="int", comment="세대수"),
            ],
        ),
    ]
    adapter = _FakeSchemaAdapter(tables)
    result = get_table_relationships(
        adapter, "document", {"tables": ["tb_complex_master", "tb_complex_area"]}
    )
    rel = result["relationships"][0]
    assert rel["from_table"] == "tb_complex_area"
    assert rel["from_column"] == "complex_cd"
    assert rel["to_table"] == "tb_complex_master"
    assert rel["to_column"] == "complex_cd"
    assert rel["confidence"] == "inferred"
    assert rel["inferred_by"] == "name"


def test_get_table_relationships_infers_via_comment_when_name_does_not_match():
    """컬럼명만으로는 매칭이 안 되지만 comment가 다른 Table을 강하게
    가리키는 경우 — comment 토큰 overlap으로 약하게 추정한다."""
    tables = [
        SchemaObject(
            container="tb_complex_master",
            container_comment="아파트 단지 마스터 정보",
            fields=[SchemaField(name="complex_cd", type="varchar", is_pk=True)],
        ),
        SchemaObject(
            container="rtms_apt_trade",
            container_comment="아파트 실거래 내역",
            fields=[
                # 컬럼명은 전혀 다르지만 comment가 "단지 마스터"를 직접 언급
                SchemaField(name="target_ref", type="varchar", comment="단지 마스터 참조값"),
            ],
        ),
    ]
    adapter = _FakeSchemaAdapter(tables)
    result = get_table_relationships(
        adapter, "document", {"tables": ["tb_complex_master", "rtms_apt_trade"]}
    )
    rel = result["relationships"][0]
    assert rel["from_table"] == "rtms_apt_trade"
    assert rel["to_table"] == "tb_complex_master"
    assert rel["confidence"] == "inferred"
    assert rel["inferred_by"] == "comment"


def test_get_table_relationships_capability_not_supported():
    class _Empty(BaseAdapter):
        capabilities: set[Capability] = set()

        def ping(self):
            return {"connected": True}

    result = get_table_relationships(_Empty({}), "rdb", {"tables": ["orders"]})
    assert result["status"] == "CAPABILITY_NOT_SUPPORTED"


# ---------------------------------------------------------------------------
# 실제 Embedding Provider(sentence-transformers)를 쓰는 의미 검색 품질 테스트.
# 느리고(모델 로드) 무거운 optional dependency가 필요해 별도로 마킹한다.
# ---------------------------------------------------------------------------


@pytest.mark.real_embeddings
def test_search_object_catalog_semantic_match_without_shared_keywords(tmp_path, monkeypatch):
    """키워드가 하나도 겹치지 않아도 의미가 통하면 찾아져야 한다 —
    순수 토큰 overlap으로는 불가능했던 것을 실제 임베딩으로 검증한다."""
    pytest.importorskip("sentence_transformers")
    monkeypatch.setattr(rag_index_module, "DATA_DIR", tmp_path)

    adapter = _FakeSchemaAdapter(
        [
            SchemaObject(
                container="rtms_apt_trade",
                container_comment="아파트 실거래 매매 계약 내역",
                fields=[SchemaField(name="deal_amount", type="numeric", comment="거래금액")],
            ),
            SchemaObject(
                container="tb_facility_medical",
                container_comment="병원, 약국 등 의료시설 위치 정보",
                fields=[],
            ),
        ],
        name="real-embed-conn",
    )
    build_ontology_index("real-embed-conn", adapter)

    result = search_object_catalog("real-embed-conn", "집 매매 가격")
    assert result["matches"][0]["table"] == "rtms_apt_trade"
