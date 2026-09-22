-- slow_query_list Tool 용. Doc/01_개발정의서.md §2.3 참고.
-- :min_duration_sec, :limit 은 Adapter에서 Parameter Binding으로 채운다.
SELECT
    DIGEST                                  AS operation_id,
    DIGEST_TEXT                             AS text,
    COUNT_STAR                              AS execution_count,
    (SUM_TIMER_WAIT / COUNT_STAR) / 1e9      AS avg_duration_ms,
    MAX_TIMER_WAIT / 1e9                     AS max_duration_ms,
    SUM_ROWS_EXAMINED                        AS rows_examined,
    SUM_ROWS_SENT                            AS rows_returned,
    SCHEMA_NAME                              AS database_name,
    LAST_SEEN                                AS last_seen
FROM performance_schema.events_statements_summary_by_digest
WHERE (SUM_TIMER_WAIT / COUNT_STAR) / 1e9 >= :min_duration_sec
ORDER BY avg_duration_ms DESC
LIMIT :limit;
