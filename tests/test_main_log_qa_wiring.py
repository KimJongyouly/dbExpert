"""main.py의 log_qa Tool이 즉시 디스크에 쓰는 대신 qa_log_writer Queue에
넣기만 하는지, Tool 호출이 activity.touch()를 갱신하는지에 대한 통합 테스트.
"""
from __future__ import annotations

import server.main as main_module
from server import activity


def test_log_qa_tool_enqueues_instead_of_writing_immediately(monkeypatch):
    written: list = []
    monkeypatch.setattr(main_module.qa_log_writer, "_write_entry", written.append)
    # 백그라운드 스레드를 켜지 않은 상태에서도(테스트 환경) enqueue만 되는지 확인.

    result = main_module.log_qa(
        connection="prod-aurora",
        skill="slow-query",
        question="왜 느려?",
        tools_used=["slow_query_list"],
        objects_referenced=["orders"],
        answer_summary="Full scan 발견",
        duration_ms=120.0,
    )

    assert result["logged"] is True
    assert result["queued"] is True
    assert written == []  # 아직 flush 안 됨 — Queue에만 들어감
    assert main_module.qa_log_writer.pending_count() == 1


def test_calling_any_tool_updates_activity_timestamp():
    import time

    activity.touch()
    time.sleep(0.1)
    assert activity.idle_seconds() >= 0.1  # touch() 이후 아무 일도 없었으니 유휴 시간이 쌓임

    try:
        main_module.db_ping("nonexistent-connection")
    except Exception:
        pass  # 실제 연결 실패는 이 테스트의 관심사가 아니다 — activity.touch() 호출 여부만 본다

    # Tool 호출(@_tool 데코레이터)이 activity.touch()를 다시 불렀어야 하므로
    # 유휴 시간이 거의 0으로 리셋된다.
    assert activity.idle_seconds() < 0.05
