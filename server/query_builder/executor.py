"""execute_readonly_query Tool 구현 (`query-builder` Skill).

자연어로 유추한 임의의 조회 쿼리를 실제로 실행해 결과 행을 반환하는
범용 Tool이다. 기존 Tool(slow_query_list, get_table_statistics,
sample_rows 등)은 전부 특정 목적에 고정돼 있어서 "이런 조건의 데이터
보여줘" 같은 자유 조회에는 쓸 수 없었다 — SQL 자체는 Claude가 만들고
(`search_object_catalog`/`get_schema`로 테이블·컬럼을 먼저 확인한 뒤),
이 Tool은 그걸 안전하게 실행만 한다(Doc/00_개발요건사항.md §6 원칙 —
판단/생성은 Claude, Python은 사실만 제공).

Read Only 방어는 새로 만들지 않고 기존 `adapter.execute_readonly()`의
3중 방어를 그대로 재사용한다: (1) `sql_guard.assert_readonly_sql()` 텍스트
검사, (2) MySQL/PostgreSQL/ClickHouse/Tibero는 연결 세션 자체가 서버
레벨에서 Read Only로 강제됨, (3) `query_timeout_sec`로 실행 시간이 제한됨
(Doc/00 §29).

⚠️ 한계: 결과 행은 DB에서 전부 가져온 뒤 Python 쪽에서 `limit`으로 자른다
(`sample_rows`와 동일한 방식) — SQL 자체에 이미 LIMIT을 걸지 않으면 대량
결과를 서버가 다 처리할 때까지 기다려야 한다. `query_timeout_sec`가 이
경우의 시간 상한 역할을 하지만, 가능하면 Claude가 SQL 구성 시 스스로
LIMIT을 포함하는 것을 권장한다(SKILL.md에 명시).
"""
from __future__ import annotations

from typing import Any, Optional

from server.database.base import BaseAdapter, Capability

_MAX_LIMIT = 1000
_DEFAULT_LIMIT = 100


def execute_readonly_query(
    adapter: BaseAdapter,
    sql: str,
    limit: int = _DEFAULT_LIMIT,
    mask: Optional[list[str]] = None,
) -> dict[str, Any]:
    """자연어로 구성한 조회 SQL을 실행해 결과 행을 반환한다.

    Returns: { "rows": dict[], "row_count": int, "truncated": bool }
    """
    if not adapter.supports(Capability.SQL):
        return {"status": "CAPABILITY_NOT_SUPPORTED", "capability": "sql"}

    limit = min(limit, _MAX_LIMIT)
    rows = adapter.execute_readonly(sql)
    truncated = len(rows) > limit
    rows = rows[:limit]

    if mask:
        for row in rows:
            for col in mask:
                if col in row:
                    row[col] = "***"

    return {"rows": rows, "row_count": len(rows), "truncated": truncated}
