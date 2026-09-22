-- get_locks Tool 용 (MSSQL).
SELECT
    tl.request_session_id  AS waiting_session_id,
    wt.blocking_session_id AS holding_session_id,
    tl.resource_type       AS resource,
    tl.request_mode        AS lock_type,
    wt.wait_duration_ms    AS wait_time_ms
FROM sys.dm_tran_locks tl
JOIN sys.dm_os_waiting_tasks wt ON wt.session_id = tl.request_session_id
WHERE tl.request_status = 'WAIT';
