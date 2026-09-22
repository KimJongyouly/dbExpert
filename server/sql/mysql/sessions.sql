-- get_sessions Tool 용. idle(Sleep) 세션도 포함해서 전부 반환한다 —
-- running/idle 구분은 server/analyzer/session_monitor.py가 status 파라미터로
-- Python 쪽에서 한다(Engine마다 idle을 나타내는 문자열 어휘가 달라 SQL
-- 단계에서 미리 걸러내면 Tool의 status="idle"/"all" 요청에 응답할 수 없다).
SELECT
    ID          AS session_id,
    USER        AS user,
    HOST        AS host,
    INFO        AS current_operation,
    COMMAND     AS state,
    TIME * 1000 AS duration_ms
FROM information_schema.PROCESSLIST;
