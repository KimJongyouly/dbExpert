"""Query/Connect Timeout이 config/connections.yaml 값을 따르고, 없으면
기본값(5초/30초)을 쓰는지에 대한 단위 테스트.

Doc/00_개발요건사항.md §33 Timeout 참고. 실제 DB 없이 검증하기 위해
mysql/postgresql은 driver의 connect() 호출 인자를 가로채서 확인한다.
"""
from __future__ import annotations

import sys
import types

from server.database.base import (
    DEFAULT_CONNECT_TIMEOUT_SEC,
    DEFAULT_QUERY_TIMEOUT_SEC,
    BaseAdapter,
)


class _MinimalAdapter(BaseAdapter):
    def ping(self):
        return {"connected": True}


def test_timeouts_default_when_not_configured():
    adapter = _MinimalAdapter({"connection": {}})
    assert adapter.connect_timeout_sec == DEFAULT_CONNECT_TIMEOUT_SEC == 5
    assert adapter.query_timeout_sec == DEFAULT_QUERY_TIMEOUT_SEC == 30


def test_timeouts_use_profile_values_when_configured():
    adapter = _MinimalAdapter(
        {"connection": {}, "connect_timeout_sec": 3, "query_timeout_sec": 90}
    )
    assert adapter.connect_timeout_sec == 3
    assert adapter.query_timeout_sec == 90


def test_connection_manager_passes_timeout_fields_through(tmp_path):
    from server.connection.manager import ConnectionManager

    config_path = tmp_path / "connections.yaml"
    config_path.write_text(
        """
connections:
  prod-aurora:
    db_type: mysql
    connection:
      host: db.internal
      port: 3306
      database: appdb
    query_timeout_sec: 60
    ssh:
      enabled: false
""",
        encoding="utf-8",
    )
    manager = ConnectionManager(config_path)
    manager.load()
    profiles = manager.list_connections()
    assert profiles == [{"name": "prod-aurora", "db_type": "mysql"}]
    # 내부 프로필에 값이 그대로 반영됐는지 확인 (private이지만 회귀 방지 목적으로 직접 확인)
    assert manager._profiles["prod-aurora"]["query_timeout_sec"] == 60


def test_mysql_adapter_passes_query_timeout_to_driver(monkeypatch):
    import server.database.mysql as mysql_module

    captured: dict = {}

    def fake_connect(**kwargs):
        captured.update(kwargs)
        return types.SimpleNamespace(open=True)

    fake_pymysql = types.SimpleNamespace(
        connect=fake_connect, cursors=types.SimpleNamespace(DictCursor=object())
    )
    monkeypatch.setitem(sys.modules, "pymysql", fake_pymysql)
    monkeypatch.setitem(sys.modules, "pymysql.cursors", fake_pymysql.cursors)

    adapter = mysql_module.MySQLAdapter(
        {
            "connection": {"host": "h", "port": 3306, "database": "d"},
            "user": "u",
            "password": "p",
            "query_timeout_sec": 45,
            "connect_timeout_sec": 7,
        }
    )
    adapter._connect()

    assert captured["read_timeout"] == 45
    assert captured["write_timeout"] == 45
    assert captured["connect_timeout"] == 7


def test_postgresql_adapter_passes_statement_timeout_to_driver(monkeypatch):
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
            "user": "u",
            "password": "p",
            "query_timeout_sec": 12,
        }
    )
    adapter._connect()

    assert captured["options"] == (
        "-c statement_timeout=12000 -c default_transaction_read_only=on"
    )
