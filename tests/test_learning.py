"""learning/*.py(Q&A Logging / 쿼리 패턴 요약)에 대한 단위 테스트.

전부 파일 기반이라 DATA_DIR을 tmp_path로 monkeypatch해서 실제 data/
디렉터리를 건드리지 않는다.
"""
from __future__ import annotations

import json

import server.learning.pattern_summarizer as pattern_summarizer_module
import server.learning.qa_logger as qa_logger_module
from server.learning.pattern_summarizer import get_query_patterns, summarize_query_patterns
from server.learning.qa_logger import log_qa


def test_log_qa_appends_jsonl_and_masks_sql_literals(tmp_path, monkeypatch):
    monkeypatch.setattr(qa_logger_module, "DATA_DIR", tmp_path)

    result = log_qa(
        connection_name="prod-aurora",
        skill="slow-query",
        question="5초 이상 걸리는 쿼리 분석해줘",
        tools_used=["slow_query_list"],
        objects_referenced=["orders"],
        answer_summary="Full Scan으로 판단",
        duration_ms=120.5,
        sql_used=["SELECT * FROM orders WHERE customer_id = 42 AND name = 'kim'"],
    )
    assert result["logged"] is True
    assert result["log_id"]

    log_files = list((tmp_path / "prod-aurora").glob("*.jsonl"))
    assert len(log_files) == 1

    lines = log_files[0].read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["log_id"] == result["log_id"]
    assert entry["sql_used"] == ["SELECT * FROM orders WHERE customer_id = ? AND name = ?"]


def test_log_qa_appends_multiple_entries_same_day(tmp_path, monkeypatch):
    monkeypatch.setattr(qa_logger_module, "DATA_DIR", tmp_path)
    for i in range(3):
        log_qa("prod-aurora", "slow-query", f"q{i}", [], [], "", 1.0)

    log_files = list((tmp_path / "prod-aurora").glob("*.jsonl"))
    assert len(log_files) == 1
    assert len(log_files[0].read_text(encoding="utf-8").strip().splitlines()) == 3


# ---------------------------------------------------------------------------
# summarize_query_patterns / get_query_patterns
# ---------------------------------------------------------------------------


def _write_log(tmp_path, connection, filename, entries):
    conn_dir = tmp_path / connection
    conn_dir.mkdir(parents=True, exist_ok=True)
    with (conn_dir / filename).open("w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def test_summarize_and_get_query_patterns(tmp_path, monkeypatch):
    monkeypatch.setattr(pattern_summarizer_module, "DATA_DIR", tmp_path)

    _write_log(
        tmp_path,
        "prod-aurora",
        "2026-09-09.jsonl",
        [
            {
                "timestamp": "2026-09-09T10:00:00+00:00",
                "skill": "slow-query",
                "question": "고객별 최근 주문 조회가 느려요",
                "objects_referenced": ["orders", "customers"],
                "sql_used": ["SELECT * FROM orders WHERE customer_id = ?"],
            },
            {
                "timestamp": "2026-09-09T11:00:00+00:00",
                "skill": "slow-query",
                "question": "비슷한 질문 또",
                "objects_referenced": ["customers", "orders"],  # 순서만 다름 -> 같은 클러스터
                "sql_used": ["SELECT * FROM orders WHERE customer_id = ?"],
            },
            {
                "timestamp": "2026-09-09T12:00:00+00:00",
                "skill": "index-tuning",
                "question": "인덱스 추천해줘",
                "objects_referenced": ["orders"],
                "sql_used": [],
            },
        ],
    )

    result = summarize_query_patterns()
    assert result["patterns_upserted"] == 2  # (slow-query, {orders,customers}) + (index-tuning, {orders})
    assert result["connections_processed"] == ["prod-aurora"]

    patterns_result = get_query_patterns("prod-aurora")
    assert len(patterns_result["patterns"]) == 2
    top = patterns_result["patterns"][0]
    assert top["frequency"] == 2
    assert set(top["related_objects"]) == {"orders", "customers"}

    filtered = get_query_patterns("prod-aurora", related_object="orders")
    assert len(filtered["patterns"]) == 2  # 둘 다 orders를 참조

    filtered_customers = get_query_patterns("prod-aurora", related_object="customers")
    assert len(filtered_customers["patterns"]) == 1


def test_summarize_query_patterns_accumulates_frequency_across_runs(tmp_path, monkeypatch):
    monkeypatch.setattr(pattern_summarizer_module, "DATA_DIR", tmp_path)
    entry = {
        "timestamp": "2026-09-09T10:00:00+00:00",
        "skill": "slow-query",
        "question": "q",
        "objects_referenced": ["orders"],
        "sql_used": [],
    }
    _write_log(tmp_path, "prod-aurora", "2026-09-09.jsonl", [entry])
    summarize_query_patterns()

    _write_log(tmp_path, "prod-aurora", "2026-09-10.jsonl", [{**entry, "timestamp": "2026-09-10T10:00:00+00:00"}])
    summarize_query_patterns()

    patterns = get_query_patterns("prod-aurora")["patterns"]
    assert len(patterns) == 1
    assert patterns[0]["frequency"] == 2
    assert patterns[0]["last_seen"] == "2026-09-10T10:00:00+00:00"


def test_get_query_patterns_returns_empty_when_no_history():
    result = get_query_patterns("no-such-connection")
    assert result == {"patterns": []}
