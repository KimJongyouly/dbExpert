"""log_qa를 CPU 유휴/Claude 유휴 상태까지 미루는 QALogWriter 단위 테스트.

실제 psutil/시간 흐름에 의존하지 않도록 cpu_percent_fn/idle_seconds_fn을
주입해서 조건을 결정론적으로 제어한다.
"""
from __future__ import annotations

import time

from server.learning.background_writer import QALogWriter


def _writer(cpu_percent: float, idle_seconds: float, **overrides) -> tuple[QALogWriter, list]:
    written: list = []
    writer = QALogWriter(
        write_entry=written.append,
        cpu_percent_fn=lambda: cpu_percent,
        idle_seconds_fn=lambda: idle_seconds,
        poll_interval_sec=0.05,
        **overrides,
    )
    return writer, written


def test_is_idle_true_when_cpu_low_and_no_recent_activity():
    writer, _ = _writer(cpu_percent=10.0, idle_seconds=30.0)
    assert writer.is_idle() is True


def test_is_idle_false_when_cpu_high_even_if_no_recent_activity():
    writer, _ = _writer(cpu_percent=90.0, idle_seconds=30.0)
    assert writer.is_idle() is False


def test_is_idle_false_when_activity_recent_even_if_cpu_low():
    writer, _ = _writer(cpu_percent=10.0, idle_seconds=0.5)
    assert writer.is_idle() is False


def test_enqueued_entry_is_not_written_while_system_busy():
    writer, written = _writer(cpu_percent=95.0, idle_seconds=999.0)
    writer.start()
    try:
        writer.enqueue({"log_id": "1"})
        time.sleep(0.2)  # 여러 poll 주기가 지나도 flush되지 않아야 한다
        assert written == []
        assert writer.pending_count() == 1
    finally:
        writer.stop()


def test_enqueued_entry_is_written_once_system_becomes_idle():
    writer, written = _writer(cpu_percent=95.0, idle_seconds=999.0)
    writer.start()
    try:
        writer.enqueue({"log_id": "1"})
        time.sleep(0.15)
        assert written == []  # 아직 바쁨

        # 이제 한가해졌다고 조건을 바꾼다.
        writer._cpu_percent_fn = lambda: 5.0
        time.sleep(0.2)
        assert written == [{"log_id": "1"}]
        assert writer.pending_count() == 0
    finally:
        writer.stop()


def test_flush_all_writes_immediately_regardless_of_idle_state():
    """atexit에서 호출되는 경로 — 컴퓨터가 바빠도 유실 없이 즉시 flush해야 한다."""
    writer, written = _writer(cpu_percent=100.0, idle_seconds=0.0)
    writer.enqueue({"log_id": "a"})
    writer.enqueue({"log_id": "b"})

    writer.flush_all()

    assert written == [{"log_id": "a"}, {"log_id": "b"}]
    assert writer.pending_count() == 0


def test_write_failure_does_not_crash_background_thread():
    def _raise(entry):
        raise RuntimeError("disk full")

    writer = QALogWriter(
        write_entry=_raise,
        cpu_percent_fn=lambda: 0.0,
        idle_seconds_fn=lambda: 999.0,
        poll_interval_sec=0.05,
    )
    writer.start()
    try:
        writer.enqueue({"log_id": "1"})
        time.sleep(0.2)
        # 예외가 나도 스레드가 죽지 않고, 항목은 큐에서 소비되어야 한다
        # (재시도 폭주를 막기 위해 실패한 항목은 버린다).
        assert writer.pending_count() == 0
        assert writer._thread.is_alive()
    finally:
        writer.stop()
