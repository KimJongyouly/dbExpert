"""SSH Bastion 로컬 포트 포워딩.

Doc/00_개발요건사항.md §8 Connection Manager (Bastion 방식) 참고.
paramiko의 공식 forward.py 예제와 동일한 "로컬 TCP 서버 + direct-tcpip
채널" 패턴을 사용한다: 로컬에 임시 포트를 하나 열어 두고, 거기로 들어오는
연결을 SSH Transport 위의 채널로 그대로 이어준다. Adapter는 이 로컬 포트를
마치 DB가 그 자리에 있는 것처럼 접속하면 된다.

Bastion 로그인 방식은 두 가지를 지원한다(Connection Profile의
`ssh.login_mode`, Doc/01_개발정의서.md §4.1 참고):
  - "pem"      : `identity_file`(개인키 경로) + 선택적 `passphrase`로 로그인
  - "password" : `password`(Bastion 계정 비밀번호)로 로그인
`login_mode`를 생략하면 기존 설정과의 하위 호환을 위해 "pem"으로 간주한다.
"""
from __future__ import annotations

import os
import select
import socket
import socketserver
import threading
from typing import Optional

import paramiko

from server.database.errors import SSHConnectionError

_FORWARD_BUFFER_SIZE = 4096
_LOGIN_MODES = ("pem", "password")


def _pipe(sock: socket.socket, channel: paramiko.Channel) -> None:
    """sock <-> channel 사이에서 한쪽이 닫힐 때까지 양방향으로 바이트를 그대로 전달한다."""
    try:
        while True:
            readable, _, _ = select.select([sock, channel], [], [])
            if sock in readable:
                data = sock.recv(_FORWARD_BUFFER_SIZE)
                if not data:
                    break
                channel.send(data)
            if channel in readable:
                data = channel.recv(_FORWARD_BUFFER_SIZE)
                if not data:
                    break
                sock.send(data)
    except Exception:  # noqa: BLE001 — 연결 종료는 정상 흐름의 일부
        pass
    finally:
        channel.close()
        sock.close()


class SSHTunnel:
    def __init__(
        self,
        ssh_host: str,
        ssh_port: int,
        ssh_user: str,
        remote_host: str,
        remote_port: int,
        login_mode: str = "pem",
        identity_file: Optional[str] = None,
        passphrase: Optional[str] = None,
        password: Optional[str] = None,
        timeout_sec: int = 10,
    ):
        if login_mode not in _LOGIN_MODES:
            raise SSHConnectionError(
                f"ssh.login_mode는 {_LOGIN_MODES} 중 하나여야 합니다: {login_mode!r}"
            )
        if login_mode == "pem" and not identity_file:
            raise SSHConnectionError("ssh.login_mode가 'pem'이면 ssh.identity_file이 필요합니다.")
        if login_mode == "password" and not password:
            raise SSHConnectionError(
                "ssh.login_mode가 'password'이면 ssh.password_env(비밀번호)가 필요합니다."
            )

        self.ssh_host = ssh_host
        self.ssh_port = ssh_port
        self.ssh_user = ssh_user
        self.login_mode = login_mode
        # "~/.ssh/..." 처럼 홈 디렉터리 표기를 쓴 경로는 YAML/OS 어느 쪽도
        # 자동으로 펼쳐주지 않아, 그대로 paramiko에 넘기면 파일을 못 찾는다.
        self.identity_file = os.path.expanduser(identity_file) if identity_file else None
        self.passphrase = passphrase
        self.password = password
        self.remote_host = remote_host
        self.remote_port = remote_port
        self.timeout_sec = timeout_sec

        self.local_bind_port: Optional[int] = None
        self._ssh_client: Optional[paramiko.SSHClient] = None
        self._local_server: Optional[socketserver.ThreadingTCPServer] = None
        self._server_thread: Optional[threading.Thread] = None

    def open(self) -> int:
        try:
            client = paramiko.SSHClient()
            # TODO: 운영 환경에서는 AutoAddPolicy 대신 알려진 Bastion Host Key를
            #       명시적으로 등록(load_host_keys)하는 방식으로 강화 검토.
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            if self.login_mode == "password":
                client.connect(
                    hostname=self.ssh_host,
                    port=self.ssh_port,
                    username=self.ssh_user,
                    password=self.password,
                    timeout=self.timeout_sec,
                )
            else:
                client.connect(
                    hostname=self.ssh_host,
                    port=self.ssh_port,
                    username=self.ssh_user,
                    key_filename=self.identity_file,
                    passphrase=self.passphrase,
                    timeout=self.timeout_sec,
                )
        except Exception as exc:  # noqa: BLE001
            raise SSHConnectionError(str(exc)) from exc

        self._ssh_client = client
        transport = client.get_transport()
        if transport is None:
            raise SSHConnectionError("SSH Transport를 얻지 못했습니다.")

        remote_host, remote_port = self.remote_host, self.remote_port

        class _ForwardHandler(socketserver.BaseRequestHandler):
            def handle(self) -> None:  # noqa: ANN001 — socketserver 표준 시그니처
                try:
                    channel = transport.open_channel(
                        "direct-tcpip",
                        (remote_host, remote_port),
                        self.request.getpeername(),
                    )
                except Exception:
                    self.request.close()
                    return
                if channel is None:
                    self.request.close()
                    return
                _pipe(self.request, channel)

        self._local_server = socketserver.ThreadingTCPServer(
            ("127.0.0.1", 0), _ForwardHandler
        )
        self._local_server.daemon_threads = True
        self.local_bind_port = self._local_server.server_address[1]

        self._server_thread = threading.Thread(
            target=self._local_server.serve_forever, daemon=True
        )
        self._server_thread.start()
        return self.local_bind_port

    def close(self) -> None:
        if self._local_server is not None:
            self._local_server.shutdown()
            self._local_server.server_close()
            self._local_server = None
        if self._ssh_client is not None:
            self._ssh_client.close()
            self._ssh_client = None
        self.local_bind_port = None

    def __enter__(self) -> "SSHTunnel":
        self.open()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
