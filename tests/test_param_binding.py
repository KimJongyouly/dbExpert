"""Named Parameter 변환기에 대한 단위 테스트. DB 연결이 전혀 필요 없다."""
from __future__ import annotations

from server.database.param_binding import build_in_clause, convert_named_params


def test_pyformat_conversion() -> None:
    sql, params = convert_named_params(
        "SELECT * FROM t WHERE a = :a AND b = :b", {"a": 1, "b": "x"}, "pyformat"
    )
    assert sql == "SELECT * FROM t WHERE a = %(a)s AND b = %(b)s"
    assert params == {"a": 1, "b": "x"}


def test_qmark_conversion_preserves_order() -> None:
    sql, params = convert_named_params(
        "SELECT * FROM t WHERE a = :a AND b = :b", {"b": "x", "a": 1}, "qmark"
    )
    assert sql == "SELECT * FROM t WHERE a = ? AND b = ?"
    assert params == [1, "x"]


def test_postgresql_double_colon_cast_is_not_treated_as_a_param() -> None:
    """`::bigint` 같은 PostgreSQL 캐스트 문법을 `:bigint` 파라미터로 잘못
    치환하면 안 된다 — 실제 통합 테스트에서 발견된 회귀 방지용 테스트
    (tests/integration/test_postgresql_integration.py)."""
    sql, params = convert_named_params(
        "SELECT c.reltuples::bigint AS n FROM t WHERE c.relname = :table",
        {"table": "orders"},
        "pyformat",
    )
    assert sql == "SELECT c.reltuples::bigint AS n FROM t WHERE c.relname = %(table)s"
    assert params == {"table": "orders"}


def test_no_params_returns_sql_unchanged() -> None:
    sql, params = convert_named_params("SELECT 1", None, "pyformat")
    assert sql == "SELECT 1"
    assert params == {}


def test_build_in_clause() -> None:
    placeholders, params = build_in_clause("c", ["orders", "customers"])
    assert placeholders == ":c0, :c1"
    assert params == {"c0": "orders", "c1": "customers"}
