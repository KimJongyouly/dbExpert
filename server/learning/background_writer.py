"""log_qa 기록을 CPU 여유/Claude 유휴 상태로 미루는 백그라운드 기록기.

Doc/01_개발정의서.md §3.6/§11 "log_qa 호출 주체" 관련 — Tool 자체는 여전히
Claude(Skill)가 명시적으로 호출한다. 다만 실제 JSONL 디스크 쓰기는 이
Writer가 맡아, Tool 호출 시점에는 메모리 Queue에 넣기만 하고 즉시
반환한다(사용자 응답 지연 없음). 실제 쓰기는 아래 두 조건을 모두 만족할
때만 일어난다:
  - CPU 사용률이 `idle_cpu_threshold`(기본 50%) 이하 — "컴퓨터가 한가함"
  - 마지막 MCP Tool 호출(`server/activity.py`)로부터 `idle_activity_sec`
    (기본 10초) 이상 지남 — "Claude 작업이 없음"의 대리 지표

프로세스가 종료될 때(atexit)는 조건과 무관하게 남은 항목을 전부 flush한다
— 그렇지 않으면 컴퓨터가 계속 바쁜 상태로 프로세스가 끝나 로그가 조용히
유실될 수 있기 때문이다.
"""
from __future__ import annotations

import atexit
import queue
import threading
from typing import Any, Callable, Optional

from server import activity

IDLE_CPU_THRESHOLD_PERCENT = 50.0
IDLE_ACTIVITY_SEC = 10.0
FLUSH_POLL_INTERVAL_SEC = 5.0


def _default_cpu_percent_fn() -> Callable[[], float]:
    import psutil  # 지연 import — 이 모듈을 쓰지 않는 테스트 등에서는 불필요

    # interval=None: 논블로킹 — 직전 호출 이후 누적치를 즉시 반환한다.
    # 첫 호출은 기준선이 없어 0.0이 나오므로, 여기서 한 번 "워밍업" 호출을
    # 해서 기준선을 등록해 둔다(psutil 공식 권장 패턴).
    psutil.cpu_percent(interval=None)
    return lambda: psutil.cpu_percent(interval=None)


class QALogWriter:
    """Q&A 로그 항목을 Queue에 모았다가, 시스템이 한가할 때만 flush한다."""

    def __init__(
        self,
        write_entry: Callable[[dict[str, Any]], None],
        cpu_percent_fn: Optional[Callable[[], float]] = None,
        idle_seconds_fn: Callable[[], float] = activity.idle_seconds,
        idle_cpu_threshold: float = IDLE_CPU_THRESHOLD_PERCENT,
        idle_activity_sec: float = IDLE_ACTIVITY_SEC,
        poll_interval_sec: float = FLUSH_POLL_INTERVAL_SEC,
    ):
        self._write_entry = write_entry
        self._cpu_percent_fn = cpu_percent_fn or _default_cpu_percent_fn()
        self._idle_seconds_fn = idle_seconds_fn
        self._idle_cpu_threshold = idle_cpu_threshold
        self._idle_activity_sec = idle_activity_sec
        self._poll_interval_sec = poll_interval_sec

        self._queue: "queue.Queue[dict[str, Any]]" = queue.Queue()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._atexit_registered = False

    def start(self) -> None:
        """백그라운드 flush 스레드를 시작한다. 이미 시작했으면 아무 일도 하지 않는다."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="qa-log-writer", daemon=True
        )
        self._thread.start()
        if not self._atexit_registered:
            atexit.register(self.flush_all)
            self._atexit_registered = True

    def enqueue(self, entry: dict[str, Any]) -> None:
        self._queue.put(entry)

    def pending_count(self) -> int:
        return self._queue.qsize()

    def is_idle(self) -> bool:
        return (
            self._cpu_percent_fn() <= self._idle_cpu_threshold
            and self._idle_seconds_fn() >= self._idle_activity_sec
        )

    def _run(self) -> None:
        while not self._stop_event.wait(self._poll_interval_sec):
            if not self._queue.empty() and self.is_idle():
                self._flush_pending()

    def _flush_pending(self) -> None:
        while True:
            try:
                entry = self._queue.get_nowait()
            except queue.Empty:
                return
            try:
                self._write_entry(entry)
            except Exception:  # noqa: BLE001 — 기록 실패로 백그라운드 스레드가 죽으면 안 됨
                pass

    def flush_all(self) -> None:
        """조건과 무관하게 남은 항목을 즉시 전부 flush한다(프로세스 종료 시 유실 방지)."""
        self._flush_pending()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=self._poll_interval_sec + 1)
            self._thread = None
