"""공통 예외 클래스.

에러 코드 매핑은 Doc/01_개발정의서.md §8 에러 코드 정의를 그대로 따른다.
"""
from __future__ import annotations


class DBConnectionError(Exception):
    """CONNECTION_ERROR — DB 연결 실패/타임아웃."""


class SSHConnectionError(Exception):
    """SSH_CONNECTION_ERROR — Bastion 연결 실패."""


class AuthenticationError(Exception):
    """AUTH_ERROR — 인증 실패."""


class DatabaseQueryError(Exception):
    """QUERY_ERROR — SQL/Query 실행 오류."""


class QueryTimeoutError(Exception):
    """QUERY_TIMEOUT — Query Timeout 초과."""


class ReadOnlyViolationError(Exception):
    """DDL_BLOCKED — Read Only 원칙 위반 구문 감지.

    Doc/00_개발요건사항.md §29 보안 요구사항의 "이중 방어" 중 두 번째 방어선
    (DB 계정 자체는 Read Only 권한 + 여기서 구문 단위로 한 번 더 차단).
    """


class ObjectNotFoundError(Exception):
    """OBJECT_NOT_FOUND — 지정한 Namespace/Container/Field가 존재하지 않음."""


class AmbiguousObjectError(Exception):
    """AMBIGUOUS_OBJECT — search_object_catalog 결과 유사도가 근접한 후보가 다수."""
