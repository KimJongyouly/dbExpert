-- slow_query_list Tool 용. pg_stat_statements extension 필요.
-- :min_duration_sec, :limit 은 Adapter에서 Parameter Binding.
SELECT
    queryid::text                    AS operation_id,
    query                             AS text,
    calls                             AS execution_count,
    mean_exec_time                    AS avg_duration_ms,
    max_exec_time                     AS max_duration_ms,
    rows                              AS rows_returned
FROM pg_stat_statements
WHERE mean_exec_time >= :min_duration_sec * 1000
ORDER BY mean_exec_time DESC
LIMIT :limit;
