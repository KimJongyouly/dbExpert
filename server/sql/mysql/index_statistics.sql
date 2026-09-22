-- get_index_statistics Tool 용. :schema, :table 은 Parameter Binding.
SELECT
    INDEX_NAME  AS index_name,
    COLUMN_NAME AS column_name,
    SEQ_IN_INDEX,
    CARDINALITY AS cardinality,
    NON_UNIQUE  AS non_unique
FROM information_schema.STATISTICS
WHERE TABLE_SCHEMA = :schema AND TABLE_NAME = :table
ORDER BY INDEX_NAME, SEQ_IN_INDEX;
