-- get_sessions Tool 용 (MSSQL).
SELECT
    s.session_id                 AS session_id,
    s.login_name                 AS user,
    s.host_name                  AS host,
    r.command                    AS current_operation,
    s.status                     AS state,
    r.total_elapsed_time         AS duration_ms
FROM sys.dm_exec_sessions s
LEFT JOIN sys.dm_exec_requests r ON r.session_id = s.session_id
WHERE s.is_user_process = 1;
