"""SSH Bastion 터널의 두 로그인 방식(pem/password)에 대한 단위 테스트.

실제 SSH 서버 없이 paramiko.SSHClient.connect() 호출 인자를 가로채 검증한다
(tests/test_timeouts.py의 Driver mocking 스타일과 동일).
"""
from __future__ import annotations

import pytest

from server.connection.ssh_tunnel import SSHTunnel
from server.database.errors import SSHConnectionError


class _FakeTransport:
    def is_active(self):
        return True


class _FakeSSHClient:
    """paramiko.SSHClient를 흉내내는 최소 스텁 — connect() 호출 인자만 기록한다."""

    last_connect_kwargs: dict = {}

    def set_missing_host_key_policy(self, policy):
        pass

    def connect(self, **kwargs):
        _FakeSSHClient.last_connect_kwargs = kwargs

    def get_transport(self):
        return _FakeTransport()

    def close(self):
        pass


@pytest.fixture(autouse=True)
def _patch_paramiko(monkeypatch):
    import server.connection.ssh_tunnel as ssh_tunnel_module

    monkeypatch.setattr(ssh_tunnel_module.paramiko, "SSHClient", _FakeSSHClient)
    # ThreadingTCPServer가 실제로 소켓을 열고 스레드를 띄우는 부분은 이 테스트의
    # 관심사가 아니므로 그대로 두되(가벼우니 실제로 열어도 무방), 매 테스트마다
    # 새 local server가 열리게 둔다.
    yield


def test_pem_mode_passes_key_filename_and_expands_home_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    tunnel = SSHTunnel(
        ssh_host="bastion.example.com",
        ssh_port=22,
        ssh_user="ec2-user",
        remote_host="db.internal",
        remote_port=3306,
        login_mode="pem",
        identity_file="~/.ssh/prod.pem",
    )
    tunnel.open()
    try:
        kwargs = _FakeSSHClient.last_connect_kwargs
        assert kwargs["username"] == "ec2-user"
        assert kwargs["key_filename"] == str(tmp_path / ".ssh" / "prod.pem")
        assert "password" not in kwargs
    finally:
        tunnel.close()


def test_pem_mode_passes_passphrase_when_given():
    tunnel = SSHTunnel(
        ssh_host="bastion.example.com",
        ssh_port=22,
        ssh_user="ec2-user",
        remote_host="db.internal",
        remote_port=3306,
        login_mode="pem",
        identity_file="/keys/prod.pem",
        passphrase="secret-passphrase",
    )
    tunnel.open()
    try:
        assert _FakeSSHClient.last_connect_kwargs["passphrase"] == "secret-passphrase"
    finally:
        tunnel.close()


def test_password_mode_passes_password_instead_of_key_filename():
    tunnel = SSHTunnel(
        ssh_host="bastion.example.com",
        ssh_port=22,
        ssh_user="ec2-user",
        remote_host="db.internal",
        remote_port=3306,
        login_mode="password",
        password="bastion-password",
    )
    tunnel.open()
    try:
        kwargs = _FakeSSHClient.last_connect_kwargs
        assert kwargs["password"] == "bastion-password"
        assert "key_filename" not in kwargs
    finally:
        tunnel.close()


def test_defaults_to_pem_mode_when_login_mode_omitted():
    tunnel = SSHTunnel(
        ssh_host="bastion.example.com",
        ssh_port=22,
        ssh_user="ec2-user",
        remote_host="db.internal",
        remote_port=3306,
        identity_file="/keys/prod.pem",
    )
    assert tunnel.login_mode == "pem"


def test_pem_mode_without_identity_file_raises_before_connecting():
    with pytest.raises(SSHConnectionError, match="identity_file"):
        SSHTunnel(
            ssh_host="bastion.example.com",
            ssh_port=22,
            ssh_user="ec2-user",
            remote_host="db.internal",
            remote_port=3306,
            login_mode="pem",
        )


def test_password_mode_without_password_raises_before_connecting():
    with pytest.raises(SSHConnectionError, match="password"):
        SSHTunnel(
            ssh_host="bastion.example.com",
            ssh_port=22,
            ssh_user="ec2-user",
            remote_host="db.internal",
            remote_port=3306,
            login_mode="password",
        )


def test_unknown_login_mode_is_rejected():
    with pytest.raises(SSHConnectionError, match="login_mode"):
        SSHTunnel(
            ssh_host="bastion.example.com",
            ssh_port=22,
            ssh_user="ec2-user",
            remote_host="db.internal",
            remote_port=3306,
            login_mode="totp",
        )


# ----------------------------------------------------------------------
# ConnectionManager — ssh.password_env/passphrase_env가 실제로 resolve되어
# SSHTunnel까지 전달되는지 (server/connection/manager.py)
# ----------------------------------------------------------------------


def test_connection_manager_resolves_ssh_password_env(tmp_path, monkeypatch):
    from server.connection.manager import ConnectionManager

    monkeypatch.setenv("BASTION_PW", "bastion-secret")
    config_path = tmp_path / "connections.yaml"
    config_path.write_text(
        """
connections:
  prod-mongo:
    db_type: mongodb
    connection:
      uri_host: mongo.internal
      port: 27017
      database: d
    ssh:
      enabled: true
      host: bastion.example.com
      user: ec2-user
      login_mode: password
      password_env: BASTION_PW
""",
        encoding="utf-8",
    )
    manager = ConnectionManager(config_path)
    manager.load()

    resolved_ssh = manager._profiles["prod-mongo"]["ssh"]
    assert resolved_ssh["password"] == "bastion-secret"


def test_connection_manager_passes_ssh_login_mode_to_tunnel(tmp_path, monkeypatch):
    import server.connection.manager as manager_module

    captured: dict = {}

    class FakeTunnel:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def open(self):
            return 55555

        def close(self):
            pass

    monkeypatch.setattr(manager_module, "SSHTunnel", FakeTunnel)
    monkeypatch.setenv("BASTION_PW", "bastion-secret")

    config_path = tmp_path / "connections.yaml"
    config_path.write_text(
        """
connections:
  prod-mongo:
    db_type: mongodb
    connection:
      uri_host: mongo.internal
      port: 27017
      database: d
    ssh:
      enabled: true
      host: bastion.example.com
      user: ec2-user
      login_mode: password
      password_env: BASTION_PW
""",
        encoding="utf-8",
    )
    manager = manager_module.ConnectionManager(config_path)
    manager.load()
    profile = manager._profiles["prod-mongo"]
    effective_profile = manager._resolve_bastion_if_needed("prod-mongo", profile)

    assert captured["login_mode"] == "password"
    assert captured["password"] == "bastion-secret"
    assert captured["identity_file"] is None
    # MongoDB는 host가 아니라 uri_host 필드를 쓴다 — 이 필드가 로컬 포워딩
    # 포트로 정확히 바꿔치기되는지 확인한다(과거에는 "host"만 찾아서
    # KeyError가 나던 버그가 있었다).
    assert effective_profile["connection"]["uri_host"] == "127.0.0.1"
    assert effective_profile["connection"]["port"] == 55555


def test_bastion_raises_clear_error_for_uri_style_mongo_connection(tmp_path, monkeypatch):
    """connection.uri(mongodb+srv:// 등 완전한 연결 문자열)는 단일 host/port로
    표현되지 않아 Bastion 터널을 자동으로 구성할 수 없다 — KeyError로 죽는
    대신 원인을 알 수 있는 에러를 내야 한다."""
    from server.connection.manager import ConnectionManager
    from server.database.errors import DBConnectionError

    monkeypatch.setenv("BASTION_PW", "x")
    config_path = tmp_path / "connections.yaml"
    config_path.write_text(
        """
connections:
  prod-mongo-replicaset:
    db_type: mongodb
    connection:
      uri: "mongodb+srv://cluster0.example.mongodb.net/db"
    ssh:
      enabled: true
      host: bastion.example.com
      user: ec2-user
      login_mode: password
      password_env: BASTION_PW
""",
        encoding="utf-8",
    )
    manager = ConnectionManager(config_path)
    manager.load()
    profile = manager._profiles["prod-mongo-replicaset"]

    with pytest.raises(DBConnectionError, match="uri"):
        manager._resolve_bastion_if_needed("prod-mongo-replicaset", profile)
