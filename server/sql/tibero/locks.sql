-- get_locks Tool 용 (Tibero). TODO: 버전별 V$LOCK 컬럼 구성 검증 필요.
SELECT
    l.sid          AS holding_session_id,
    l.type         AS lock_type,
    o.object_name  AS resource
FROM v$lock l
JOIN dba_objects o ON o.object_id = l.id1
WHERE l.block = 1;
