"""MySQL Adapter 통합 테스트 — 실제 MySQL Docker 컨테이너에 붙여서 검증한다.

단위 테스트(tests/test_execute_readonly_guard.py 등)는 Fake 객체로
"가드가 코드상 맞게 짜여 있다"만 증명했다. 여기서는 진짜 pymysql 연결로
- Read Only 가드가 실제 쓰기를 막는지
- Table/Column comment, FK가 정말로 조회되는지
- Query Timeout이 실제로 소켓을 끊는지
를 검증한다.
"""
from __future__ import annotations

from decimal import Decimal

import pymysql
import pytest
from testcontainers.community.mysql import MySqlContainer

from server.database.errors import (
    DatabaseQueryError,
    QueryTimeoutError,
    ReadOnlyViolationError,
)
from server.database.mysql import MySQLAdapter

_INIT_STATEMENTS = [
    """
    CREATE TABLE customers (
        id BIGINT PRIMARY KEY
    ) COMMENT='고객 정보'
    """,
    """
    CREATE TABLE orders (
        id BIGINT PRIMARY KEY,
        customer_id BIGINT COMMENT '고객 ID',
        amount DECIMAL(10,2),
        CONSTRAINT fk_orders_customer FOREIGN KEY (customer_id) REFERENCES customers(id)
    ) COMMENT='주문 정보'
    """,
    "INSERT INTO customers (id) VALUES (1), (2)",
    "INSERT INTO orders (id, customer_id, amount) VALUES (1, 1, 100.00), (2, 2, 200.00)",
]


def _profile(container: MySqlContainer, **overrides) -> dict:
    profile = {
        "connection": {
            "host": container.get_container_host_ip(),
            "port": int(container.get_exposed_port(3306)),
            "database": container.dbname,
        },
        "user": container.username,
        "password": container.password,
    }
    profile.update(overrides)
    return profile


@pytest.fixture(scope="module")
def mysql_container():
    with MySqlContainer("mysql:8.0") as container:
        yield container


@pytest.fixture(scope="module")
def seeded_mysql(mysql_container):
    conn = pymysql.connect(
        host=mysql_container.get_container_host_ip(),
        port=int(mysql_container.get_exposed_port(3306)),
        user=mysql_container.username,
        password=mysql_container.password,
        database=mysql_container.dbname,
    )
    try:
        with conn.cursor() as cursor:
            for statement in _INIT_STATEMENTS:
                cursor.execute(statement)
        conn.commit()
    finally:
        conn.close()
    return mysql_container


@pytest.fixture
def adapter(seeded_mysql):
    instance = MySQLAdapter(_profile(seeded_mysql))
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
    assert rows[0]["cnt"] == 2  # 가드에 막혀서 실제로 지워지지 않았다


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


def test_get_table_statistics_returns_real_row_count(adapter):
    stats = adapter.get_table_statistics("test", "orders")
    # InnoDB의 TABLE_ROWS는 추정치라 정확히 2가 아닐 수 있어 None이 아님만 확인한다.
    assert stats.row_count_estimate is not None


def test_server_itself_rejects_writes_even_if_sql_guard_is_bypassed(adapter):
    """관리자 계정을 쓰는 경우를 대비한 세 번째 방어선 검증 — Python 레벨
    가드(assert_readonly_sql)를 완전히 우회해서 raw connection으로 직접
    DELETE를 실행해도, `_after_connect()`가 건 `SET SESSION TRANSACTION
    READ ONLY` 덕분에 MySQL 서버 자신이 쓰기를 거부해야 한다(Doc/00 §29)."""
    raw_conn = adapter._ensure_connection()  # execute_readonly()를 거치지 않음 = 가드 우회
    cursor = raw_conn.cursor()
    try:
        with pytest.raises(pymysql.err.OperationalError, match=r"(?i)read.only"):
            cursor.execute("DELETE FROM orders WHERE id = 1")
    finally:
        cursor.close()

    rows = adapter.execute_readonly("SELECT COUNT(*) AS cnt FROM orders")
    assert rows[0]["cnt"] == 2  # 서버가 직접 막아서 실제로 지워지지 않았다


def test_query_timeout_is_actually_enforced_by_the_socket(seeded_mysql):
    """query_timeout_sec=1로 설정하고 3초짜리 쿼리를 실행하면 실제로
    타임아웃 예외가 발생해야 한다 — PyMySQL의 read_timeout이 진짜로
    소켓을 끊는지 확인한다(§33)."""
    timeout_adapter = MySQLAdapter(_profile(seeded_mysql, query_timeout_sec=1))
    try:
        with pytest.raises((QueryTimeoutError, DatabaseQueryError)):
            timeout_adapter.execute_readonly("SELECT SLEEP(3)")
    finally:
        timeout_adapter.close()


def test_get_sessions_includes_idle_connections(seeded_mysql, adapter):
    """server/sql/mysql/sessions.sql에서 `WHERE COMMAND <> 'Sleep'` 필터를
    없앤 것이 실제로 idle 세션까지 돌려주는지 실제 서버로 확인한다 —
    running/idle 구분은 이제 Python(session_monitor.py)의 몫이다."""
    idle_conn = pymysql.connect(
        host=seeded_mysql.get_container_host_ip(),
        port=int(seeded_mysql.get_exposed_port(3306)),
        user=seeded_mysql.username,
        password=seeded_mysql.password,
        database=seeded_mysql.dbname,
    )
    try:
        with idle_conn.cursor() as cursor:
            cursor.execute("SELECT 1")  # 연결을 맺고 곧바로 아무것도 안 해 Sleep 상태로 둔다

        sessions = adapter.get_sessions()
        states = {s.state for s in sessions}
        assert "Sleep" in states
    finally:
        idle_conn.close()
