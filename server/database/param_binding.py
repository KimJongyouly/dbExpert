"""Named Parameter(`:name`) 표기를 Driver별 Paramstyle로 변환.

server/sql/<engine>/*.sql 은 전부 `:name` 표기(named)로 통일해서 작성한다
(Doc/00_개발요건사항.md §30 SQL Injection 방지 — 값은 반드시 Parameter Binding
으로 전달하고 문자열 포매팅으로 SQL에 직접 삽입하지 않는다).

Driver마다 실제로 받는 Paramstyle이 다르므로 이 모듈에서 변환한다.
  - "pyformat": %(name)s 형태 + dict 그대로 전달 (PyMySQL, psycopg)
  - "qmark"   : ? 형태(위치 기반) + list로 순서를 맞춰 전달 (pyodbc, pymssql)
ClickHouse는 `{name:Type}` 자체 문법을 쓰므로 이 변환기를 거치지 않는다
(server/database/clickhouse.py 참고).
"""
from __future__ import annotations

import re
from typing import Any, Literal

ParamStyle = Literal["pyformat", "qmark"]

# (?<!:) — PostgreSQL의 `::type` 캐스트 문법(예: c.reltuples::bigint)을
# 자리표시자로 잘못 인식하지 않도록, 콜론 앞에 또 다른 콜론이 오면 제외한다.
# 실제로 tests/integration/test_postgresql_integration.py에서 `::bigint`가
# `:bigint` 파라미터로 오인되어 "query parameter missing: bigint" 오류가
# 나는 걸 발견하고 고쳤다.
_NAMED_PARAM_RE = re.compile(r"(?<!:):([A-Za-z_][A-Za-z0-9_]*)")


def convert_named_params(
    sql: str, params: dict[str, Any] | None, style: ParamStyle
) -> tuple[str, Any]:
    """`:name` 표기를 style에 맞게 치환하고, 그에 맞는 파라미터 컨테이너를 반환한다."""
    if not params:
        return sql, ({} if style == "pyformat" else [])

    if style == "pyformat":
        converted = _NAMED_PARAM_RE.sub(lambda m: f"%({m.group(1)})s", sql)
        return converted, params

    if style == "qmark":
        order: list[str] = []

        def _replace(match: re.Match[str]) -> str:
            order.append(match.group(1))
            return "?"

        converted = _NAMED_PARAM_RE.sub(_replace, sql)
        try:
            positional = [params[name] for name in order]
        except KeyError as exc:
            raise ValueError(f"SQL에 필요한 파라미터가 누락되었습니다: {exc}") from exc
        return converted, positional

    raise ValueError(f"지원하지 않는 paramstyle: {style}")


def build_in_clause(param_prefix: str, values: list[Any]) -> tuple[str, dict[str, Any]]:
    """`IN (:p0, :p1, ...)` 형태의 가변 길이 IN 절을 만든다.

    Table명 목록처럼 개수가 정해지지 않은 값을 안전하게 Parameter Binding
    하기 위한 헬퍼 — 자리표시자 이름만 동적으로 생성할 뿐, 값은 항상
    execute_readonly의 params를 통해 바인딩된다.
    """
    names = [f"{param_prefix}{i}" for i in range(len(values))]
    placeholders = ", ".join(f":{n}" for n in names)
    params = {n: v for n, v in zip(names, values)}
    return placeholders, params
