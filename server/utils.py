"""Tool 실행 계층(analyzer/pipeline/catalog/learning)이 공통으로 쓰는 유틸.

Adapter 메서드는 base.py의 dataclass(SlowOperation, Lock, Session 등)를
반환하는데, MCP Tool은 JSON으로 직렬화 가능한 dict/list/str/number만
반환해야 하므로 여기서 재귀적으로 변환한다.
"""
from __future__ import annotations

import dataclasses
from datetime import datetime
from typing import Any


def to_serializable(value: Any) -> Any:
    """dataclass / datetime을 포함한 값을 JSON 직렬화 가능한 구조로 재귀 변환한다."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            f.name: to_serializable(getattr(value, f.name))
            for f in dataclasses.fields(value)
        }
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: to_serializable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_serializable(v) for v in value]
    return value
