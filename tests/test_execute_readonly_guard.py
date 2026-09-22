"""SqlAdapterBase.execute_readonly()가 실제 Driver를 호출하기 '전에'
쓰기 구문을 차단하는지 확인한다 — Fake Adapter를 만들어 실제 DB 연결 없이
검증한다(회귀 방지용 핵심 안전장치 테스트).
"""
from __future__ import annotations

import pytest

from server.database.base import Capability
from server.database.errors import ReadOnlyViolationError
from server.database.sql_base import SqlAdapterBase


class _FakeCursor:
    def __init__(self, rows: list[dict]):
        self._rows = rows
        self.executed_sql: list[str] = []
        self.description = None

    def execute(self, sql, params=None):
        self.executed_sql.append(sql)  # 여기까지 오면 가드를 통과했다는 뜻

    def fetchall(self):
        return self._rows

    def close(self):
        pass


class _FakeConnection:
    def __init__(self, cursor: _FakeCursor):
        self._cursor = cursor
        self.closed = False

    def cursor(self):
        return self._cursor


class _FakeAdapter(SqlAdapterBase):
    capabilities = {Capability.SQL}
    paramstyle = "pyformat"
    engine_name = "fake"

    def __init__(self, connection_profile, cursor: _FakeCursor):
        super().__init__(connection_profile)
        self._fake_cursor = cursor

    def _connect(self):
        return _FakeConnection(self._fake_cursor)

    def _is_closed(self, conn) -> bool:
        return conn.closed

    def explain(self, sql):  # pragma: no cover - 본 테스트 범위 밖
        raise NotImplementedError


def _make_adapter(rows: list[dict] | None = None) -> tuple[_FakeAdapter, _FakeCursor]:
    cursor = _FakeCursor(rows or [{"id": 1}])
    adapter = _FakeAdapter({"connection": {}}, cursor)
    return adapter, cursor


def test_select_reaches_the_driver() -> None:
    adapter, cursor = _make_adapter([{"id": 1, "name": "a"}])
    rows = adapter.execute_readonly("SELECT id, name FROM orders")
    assert rows == [{"id": 1, "name": "a"}]
    assert cursor.executed_sql == ["SELECT id, name FROM orders"]


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM orders",
        "UPDATE orders SET status = 'X'",
        "DROP TABLE orders",
        "INSERT INTO orders VALUES (1)",
    ],
)
def test_write_statements_never_reach_the_driver(sql: str) -> None:
    adapter, cursor = _make_adapter()
    with pytest.raises(ReadOnlyViolationError):
        adapter.execute_readonly(sql)
    assert cursor.executed_sql == []  # 가드가 Driver 호출 자체를 막았는지 확인


def test_named_params_are_converted_before_reaching_driver() -> None:
    adapter, cursor = _make_adapter()
    adapter.execute_readonly("SELECT * FROM orders WHERE id = :id", {"id": 42})
    assert cursor.executed_sql == ["SELECT * FROM orders WHERE id = %(id)s"]
