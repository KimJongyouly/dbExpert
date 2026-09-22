"""쿼리 트리거 기반 Ontology 자동 갱신(auto_refresh.py) 단위 테스트.

"당일 최초 1회만" 규칙과 "실패해도 원 호출에 영향 없음"을 실제 LanceDB
(fake embedding으로 가볍게) + 임시 디렉터리로 검증한다.
"""
from __future__ import annotations

import json

import pytest

pytest.importorskip("lancedb")

import server.catalog.auto_refresh as auto_refresh_module
import server.catalog.rag_index as rag_index_module
from server.catalog.auto_refresh import refresh_stale_tables
from server.catalog.embeddings import EMBEDDING_DIM
from server.catalog.rag_index import RagIndex
from server.database.base import BaseAdapter, Capability, SchemaCapable, SchemaObject


def _fake_embed(texts: list[str]) -> list[list[float]]:
    return [[0.0] * EMBEDDING_DIM for _ in texts]


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(rag_index_module, "embed", _fake_embed)
    monkeypatch.setattr(rag_index_module, "DATA_DIR", tmp_path / "ontology_index")
    monkeypatch.setattr(auto_refresh_module, "DATA_DIR", tmp_path / "ontology_index")
    # 테스트끼리 lock 딕셔너리를 공유하면 순서에 따라 죽은 락이 남을 수 있어 초기화한다.
    auto_refresh_module._connection_locks.clear()


class _FakeAdapter(BaseAdapter, SchemaCapable):
    capabilities = {Capability.SCHEMA}

    def __init__(self, schema_objects_by_table, fail_for=None):
        super().__init__({"connection": {}})
        self._by_table = schema_objects_by_table
        self._fail_for = fail_for or set()
        self.get_schema_calls: list[str] = []

    def ping(self):
        return {"connected": True}

    def get_schema(self, scope):
        container = scope.get("container")
        self.get_schema_calls.append(container)
        if container in self._fail_for:
            raise RuntimeError("boom")
        obj = self._by_table.get(container)
        return [obj] if obj else []


class _NoSchemaAdapter(BaseAdapter):
    capabilities: set[Capability] = set()

    def ping(self):
        return {"connected": True}


def test_returns_none_and_does_nothing_when_capability_not_supported():
    adapter = _NoSchemaAdapter({})
    result = refresh_stale_tables("conn1", adapter, ["orders"])
    assert result is None


def test_returns_none_when_no_table_names():
    adapter = _FakeAdapter({})
    assert refresh_stale_tables("conn1", adapter, []) is None
    assert refresh_stale_tables("conn1", adapter, [None, ""]) is None


def test_refreshes_table_and_upserts_into_ontology_index():
    adapter = _FakeAdapter({"orders": SchemaObject(container="orders", container_comment="주문")})
    thread = refresh_stale_tables("conn1", adapter, ["orders"])
    assert thread is not None
    thread.join(timeout=5)

    index = RagIndex("conn1")
    ids = index.all_ids()
    assert any(doc_id.endswith(":orders") for doc_id in ids)


def test_second_call_same_day_skips_already_refreshed_table():
    adapter = _FakeAdapter({"orders": SchemaObject(container="orders")})

    thread1 = refresh_stale_tables("conn1", adapter, ["orders"])
    thread1.join(timeout=5)
    assert adapter.get_schema_calls == ["orders"]

    thread2 = refresh_stale_tables("conn1", adapter, ["orders"])
    # 오늘 이미 갱신했으므로 추가로 스레드를 띄우긴 하지만, 내부에서 아무
    # get_schema도 호출하지 않아야 한다(stale 목록이 비어 바로 반환).
    if thread2 is not None:
        thread2.join(timeout=5)
    assert adapter.get_schema_calls == ["orders"]  # 두 번째 호출에서 추가되지 않음


def test_multiple_tables_from_one_query_are_all_refreshed():
    adapter = _FakeAdapter(
        {
            "orders": SchemaObject(container="orders"),
            "customers": SchemaObject(container="customers"),
        }
    )
    thread = refresh_stale_tables("conn1", adapter, ["orders", "customers"])
    thread.join(timeout=5)

    assert set(adapter.get_schema_calls) == {"orders", "customers"}
    ids = RagIndex("conn1").all_ids()
    assert any(doc_id.endswith(":orders") for doc_id in ids)
    assert any(doc_id.endswith(":customers") for doc_id in ids)


def test_failure_for_one_table_does_not_block_others_or_raise():
    adapter = _FakeAdapter(
        {
            "orders": SchemaObject(container="orders"),
            "customers": SchemaObject(container="customers"),
        },
        fail_for={"customers"},
    )
    thread = refresh_stale_tables("conn1", adapter, ["orders", "customers"])
    thread.join(timeout=5)  # 예외가 스레드 밖으로 전파되지 않아야 한다(죽지 않고 join됨)

    ids = RagIndex("conn1").all_ids()
    assert any(doc_id.endswith(":orders") for doc_id in ids)
    assert not any(doc_id.endswith(":customers") for doc_id in ids)

    tracker_path = auto_refresh_module._tracker_path("conn1")
    tracker = json.loads(tracker_path.read_text(encoding="utf-8"))
    assert "orders" in tracker
    assert "customers" not in tracker  # 실패한 table은 오늘 날짜로 기록되지 않아 다음에 재시도됨


def test_tables_are_tracked_per_connection_independently():
    adapter1 = _FakeAdapter({"orders": SchemaObject(container="orders")})
    adapter2 = _FakeAdapter({"orders": SchemaObject(container="orders")})

    refresh_stale_tables("conn1", adapter1, ["orders"]).join(timeout=5)
    refresh_stale_tables("conn2", adapter2, ["orders"]).join(timeout=5)

    assert adapter1.get_schema_calls == ["orders"]
    assert adapter2.get_schema_calls == ["orders"]  # conn2는 conn1과 별개로 갱신됨
