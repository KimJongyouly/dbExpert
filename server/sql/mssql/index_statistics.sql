-- get_index_statistics Tool 용 (MSSQL). :schema, :table 은 Parameter Binding.
SELECT
    i.name                    AS index_name,
    c.name                    AS column_name,
    i.is_unique                AS is_unique,
    s.user_seeks + s.user_scans AS usage_count
FROM sys.indexes i
JOIN sys.tables t ON t.object_id = i.object_id
JOIN sys.schemas sc ON sc.schema_id = t.schema_id
JOIN sys.index_columns ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id
JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
LEFT JOIN sys.dm_db_index_usage_stats s
       ON s.object_id = i.object_id AND s.index_id = i.index_id
WHERE sc.name = :schema AND t.name = :table
ORDER BY i.name, ic.key_ordinal;
