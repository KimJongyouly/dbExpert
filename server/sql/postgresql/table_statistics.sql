-- get_table_statistics Tool 용. :schema, :table 은 Parameter Binding.
SELECT
    c.reltuples::bigint                      AS row_count_estimate,
    pg_total_relation_size(c.oid)            AS size_bytes,
    s.last_analyze                           AS last_analyzed_at
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
LEFT JOIN pg_stat_user_tables s ON s.relid = c.oid
WHERE n.nspname = :schema AND c.relname = :table;
