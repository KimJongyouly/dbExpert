"""get_locks / get_sessions Tool 구현(session_monitor.py)에 대한 단위 테스트."""
from __future__ import annotations

from server.analyzer.session_monitor import get_locks, get_sessions
from server.database.base import ActivityCapable, BaseAdapter, Capability, Lock, Session


class _FakeAdapter(BaseAdapter, ActivityCapable):
    capabilities = {Capability.ACTIVITY}

    def __init__(self, locks=None, sessions=None):
        super().__init__({"connection": {}})
        self._locks = locks or []
        self._sessions = sessions or []

    def ping(self):
        return {"connected": True}

    def get_locks(self):
        return self._locks

    def get_sessions(self):
        return self._sessions


class _NoCapabilityAdapter(BaseAdapter):
    capabilities: set[Capability] = set()

    def ping(self):
        return {"connected": True}


def test_get_locks_capability_not_supported():
    result = get_locks(_NoCapabilityAdapter({}))
    assert result["status"] == "CAPABILITY_NOT_SUPPORTED"


def test_get_locks_serializes_locks():
    adapter = _FakeAdapter(locks=[Lock(holding_session_id="10", resource="orders", lock_type="X")])
    result = get_locks(adapter)
    assert result["locks"][0]["resource"] == "orders"


def test_get_sessions_capability_not_supported():
    result = get_sessions(_NoCapabilityAdapter({}))
    assert result["status"] == "CAPABILITY_NOT_SUPPORTED"


def _sample_sessions() -> list[Session]:
    return [
        Session(session_id="1", state="Query", current_operation="SELECT 1"),
        Session(session_id="2", state="Sleep"),
        Session(session_id="3", state="idle"),
        Session(session_id="4", state="ACTIVE"),
        Session(session_id="5", state="INACTIVE"),
    ]


def test_get_sessions_status_all_returns_everything():
    adapter = _FakeAdapter(sessions=_sample_sessions())
    result = get_sessions(adapter, status="all")
    assert len(result["sessions"]) == 5


def test_get_sessions_status_running_excludes_idle_variants():
    adapter = _FakeAdapter(sessions=_sample_sessions())
    result = get_sessions(adapter, status="running")
    ids = {s["session_id"] for s in result["sessions"]}
    assert ids == {"1", "4"}  # "Query", "ACTIVE"


def test_get_sessions_status_idle_returns_only_idle_variants():
    adapter = _FakeAdapter(sessions=_sample_sessions())
    result = get_sessions(adapter, status="idle")
    ids = {s["session_id"] for s in result["sessions"]}
    assert ids == {"2", "3", "5"}  # "Sleep", "idle", "INACTIVE"
