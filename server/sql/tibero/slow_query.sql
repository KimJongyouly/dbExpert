-- slow_query_list Tool 용 (Tibero).
-- TODO: 확정 필요 — Doc/01_개발정의서.md §11 미결정 사항.
-- Tibero의 Performance View(예: V$SQL 계열) 중 정확한 대상을 검증 후 채운다.
-- 아래는 Oracle 호환 V$SQL 기준의 초안이며 실제 버전에서 반드시 검증할 것.
SELECT
    sql_id                    AS operation_id,
    sql_text                  AS text,
    executions                AS execution_count,
    elapsed_time / NULLIF(executions, 0) / 1000 AS avg_duration_ms,
    elapsed_time / 1000        AS max_duration_ms
FROM v$sql
WHERE elapsed_time / NULLIF(executions, 0) / 1000 >= :min_duration_sec * 1000
ORDER BY avg_duration_ms DESC
FETCH FIRST :limit ROWS ONLY;
