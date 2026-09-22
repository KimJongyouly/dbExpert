# Redis 명령 템플릿

`server/database/redis.py` 가 사용하는 읽기 전용 명령 목록. 쓰기 명령은
이 Plugin에서 절대 호출하지 않는다 (Doc/00_개발요건사항.md §29).

| 용도 | 명령 |
|---|---|
| ping | `PING` |
| get_slow_operations | `SLOWLOG GET {{limit}}` |
| get_sessions | `CLIENT LIST` |
| get_locks (Blocked Client 대체) | `CLIENT LIST` 결과 중 `flags` 필드 파싱 |
| get_errors | `INFO errorstats` |
| get_table_statistics (Key 패턴 단위) | `DBSIZE`, 패턴 매칭 후 `MEMORY USAGE {{key}}` 샘플링 |
| sample_rows / Ontology 수집 | `SCAN {{cursor}} MATCH {{pattern}} COUNT {{count}}` |
