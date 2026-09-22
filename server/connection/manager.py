"""Connection Manager — Connection Profile 로드 및 Adapter 인스턴스 관리.

PropLBS(app/db/dao.py)가 모듈 최초 import 시점에 `obj_db = Database(); obj_db.connect()`
로 앱 전체가 공유하는 싱글턴을 만들어 두던 것과 같은 구조를, Connection
Profile 이름별로 일반화했다: 이름당 Adapter 인스턴스를 1개만 만들어
캐시해 두고(지연 생성), 이후 호출은 캐시를 재사용한다. Adapter 내부의
실제 DB 연결은 (SQL 계열의 경우) sql_base.py가 스레드별로 관리한다.

Timeout 기본값(Connection 5s / Query 30s / SSH 10s)은
Doc/00_개발요건사항.md §33을 따른다.

⚠️ load()는 Connection 하나가 실패해도 MCP Server 전체가 죽지 않는다
(2026-09, 실제 운영 환경에서 재현/수정됨) — Connection이 많아질수록(이
Plugin은 실제로 수십 개를 등록해 쓴다) 그중 하나의 환경변수 설정이
누락되거나 틀릴 가능성은 항상 있는데, 예전에는 그 하나 때문에
`ConnectionManager.load()`가 `AuthenticationError`를 던져 `server/main.py`의
`main()`이 그대로 죽었다 — Claude Code 쪽에서는 이게 그냥 "MCP 서버 연결
끊김(CONNECTION_CLOSED)"으로만 보여서 원인 파악이 어려웠다. 지금은 실패한
Connection만 건너뛰고 `load_errors`에 사유를 남긴 채, 나머지는 정상
동작한다.
"""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path
from typing import Any

import yaml

from server.connection.ssh_tunnel import SSHTunnel
from server.database.base import BaseAdapter
from server.database.errors import AuthenticationError, DBConnectionError
from server.database.registry import AdapterRegistry

DEFAULT_CONNECTION_TIMEOUT_SEC = 5
DEFAULT_QUERY_TIMEOUT_SEC = 30
DEFAULT_SSH_TIMEOUT_SEC = 10


class ConnectionManager:
    def __init__(self, config_path: str | Path = "config/connections.yaml"):
        self._config_path = Path(config_path)
        self._profiles: dict[str, dict[str, Any]] = {}
        self._load_errors: dict[str, str] = {}

        # dao.py의 obj_db 싱글턴 대응: 이름당 Adapter를 1개만 만들어 재사용한다.
        self._adapters: dict[str, BaseAdapter] = {}
        self._tunnels: dict[str, SSHTunnel] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # 설정 로드
    # ------------------------------------------------------------------

    def load(self) -> None:
        if not self._config_path.exists():
            raise DBConnectionError(
                f"{self._config_path} 가 존재하지 않습니다. "
                f"config/connections.example.yaml을 복사해서 만드세요."
            )
        raw = yaml.safe_load(self._config_path.read_text(encoding="utf-8")) or {}
        connections = raw.get("connections", {})

        profiles: dict[str, dict[str, Any]] = {}
        load_errors: dict[str, str] = {}
        for name, cfg in connections.items():
            try:
                profile = dict(cfg)
                profile["name"] = name
                profile["user"] = self._resolve_env(cfg.get("user_env"))
                profile["password"] = self._resolve_env(cfg.get("password_env"))
                ssh_cfg = cfg.get("ssh")
                if ssh_cfg:
                    # Bastion 로그인 정보도 DB 계정과 동일한 원칙(§9)을 따른다 —
                    # yaml에는 환경변수 "이름"만 적고, 여기서 실제 값으로 resolve한다.
                    resolved_ssh = dict(ssh_cfg)
                    if ssh_cfg.get("password_env"):
                        resolved_ssh["password"] = self._resolve_env(ssh_cfg["password_env"])
                    if ssh_cfg.get("passphrase_env"):
                        resolved_ssh["passphrase"] = self._resolve_env(ssh_cfg["passphrase_env"])
                    profile["ssh"] = resolved_ssh
                profiles[name] = profile
            except AuthenticationError as exc:
                # Connection 하나의 설정 누락/오류가 서버 전체를 죽이면 안 된다
                # — 이 Connection만 건너뛰고 나머지는 정상적으로 쓸 수 있게 한다.
                load_errors[name] = str(exc)
                print(
                    f"[dbExpert] Connection {name!r} 로드 실패, 건너뜁니다: {exc}",
                    file=sys.stderr,
                )
        self._profiles = profiles
        self._load_errors = load_errors

    def list_load_errors(self) -> dict[str, str]:
        """load() 시점에 자격증명을 확인하지 못해 건너뛴 Connection과 사유.

        `list_connections()`에는 나타나지 않는(=사용할 수 없는) Connection을
        진단할 때 쓴다."""
        return dict(self._load_errors)

    @staticmethod
    def _resolve_env(env_var: str | None) -> str | None:
        """비밀번호/계정을 평문으로 저장하지 않는다는 원칙(§9) —
        connections.yaml에는 실제 값 대신 환경변수 이름만 적고 여기서 resolve한다."""
        if not env_var:
            return None
        value = os.environ.get(env_var)
        if value is None:
            raise AuthenticationError(f"환경변수 {env_var} 가 설정되어 있지 않습니다.")
        return value

    def list_connections(self) -> list[dict[str, Any]]:
        return [
            {"name": name, "db_type": profile["db_type"]}
            for name, profile in self._profiles.items()
        ]

    # ------------------------------------------------------------------
    # Adapter 조회/생성 (지연 생성 + 캐시)
    # ------------------------------------------------------------------

    def get_connection(self, name: str) -> BaseAdapter:
        adapter = self._adapters.get(name)
        if adapter is not None:
            return adapter

        with self._lock:
            adapter = self._adapters.get(name)
            if adapter is not None:  # 락 대기 중 다른 스레드가 먼저 만들었을 수 있음
                return adapter

            profile = self._profiles.get(name)
            if profile is None:
                if name in self._load_errors:
                    raise AuthenticationError(
                        f"{name!r}은(는) 설정은 있지만 자격증명을 확인하지 못해 "
                        f"쓸 수 없습니다: {self._load_errors[name]}"
                    )
                raise DBConnectionError(f"등록되지 않은 connection 입니다: {name!r}")

            effective_profile = self._resolve_bastion_if_needed(name, profile)
            adapter_class = AdapterRegistry.resolve(effective_profile["db_type"])
            adapter = adapter_class(effective_profile)
            self._adapters[name] = adapter
            return adapter

    # Engine마다 접속 대상 host를 담는 필드명이 다르다(RDB/DW/ES/Redis는
    # "host", MongoDB의 단일 host 방식은 "uri_host") — Bastion 터널은 이
    # 필드를 "127.0.0.1"로 바꿔치기해야 하므로 순서대로 찾아본다.
    _HOST_FIELD_CANDIDATES = ("host", "uri_host")

    def _resolve_bastion_if_needed(
        self, name: str, profile: dict[str, Any]
    ) -> dict[str, Any]:
        ssh_cfg = profile.get("ssh") or {}
        if not ssh_cfg.get("enabled"):
            return profile

        connection = profile["connection"]
        host_field = next((f for f in self._HOST_FIELD_CANDIDATES if f in connection), None)
        if host_field is None or "port" not in connection:
            raise DBConnectionError(
                f"{name!r}: connection.uri(전체 연결 문자열)나 "
                "connection.cluster_mode(다중 노드)처럼 단일 host/port로 표현되지 "
                "않는 Connection은 SSH Bastion 터널을 자동으로 구성할 수 없습니다. "
                "이 Connection은 ssh.enabled를 false로 두고 Bastion에서 직접 "
                "포트를 열어 접속하거나, 단일 host/port 방식으로 바꾸세요."
            )

        tunnel = SSHTunnel(
            ssh_host=ssh_cfg["host"],
            ssh_port=ssh_cfg.get("port", 22),
            ssh_user=ssh_cfg["user"],
            login_mode=ssh_cfg.get("login_mode", "pem"),
            identity_file=ssh_cfg.get("identity_file"),
            passphrase=ssh_cfg.get("passphrase"),  # load()에서 passphrase_env로부터 resolve됨
            password=ssh_cfg.get("password"),  # load()에서 password_env로부터 resolve됨
            remote_host=connection[host_field],
            remote_port=connection["port"],
            timeout_sec=DEFAULT_SSH_TIMEOUT_SEC,
        )
        local_port = tunnel.open()
        self._tunnels[name] = tunnel

        # Adapter는 실제로는 로컬로 포워딩된 포트에 접속하므로, 그 자리만 바꿔서 넘긴다.
        effective_profile = dict(profile)
        effective_connection = dict(connection)
        effective_connection[host_field] = "127.0.0.1"
        effective_connection["port"] = local_port
        effective_profile["connection"] = effective_connection
        return effective_profile

    # ------------------------------------------------------------------
    # 종료
    # ------------------------------------------------------------------

    def release(self, name: str) -> None:
        """단일 connection만 정리한다 (평소에는 close_all()을 atexit에서 호출)."""
        with self._lock:
            adapter = self._adapters.pop(name, None)
            tunnel = self._tunnels.pop(name, None)
        if adapter is not None:
            adapter.close()
        if tunnel is not None:
            tunnel.close()

    def close_all(self) -> None:
        """dao.py의 Database.close()/atexit 대응 — 프로세스 종료 시 전체 정리."""
        with self._lock:
            adapters = list(self._adapters.values())
            tunnels = list(self._tunnels.values())
            self._adapters.clear()
            self._tunnels.clear()
        for adapter in adapters:
            try:
                adapter.close()
            except Exception:  # noqa: BLE001 — 종료 중 에러는 무시하고 계속 정리
                pass
        for tunnel in tunnels:
            try:
                tunnel.close()
            except Exception:  # noqa: BLE001
                pass
