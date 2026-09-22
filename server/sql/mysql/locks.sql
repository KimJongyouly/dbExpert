-- get_locks Tool 용 (MySQL 8.0+, performance_schema.data_locks 기반).
SELECT
    r.trx_mysql_thread_id  AS waiting_session_id,
    b.trx_mysql_thread_id  AS holding_session_id,
    dl.OBJECT_NAME         AS resource,
    dl.LOCK_TYPE           AS lock_type,
    TIMESTAMPDIFF(MICROSECOND, r.trx_wait_started, NOW()) / 1000 AS wait_time_ms
FROM performance_schema.data_lock_waits w
JOIN information_schema.INNODB_TRX r ON r.trx_id = w.REQUESTING_ENGINE_TRANSACTION_ID
JOIN information_schema.INNODB_TRX b ON b.trx_id = w.BLOCKING_ENGINE_TRANSACTION_ID
JOIN performance_schema.data_locks dl ON dl.ENGINE_LOCK_ID = w.REQUESTING_ENGINE_LOCK_ID;
