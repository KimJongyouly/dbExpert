"""server/activity.py — 최근 Tool 활동 시각 추적에 대한 단위 테스트."""
from __future__ import annotations

import time

from server import activity


def test_idle_seconds_grows_over_time_without_touch():
    activity.touch()
    first = activity.idle_seconds()
    time.sleep(0.05)
    second = activity.idle_seconds()
    assert second > first


def test_touch_resets_idle_seconds_to_near_zero():
    activity.touch()
    time.sleep(0.1)
    assert activity.idle_seconds() >= 0.1
    activity.touch()
    assert activity.idle_seconds() < 0.05
