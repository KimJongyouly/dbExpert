"""ConnectionManager.load()가 Connection 하나의 설정 오류로 MCP Server 전체를
죽이지 않는지에 대한 단위 테스트.

실제 운영 환경에서 재현된 문제: Connection이 수십 개 등록된 상태에서 그중
하나(예: 비밀번호를 못 찾은 Connection)의 환경변수가 없으면
`AuthenticationError`가 `main()`까지 전파되어 서버 프로세스 자체가 죽었다
(Claude Code에는 그냥 "MCP 서버 연결 끊김"으로만 보임). 지금은 그 Connection
하나만 건너뛰고 나머지는 정상 동작해야 한다.
"""
from __future__ import annotations

import pytest

from server.connection.manager import ConnectionManager
from server.database.errors import AuthenticationError, DBConnectionError


def _write_config(tmp_path, monkeypatch, good_env_user="GOOD_USER", good_env_pw="GOOD_PW"):
    monkeypatch.setenv(good_env_user, "root")
    monkeypatch.setenv(good_env_pw, "secret")
    # BROKEN_* 환경변수는 의도적으로 설정하지 않는다.
    config_path = tmp_path / "connections.yaml"
    config_path.write_text(
        f"""
connections:
  good-conn:
    db_type: mysql
    connection:
      host: good.internal
      port: 3306
      database: d
    user_env: {good_env_user}
    password_env: {good_env_pw}
    ssh:
      enabled: false

  broken-conn:
    db_type: mysql
    connection:
      host: broken.internal
      port: 3306
      database: d
    user_env: BROKEN_USER_NOT_SET
    password_env: BROKEN_PASSWORD_NOT_SET
    ssh:
      enabled: false
""",
        encoding="utf-8",
    )
    return config_path


def test_load_does_not_raise_when_one_connection_is_missing_credentials(tmp_path, monkeypatch):
    config_path = _write_config(tmp_path, monkeypatch)
    manager = ConnectionManager(config_path)

    manager.load()  # 예전엔 여기서 AuthenticationError가 그대로 터져 서버가 죽었다


def test_good_connection_still_usable_when_another_connection_is_broken(tmp_path, monkeypatch):
    config_path = _write_config(tmp_path, monkeypatch)
    manager = ConnectionManager(config_path)
    manager.load()

    names = {c["name"] for c in manager.list_connections()}
    assert "good-conn" in names
    assert "broken-conn" not in names  # 깨진 건 목록에서 빠진다


def test_broken_connection_records_load_error_with_reason(tmp_path, monkeypatch):
    config_path = _write_config(tmp_path, monkeypatch)
    manager = ConnectionManager(config_path)
    manager.load()

    errors = manager.list_load_errors()
    assert "broken-conn" in errors
    assert "BROKEN_USER_NOT_SET" in errors["broken-conn"]
    assert "good-conn" not in errors


def test_get_connection_on_broken_connection_raises_helpful_authentication_error(
    tmp_path, monkeypatch
):
    config_path = _write_config(tmp_path, monkeypatch)
    manager = ConnectionManager(config_path)
    manager.load()

    with pytest.raises(AuthenticationError, match="broken-conn"):
        manager.get_connection("broken-conn")


def test_get_connection_on_truly_unknown_name_still_raises_db_connection_error(
    tmp_path, monkeypatch
):
    config_path = _write_config(tmp_path, monkeypatch)
    manager = ConnectionManager(config_path)
    manager.load()

    with pytest.raises(DBConnectionError, match="등록되지 않은"):
        manager.get_connection("no-such-connection-at-all")
