"""통합 테스트 공통 설정.

tests/(단위 테스트)와 달리 이 디렉터리는 실제 Docker 컨테이너(MySQL/
PostgreSQL)를 띄워 Adapter가 진짜 DB에 대해 동작하는지 검증한다
(Doc/00_개발요건사항.md §29 Read Only 이중 방어, §33 Timeout이 실제로
지켜지는지 — Fake Adapter로는 증명할 수 없는 부분).

`dev` extras(testcontainers)가 없거나 Docker Daemon에 붙을 수 없으면 이
디렉터리 전체를 건너뛴다 — 기본 `pytest tests/` 실행이 Docker 유무와
무관하게 항상 안전하게 끝나야 하기 때문이다.
"""
from __future__ import annotations

import pytest

pytest.importorskip("testcontainers")


def _docker_available() -> bool:
    try:
        import docker

        client = docker.from_env()
        client.ping()
        return True
    except Exception:
        return False


if not _docker_available():
    pytest.skip(
        "Docker Daemon에 연결할 수 없어 통합 테스트를 건너뜁니다.",
        allow_module_level=True,
    )
