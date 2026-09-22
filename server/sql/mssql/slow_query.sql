-- slow_query_list Tool 용 (MSSQL). Doc/01_개발정의서.md §2.3 참고.
SELECT
    CONVERT(varchar(64), qs.query_hash, 1)        AS operation_id,
    st.text                                        AS text,
    qs.execution_count                             AS execution_count,
    qs.total_elapsed_time / qs.execution_count / 1000.0 AS avg_duration_ms,
    qs.max_elapsed_time / 1000.0                   AS max_duration_ms,
    qs.total_rows / NULLIF(qs.execution_count, 0)  AS rows_returned,
    qs.last_execution_time                         AS last_seen
FROM sys.dm_exec_query_stats qs
CROSS APPLY sys.dm_exec_sql_text(qs.sql_handle) st
WHERE qs.total_elapsed_time / qs.execution_count / 1000.0 >= :min_duration_sec * 1000
ORDER BY avg_duration_ms DESC
OFFSET 0 ROWS FETCH NEXT :limit ROWS ONLY;
