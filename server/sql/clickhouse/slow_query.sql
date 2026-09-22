-- slow_query_list Tool 용 (ClickHouse). system.query_log 는 기본적으로
-- query_log 설정이 켜져 있어야 기록된다.
SELECT
    query_id                         AS operation_id,
    any(query)                       AS text,
    count()                          AS execution_count,
    avg(query_duration_ms)           AS avg_duration_ms,
    max(query_duration_ms)           AS max_duration_ms,
    max(event_time)                  AS last_seen
FROM system.query_log
WHERE type = 'QueryFinish'
GROUP BY query_id
HAVING avg(query_duration_ms) >= {min_duration_sec:Float64} * 1000
ORDER BY avg_duration_ms DESC
LIMIT {limit:UInt32};
