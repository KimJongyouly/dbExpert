-- get_index_statistics Tool 용. :schema, :table 은 Parameter Binding.
SELECT
    i.relname          AS index_name,
    ix.indisunique     AS is_unique,
    s.idx_scan         AS usage_count
FROM pg_index ix
JOIN pg_class t ON t.oid = ix.indrelid
JOIN pg_class i ON i.oid = ix.indexrelid
JOIN pg_namespace n ON n.oid = t.relnamespace
LEFT JOIN pg_stat_user_indexes s ON s.indexrelid = i.oid
WHERE n.nspname = :schema AND t.relname = :table;
