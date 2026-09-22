"""execute_readonly_query Tool 구현(query_builder/executor.py)에 대한 단위 테스트.

Read Only 방어 자체(sql_guard 등)는 이미 다른 테스트에서 검증됐으므로,
여기서는 이 Tool이 adapter.execute_readonly()를 그대로 위임하고 limit/mask를
올바르게 적용하는지만 검증한다.
"""
from __future__ import annotations

from server.database.base import BaseAdapter, Capability, SqlCapable
from server.query_builder.executor import execute_readonly_query


class _FakeAdapter(BaseAdapter, SqlCapable):
    capabilities = {Capability.SQL}

    def __init__(self, rows):
        super().__init__({"connection": {}})
        self._rows = rows
        self.executed_sql: list[str] = []

    def ping(self):
        return {"connected": True}

    def execute_readonly(self, sql, params=None):
        self.executed_sql.append(sql)
        return [dict(r) for r in self._rows]  # 매 호출마다 원본 훼손 방지용 복사

    def explain(self, sql):
        raise NotImplementedError  # 이 테스트에서는 쓰이지 않음


class _NoCapabilityAdapter(BaseAdapter):
    capabilities: set[Capability] = set()

    def ping(self):
        return {"connected": True}


def test_capability_not_supported_when_adapter_has_no_sql():
    result = execute_readonly_query(_NoCapabilityAdapter({}), sql="SELECT 1")
    assert result["status"] == "CAPABILITY_NOT_SUPPORTED"


def test_returns_rows_and_row_count_within_limit():
    adapter = _FakeAdapter(rows=[{"id": 1}, {"id": 2}])
    result = execute_readonly_query(adapter, sql="SELECT * FROM orders", limit=100)

    assert result["rows"] == [{"id": 1}, {"id": 2}]
    assert result["row_count"] == 2
    assert result["truncated"] is False
    assert adapter.executed_sql == ["SELECT * FROM orders"]


def test_truncates_when_result_exceeds_limit():
    adapter = _FakeAdapter(rows=[{"id": i} for i in range(10)])
    result = execute_readonly_query(adapter, sql="SELECT * FROM orders", limit=3)

    assert result["row_count"] == 3
    assert result["truncated"] is True
    assert [r["id"] for r in result["rows"]] == [0, 1, 2]


def test_limit_is_capped_at_max_even_if_caller_asks_for_more():
    adapter = _FakeAdapter(rows=[{"id": i} for i in range(2000)])
    result = execute_readonly_query(adapter, sql="SELECT * FROM orders", limit=5000)

    assert result["row_count"] == 1000  # _MAX_LIMIT
    assert result["truncated"] is True


def test_mask_replaces_specified_columns_with_placeholder():
    adapter = _FakeAdapter(rows=[{"id": 1, "email": "a@example.com"}])
    result = execute_readonly_query(
        adapter, sql="SELECT * FROM users", mask=["email"]
    )

    assert result["rows"] == [{"id": 1, "email": "***"}]


def test_mask_ignores_columns_not_present_in_result():
    adapter = _FakeAdapter(rows=[{"id": 1}])
    result = execute_readonly_query(adapter, sql="SELECT id FROM users", mask=["email"])

    assert result["rows"] == [{"id": 1}]  # 없는 컬럼은 조용히 무시
