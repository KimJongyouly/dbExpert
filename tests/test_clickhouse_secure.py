"""ClickHouse HTTPS(secure) 접속 지원에 대한 단위 테스트.

ClickHouse Cloud/관리형 인스턴스처럼 HTTPS(443/8443)로만 접속을 받는
경우를 위해 connection.secure 플래그를 clickhouse_connect.get_client()의
secure 인자로 그대로 전달하는지 확인한다.
"""
from __future__ import annotations

import sys
import types

from server.database.clickhouse import ClickHouseAdapter


def _fake_clickhouse_connect(monkeypatch):
    captured: dict = {}

    def fake_get_client(**kwargs):
        captured.update(kwargs)
        return types.SimpleNamespace()

    fake_module = types.SimpleNamespace(get_client=fake_get_client)
    monkeypatch.setitem(sys.modules, "clickhouse_connect", fake_module)
    return captured


def test_secure_true_is_passed_through_to_client(monkeypatch):
    captured = _fake_clickhouse_connect(monkeypatch)
    adapter = ClickHouseAdapter(
        {
            "connection": {"host": "clickhouse.example.com", "port": 443, "secure": True},
            "user": "default",
            "password": "p",
        }
    )
    adapter._ensure_client()
    assert captured["secure"] is True


def test_secure_defaults_to_false_when_omitted(monkeypatch):
    captured = _fake_clickhouse_connect(monkeypatch)
    adapter = ClickHouseAdapter(
        {
            "connection": {"host": "clickhouse.internal", "port": 8123},
            "user": "default",
            "password": "p",
        }
    )
    adapter._ensure_client()
    assert captured["secure"] is False
