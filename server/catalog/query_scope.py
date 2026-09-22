"""SQL 문에서 FROM/JOIN 뒤에 오는 Table명을 뽑아낸다.

`server/analyzer/index_advisor.py`(WHERE/JOIN ON/ORDER BY 컬럼 추출)와 같은
실용적 정규식 기반 추출이다 — 완전한 SQL Parser는 아니라서 서브쿼리 별칭,
CTE 등을 완벽히 걸러내지는 못할 수 있다. 다만 이 함수의 용도가
"Ontology 자동 갱신 트리거"(`server/catalog/auto_refresh.py`)이기 때문에
오탐/누락이 있어도 정확성에 영향을 주지 않는다 — 최악의 경우 특정 Table의
자동 갱신 시점이 하루 늦어지거나, 존재하지 않는 이름으로 get_schema를
호출해 빈 결과를 받고 조용히 무시되는 정도다.
"""
from __future__ import annotations

import re

_FROM_JOIN_RE = re.compile(
    r"\b(?:FROM|JOIN)\s+"
    r"([`\"\[]?[A-Za-z_][A-Za-z0-9_]*[`\"\]]?(?:\.[`\"\[]?[A-Za-z_][A-Za-z0-9_]*[`\"\]]?)?)",
    re.IGNORECASE,
)
_QUOTE_CHARS = "`\"[]"


def extract_table_names(sql: str) -> list[str]:
    """SQL 문에 등장하는 Table명(스키마 접두사는 제거)을 중복 없이 반환한다."""
    if not sql:
        return []

    names: list[str] = []
    seen: set[str] = set()
    for match in _FROM_JOIN_RE.finditer(sql):
        raw = match.group(1)
        # "schema.table" 형태면 마지막 부분(table)만 취한다.
        table = raw.rsplit(".", 1)[-1].strip(_QUOTE_CHARS)
        if table and table not in seen:
            seen.add(table)
            names.append(table)
    return names
