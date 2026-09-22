"""SQL 문 단위 Read-Only 가드.

Doc/00_개발요건사항.md §29 "이중 방어" 원칙 중 두 번째 방어선(Adapter 레벨
구문 감지)을 담당한다. 첫 번째 방어선(DB 계정 자체를 Read Only 권한으로
발급)은 운영 환경 사정에 따라 적용되지 않을 수 있다(별도 계정 발급이
어려워 관리 권한 계정을 그대로 쓰는 경우) — 그때는 이 가드가 사실상
유일한 텍스트 레벨 방어선이므로,
MySQL/PostgreSQL/ClickHouse/Tibero는 여기에 더해 연결 직후 세션 자체를
서버가 강제로 Read Only로 만드는 세 번째 방어선을 각 Adapter의
`_after_connect`/Client 생성 인자에 추가로 둔다(mysql.py `SET SESSION
TRANSACTION READ ONLY`, postgresql.py `default_transaction_read_only=on`,
clickhouse.py `settings={"readonly": 1}`, tibero.py `Connection.setReadOnly`).
이 서버 레벨 방어선은 "SELECT 안에 숨겨진 쓰기 부작용이 있는 함수/프로시저
호출"처럼 텍스트 검사로는 원천적으로 잡을 수 없는 영역까지 막아준다 —
관리자 계정을 쓰는 이상 이 가드 하나만으로는 "절대적" 방어를 보장할 수
없다는 뜻이며, MSSQL은 T-SQL에 세션 단위 Read Only 강제 수단이 없어
이 가드가 유일한 방어선으로 남는다(§11 참고).

완전한 SQL Parser가 아니라 실용적인 allow-list + block-list 조합이다:
  1. 세미콜론으로 여러 statement를 이어 붙이는 시도(Statement Stacking)를 차단
  2. SELECT/SHOW/EXPLAIN/WITH/DESC(RIBE) 로 시작하는 조회 구문만 허용
  3. 그 안에 DDL/DML로 의심되는 키워드가 섞여 있으면 추가로 차단
     (예: WITH 절 안에 INSERT ... RETURNING 을 끼워 넣는 형태 방어,
     SELECT ... INTO OUTFILE/DUMPFILE로 서버 파일을 쓰는 형태 방어,
     MSSQL의 SELECT ... INTO <table>로 테이블을 새로 만드는 형태 방어)
"""
from __future__ import annotations

import re

from server.database.errors import ReadOnlyViolationError

_ALLOWED_START_RE = re.compile(
    r"^\s*(?:--[^\n]*\n\s*)*(WITH|SELECT|SHOW|EXPLAIN|DESC|DESCRIBE)\b",
    re.IGNORECASE,
)

_FORBIDDEN_KEYWORDS = (
    "INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "TRUNCATE", "CREATE",
    "GRANT", "REVOKE", "MERGE", "CALL", "EXEC", "EXECUTE", "REPLACE",
    "LOCK", "UNLOCK", "COPY", "VACUUM", "REINDEX",
    # SELECT로 시작해도 서버에 쓰기 부작용을 일으키는 구문을 추가로 차단한다.
    "INTO", "OUTFILE", "DUMPFILE", "RENAME",
)
_FORBIDDEN_RE = re.compile(r"\b(" + "|".join(_FORBIDDEN_KEYWORDS) + r")\b", re.IGNORECASE)


def assert_readonly_sql(sql: str) -> None:
    """읽기 전용 구문이 아니면 ReadOnlyViolationError를 발생시킨다."""
    stripped = sql.strip()
    if not stripped:
        raise ReadOnlyViolationError("빈 SQL은 실행할 수 없습니다.")

    # 끝에 세미콜론 하나는 허용하되, 중간에 또 있으면 Statement Stacking으로 간주.
    body = stripped[:-1] if stripped.endswith(";") else stripped
    if ";" in body:
        raise ReadOnlyViolationError(
            "세미콜론으로 연결된 복수 statement는 허용하지 않습니다."
        )

    if not _ALLOWED_START_RE.match(stripped):
        raise ReadOnlyViolationError(
            "SELECT/SHOW/EXPLAIN/WITH/DESC 로 시작하는 조회 구문만 허용합니다."
        )

    if _FORBIDDEN_RE.search(stripped):
        raise ReadOnlyViolationError(
            "DDL/DML로 의심되는 키워드가 포함되어 있어 실행을 차단합니다."
        )
