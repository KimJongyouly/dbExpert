-- get_sessions Tool 용. idle 세션도 포함해서 전부 반환한다 —
-- running/idle 구분은 server/analyzer/session_monitor.py가 status 파라미터로
-- Python 쪽에서 한다(mysql/sessions.sql과 동일한 이유).
SELECT
    pid                                            AS session_id,
    usename                                        AS user,
    client_addr::text                              AS host,
    query                                          AS current_operation,
    state                                          AS state,
    EXTRACT(EPOCH FROM (now() - query_start)) * 1000 AS duration_ms
FROM pg_stat_activity;
