-- get_table_statistics Tool 용. :schema, :table 은 Parameter Binding.
SELECT
    TABLE_ROWS       AS row_count_estimate,
    DATA_LENGTH + INDEX_LENGTH AS size_bytes,
    UPDATE_TIME      AS last_analyzed_at
FROM information_schema.TABLES
WHERE TABLE_SCHEMA = :schema AND TABLE_NAME = :table;
