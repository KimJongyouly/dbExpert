-- get_locks Tool 용.
SELECT
    blocking_locks.pid  AS holding_session_id,
    blocked_locks.pid   AS waiting_session_id,
    blocked_locks.relation::regclass::text AS resource,
    blocked_locks.mode  AS lock_type
FROM pg_catalog.pg_locks blocked_locks
JOIN pg_catalog.pg_locks blocking_locks
    ON blocking_locks.locktype = blocked_locks.locktype
    AND blocking_locks.database IS NOT DISTINCT FROM blocked_locks.database
    AND blocking_locks.relation IS NOT DISTINCT FROM blocked_locks.relation
    AND blocking_locks.pid != blocked_locks.pid
WHERE NOT blocked_locks.granted;
