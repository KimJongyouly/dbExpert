"""PostgreSQL Adapter 통합 테스트 — 실제 PostgreSQL Docker 컨테이너에 붙여서 검증한다.

tests/integration/test_mysql_integration.py와 같은 항목을 PostgreSQL
방언(COMMENT ON, statement_timeout)으로 검증한다.
"""
from __future__ import annotations

from decimal import Decimal

import psycopg
import pytest
from testcontainers.community.postgres import PostgresContainer

from server.database.errors import (
    DatabaseQueryError,
    QueryTimeoutError,
    ReadOnlyViolationError,
)
from server.database.postgresql import PostgreSQLAdapter

_INIT_STATEMENTS = [
    "CREATE TABLE customers (id BIGINT PRIMARY KEY)",
    "COMMENT ON TABLE customers IS '고객 정보'",
    """
    CREATE TABLE orders (
        id BIGINT PRIMARY KEY,
        customer_id BIGINT REFERENCES customers(id),
        amount NUMERIC(10,2)
    )
    """,
    "COMMENT ON TABLE orders IS '주문 정보'",
    "COMMENT ON COLUMN orders.customer_id IS '고객 ID'",
    "INSERT INTO customers (id) VALUES (1), (2)",
    "INSERT INTO orders (id, customer_id, amount) VALUES (1, 1, 100.00), (2, 2, 200.00)",
]


def _profile(container: PostgresContainer, **overrides) -> dict:
    profile = {
        "connection": {
            "host": container.get_container_host_ip(),
            "port": int(container.get_exposed_port(5432)),
            "database": container.dbname,
        },
        "user": container.username,
        "password": container.password,
    }
    profile.update(overrides)
    return profile


@pytest.fixture(scope="module")
def postgres_container():
    with PostgresContainer("postgres:16") as container:
        yield container


@pytest.fixture(scope="module")
def seeded_postgres(postgres_container):
    conn = psycopg.connect(
        host=postgres_container.get_container_host_ip(),
        port=int(postgres_container.get_exposed_port(5432)),
        user=postgres_container.username,
        password=postgres_container.password,
        dbname=postgres_container.dbname,
        autocommit=True,
    )
    try:
        with conn.cursor() as cursor:
            for statement in _INIT_STATEMENTS:
                cursor.execute(statement)
    finally:
        conn.close()
    return postgres_container


@pytest.fixture
def adapter(seeded_postgres):
    instance = PostgreSQLAdapter(_profile(seeded_postgres))
    yield instance
    instance.close()


def test_ping_connects_to_real_server(adapter):
    result = adapter.ping()
    assert result["connected"] is True


def test_execute_readonly_returns_real_rows(adapter):
    rows = adapter.execute_readonly("SELECT id, amount FROM orders ORDER BY id")
    assert [r["id"] for r in rows] == [1, 2]
    assert [Decimal(str(r["amount"])) for r in rows] == [Decimal("100.00"), Decimal("200.00")]


def test_write_statement_is_blocked_and_data_is_untouched(adapter):
    with pytest.raises(ReadOnlyViolationError):
        adapter.execute_readonly("DELETE FROM orders WHERE id = 1")

    rows = adapter.execute_readonly("SELECT COUNT(*) AS cnt FROM orders")
    assert rows[0]["cnt"] == 2


def test_get_schema_reads_real_table_and_column_comments(adapter):
    objects = adapter.get_schema({"container": "orders"})
    assert len(objects) == 1
    schema_object = objects[0]
    assert schema_object.container_comment == "주문 정보"

    customer_id_field = next(f for f in schema_object.fields if f.name == "customer_id")
    assert customer_id_field.comment == "고객 ID"


def test_get_relationships_reads_real_foreign_key(adapter):
    relationships = adapter.get_relationships(["orders", "customers"])
    assert len(relationships) == 1
    fk = relationships[0]
    assert fk.from_container == "orders"
    assert fk.from_field == "customer_id"
    assert fk.to_container == "customers"
    assert fk.to_field == "id"
    assert fk.confidence == "declared"


def test_get_table_statistics_returns_real_size(adapter):
    stats = adapter.get_table_statistics("public", "orders")
    assert stats.size_bytes is not None and stats.size_bytes > 0


def test_server_itself_rejects_writes_even_if_sql_guard_is_bypassed(adapter):
    """관리자 계정을 쓰는 경우를 대비한 세 번째 방어선 검증 — Python 레벨
    가드(assert_readonly_sql)를 완전히 우회해서 raw connection으로 직접
    DELETE를 실행해도, 접속 시 건 `default_transaction_read_only=on` 덕분에
    PostgreSQL 서버 자신이 쓰기를 거부해야 한다(Doc/00 §29)."""
    raw_conn = adapter._ensure_connection()  # execute_readonly()를 거치지 않음 = 가드 우회
    with raw_conn.cursor() as cursor:
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            cursor.execute("DELETE FROM orders WHERE id = 1")
    raw_conn.rollback()

    rows = adapter.execute_readonly("SELECT COUNT(*) AS cnt FROM orders")
    assert rows[0]["cnt"] == 2  # 서버가 직접 막아서 실제로 지워지지 않았다


def test_query_timeout_is_actually_enforced_by_statement_timeout(seeded_postgres):
    """query_timeout_sec=1로 설정하면 접속 시 statement_timeout=1000(ms)이
    세션에 걸리고, 3초짜리 쿼리는 서버가 직접 취소해야 한다(§33)."""
    timeout_adapter = PostgreSQLAdapter(_profile(seeded_postgres, query_timeout_sec=1))
    try:
        with pytest.raises((QueryTimeoutError, DatabaseQueryError)):
            timeout_adapter.execute_readonly("SELECT pg_sleep(3)")
    finally:
        timeout_adapter.close()


def test_get_sessions_includes_idle_connections(seeded_postgres, adapter):
    """server/sql/postgresql/sessions.sql에서 `WHERE state IS DISTINCT FROM
    'idle'` 필터를 없앤 것이 실제로 idle 세션까지 돌려주는지 실제 서버로
    확인한다 — running/idle 구분은 이제 Python(session_monitor.py)의 몫이다."""
    idle_conn = psycopg.connect(
        host=seeded_postgres.get_container_host_ip(),
        port=int(seeded_postgres.get_exposed_port(5432)),
        user=seeded_postgres.username,
        password=seeded_postgres.password,
        dbname=seeded_postgres.dbname,
        autocommit=True,
    )
    try:
        with idle_conn.cursor() as cursor:
            cursor.execute("SELECT 1")  # 연결을 맺고 곧바로 아무것도 안 해 idle 상태로 둔다

        sessions = adapter.get_sessions()
        states = {s.state for s in sessions}
        assert "idle" in states
    finally:
        idle_conn.close()
