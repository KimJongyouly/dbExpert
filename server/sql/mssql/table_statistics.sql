-- get_table_statistics Tool 용 (MSSQL). :schema, :table 은 Parameter Binding.
SELECT
    SUM(p.rows)                                   AS row_count_estimate,
    SUM(a.total_pages) * 8 * 1024                 AS size_bytes,
    STATS_DATE(t.object_id, i.index_id)           AS last_analyzed_at
FROM sys.tables t
JOIN sys.schemas sc ON sc.schema_id = t.schema_id
JOIN sys.indexes i ON i.object_id = t.object_id AND i.index_id IN (0, 1)
JOIN sys.partitions p ON p.object_id = t.object_id AND p.index_id = i.index_id
JOIN sys.allocation_units a ON a.container_id = p.partition_id
WHERE sc.name = :schema AND t.name = :table
GROUP BY t.object_id, i.index_id;
