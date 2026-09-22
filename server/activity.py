"""MCP Server 전체의 최근 활동 시각을 추적한다.

`server/learning/background_writer.py`가 "Claude 작업이 없다"를 판단하는
근거로 쓴다 — 이 프로세스는 stdio로 뜨는 MCP Server라서 Claude Code 자체의
작업 여부를 직접 알 방법이 없고, "최근에 어떤 Tool이든 호출됐는가"를 대리
지표로 쓴다. `main.py`의 모든 `@mcp.tool()` 함수가 호출될 때마다 `touch()`를
부른다(`_tool` 데코레이터).
"""
from __future__ import annotations

import threading
import time

_lock = threading.Lock()
_last_activity_at = time.monotonic()


def touch() -> None:
    global _last_activity_at
    with _lock:
        _last_activity_at = time.monotonic()


def idle_seconds() -> float:
    with _lock:
        return time.monotonic() - _last_activity_at
