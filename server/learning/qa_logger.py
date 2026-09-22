"""log_qa Tool 구현.

Tool 시그니처, QALogEntry Schema: Doc/01_개발정의서.md §3.6, §4.4.
저장 위치: data/qa_logs/<connection>/YYYY-MM-DD.jsonl (append-only).
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

DATA_DIR = Path("data/qa_logs")

# SQL 리터럴(문자열/숫자)을 Prepared Statement 형태로 마스킹한다.
# 완전한 SQL Parser는 아니지만, 로그에 실제 값이 남지 않도록 하는 것이 목적이다.
_STRING_LITERAL_RE = re.compile(r"'[^']*'")
_NUMBER_LITERAL_RE = re.compile(r"(?<![A-Za-z0-9_])\d+(?:\.\d+)?")


def build_qa_entry(
    connection_name: str,
    skill: str,
    question: str,
    tools_used: list[str],
    objects_referenced: list[str],
    answer_summary: str,
    duration_ms: float,
    sql_used: Optional[list[str]] = None,
) -> dict[str, Any]:
    """QALogEntry(Doc §4.4) 하나를 조립한다. 디스크 I/O는 하지 않는다 —
    `write_qa_entry()`가 실제 쓰기를 담당하며, 이 둘을 분리한 이유는
    `learning/background_writer.py`가 "조립은 즉시, 쓰기는 나중에"
    (CPU 여유/Claude 유휴 시점) 패턴을 쓰기 때문이다."""
    now = datetime.now(timezone.utc)
    return {
        "log_id": str(uuid.uuid4()),
        "timestamp": now.isoformat(),
        "connection": connection_name,
        "skill": skill,
        "question": question,
        "tools_used": tools_used,
        "objects_referenced": objects_referenced,
        "sql_used": _mask_sql_params(sql_used or []),
        "answer_summary": answer_summary,
        "duration_ms": duration_ms,
    }


def write_qa_entry(entry: dict[str, Any]) -> None:
    """build_qa_entry()가 만든 항목 하나를 실제로 JSONL에 append한다."""
    connection_dir = DATA_DIR / entry["connection"]
    connection_dir.mkdir(parents=True, exist_ok=True)
    log_date = entry["timestamp"][:10]  # ISO 8601 문자열의 YYYY-MM-DD 부분
    log_path = connection_dir / f"{log_date}.jsonl"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def log_qa(
    connection_name: str,
    skill: str,
    question: str,
    tools_used: list[str],
    objects_referenced: list[str],
    answer_summary: str,
    duration_ms: float,
    sql_used: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Skill 실행 1건을 조립해 즉시 JSONL로 append한다(동기 버전).

    main.py의 실제 log_qa Tool은 이 함수 대신 build_qa_entry() +
    learning/background_writer.py의 Queue를 사용해 쓰기를 지연시킨다 — 이
    함수는 그 지연 없이 즉시 기록해야 하는 경우(테스트, 직접 호출)를 위해
    남겨둔다.

    Returns: { "logged": bool, "log_id": str }
    """
    entry = build_qa_entry(
        connection_name,
        skill,
        question,
        tools_used,
        objects_referenced,
        answer_summary,
        duration_ms,
        sql_used,
    )
    write_qa_entry(entry)
    return {"logged": True, "log_id": entry["log_id"]}


def _mask_sql_params(sql_list: list[str]) -> list[str]:
    """SQL의 리터럴 값을 Prepared Statement 형태(`?`)로 마스킹한다."""
    masked = []
    for sql in sql_list:
        sql = _STRING_LITERAL_RE.sub("?", sql)
        sql = _NUMBER_LITERAL_RE.sub("?", sql)
        masked.append(sql)
    return masked
