"""TiberoAdapter.get_sessions()가 V$SESSION 조회 결과를 Session으로 올바르게
매핑하는지에 대한 단위 테스트. JDBC 실행 경로는 타지 않고 _run_template만
가로챈다.
"""
from __future__ import annotations

import server.database.tibero as tibero_module


def test_get_sessions_maps_vsession_columns(monkeypatch):
    adapter = tibero_module.TiberoAdapter(
        {"connection": {"host": "h", "port": 8629}, "user": "appuser", "password": "p"}
    )
    monkeypatch.setattr(
        adapter,
        "_run_template",
        lambda name, params=None: [
            {
                "SID": "101",
                "SERIAL#": "5001",
                "USERNAME": "APPUSER",
                "STATUS": "ACTIVE",
                "TYPE": "USER",
                "MACHINE": "app-server-01",
                "LOGON_TIME": "2026-09-10 09:00:00",
            }
        ],
    )

    sessions = adapter.get_sessions()

    assert len(sessions) == 1
    session = sessions[0]
    assert session.session_id == "101/5001"
    assert session.user == "APPUSER"
    assert session.host == "app-server-01"
    assert session.state == "ACTIVE"
    assert session.current_operation == "USER"


def test_sessions_sql_template_matches_requested_query():
    sql = tibero_module.load_sql_template("tibero", "sessions")
    for expected_fragment in ("SID", "SERIAL#", "USERNAME", "STATUS", "TYPE", "MACHINE", "LOGON_TIME", "V$SESSION"):
        assert expected_fragment in sql
