"""관리자 계정으로 접속하는 경우를 대비한 세 번째 방어선(서버/세션 레벨
Read Only 강제) 단위 테스트.

Doc/00_개발요건사항.md §29 — 원래 첫 번째 방어선(DB 계정 자체를 Read Only로
발급)이 적용되지 않을 수 있어(실제 운영에서 관리자 계정 사용 결정), Engine이
지원하는 경우 연결 직후 서버 스스로 쓰기를 거부하게 만드는 세션 설정을
추가했다. 실제 DB 없이 Driver 호출 인자를 가로채 검증한다.
"""
from __future__ import annotations

import sys
import types


def test_mysql_after_connect_sets_session_transaction_read_only(monkeypatch):
    import server.database.mysql as mysql_module

    executed: list[str] = []

    class FakeCursor:
        def execute(self, sql):
            executed.append(sql)

        def close(self):
            pass

    class FakeConn:
        open = True

        def cursor(self):
            return FakeCursor()

    def fake_connect(**kwargs):
        return FakeConn()

    fake_pymysql = types.SimpleNamespace(
        connect=fake_connect, cursors=types.SimpleNamespace(DictCursor=object())
    )
    monkeypatch.setitem(sys.modules, "pymysql", fake_pymysql)
    monkeypatch.setitem(sys.modules, "pymysql.cursors", fake_pymysql.cursors)

    adapter = mysql_module.MySQLAdapter(
        {
            "connection": {"host": "h", "port": 3306, "database": "d"},
            "user": "admin",
            "password": "p",
        }
    )
    adapter._ensure_connection()

    assert executed == ["SET SESSION TRANSACTION READ ONLY"]


def test_postgresql_options_include_default_transaction_read_only(monkeypatch):
    import server.database.postgresql as postgresql_module

    captured: dict = {}

    def fake_connect(**kwargs):
        captured.update(kwargs)
        return types.SimpleNamespace(closed=False)

    fake_psycopg = types.SimpleNamespace(
        connect=fake_connect, rows=types.SimpleNamespace(dict_row=object())
    )
    monkeypatch.setitem(sys.modules, "psycopg", fake_psycopg)
    monkeypatch.setitem(sys.modules, "psycopg.rows", fake_psycopg.rows)

    adapter = postgresql_module.PostgreSQLAdapter(
        {
            "connection": {"host": "h", "port": 5432, "database": "d"},
            "user": "admin",
            "password": "p",
        }
    )
    adapter._connect()

    assert "default_transaction_read_only=on" in captured["options"]


def test_clickhouse_client_uses_readonly_setting(monkeypatch):
    import server.database.clickhouse as clickhouse_module

    captured: dict = {}

    def fake_get_client(**kwargs):
        captured.update(kwargs)
        return types.SimpleNamespace()

    fake_module = types.SimpleNamespace(get_client=fake_get_client)
    monkeypatch.setitem(sys.modules, "clickhouse_connect", fake_module)

    adapter = clickhouse_module.ClickHouseAdapter(
        {
            "connection": {"host": "h", "port": 8123, "database": "d"},
            "user": "admin",
            "password": "p",
        }
    )
    adapter._ensure_client()

    assert captured["settings"] == {"readonly": 1}


def test_tibero_connect_calls_set_read_only(monkeypatch):
    import server.database.tibero as tibero_module

    read_only_calls: list[bool] = []

    class FakeConnection:
        def setReadOnly(self, value):
            read_only_calls.append(value)

    class FakeDriverManager:
        @staticmethod
        def setLoginTimeout(seconds):
            pass

        @staticmethod
        def getConnection(url, user, password):
            return FakeConnection()

    fake_java_sql = types.SimpleNamespace(DriverManager=FakeDriverManager)
    monkeypatch.setitem(sys.modules, "java.sql", fake_java_sql)
    monkeypatch.setattr(tibero_module, "_ensure_jvm_started", lambda: None)

    adapter = tibero_module.TiberoAdapter(
        {
            "connection": {"host": "h", "port": 8629},
            "user": "admin",
            "password": "p",
        }
    )
    adapter._ensure_connection()

    assert read_only_calls == [True]


def test_tibero_connect_survives_setreadonly_not_supported(monkeypatch):
    """일부 JDBC Driver/서버 조합은 setReadOnly를 지원하지 않을 수 있다 —
    best-effort이므로 실패해도 연결 자체는 계속 진행해야 한다."""
    import server.database.tibero as tibero_module

    class FakeConnection:
        def setReadOnly(self, value):
            raise RuntimeError("not supported")

    class FakeDriverManager:
        @staticmethod
        def setLoginTimeout(seconds):
            pass

        @staticmethod
        def getConnection(url, user, password):
            return FakeConnection()

    fake_java_sql = types.SimpleNamespace(DriverManager=FakeDriverManager)
    monkeypatch.setitem(sys.modules, "java.sql", fake_java_sql)
    monkeypatch.setattr(tibero_module, "_ensure_jvm_started", lambda: None)

    adapter = tibero_module.TiberoAdapter(
        {
            "connection": {"host": "h", "port": 8629},
            "user": "admin",
            "password": "p",
        }
    )
    conn = adapter._ensure_connection()
    assert isinstance(conn, FakeConnection)
