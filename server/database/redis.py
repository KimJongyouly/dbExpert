"""Redis / ElastiCache(Redis) Adapter (Engine Family: keyvalue).

redis-py의 Redis 클라이언트는 자체 커넥션 풀을 가지므로 스레드-로컬 관리가
필요 없다. Schema/Comment 개념이 없으므로 SchemaCapable은 구현하지 않는다
— server/catalog/ontology_builder.py 가 SCAN 기반 Key 샘플링으로 Key 패턴을
정규화(`user:*:session` 등)하여 Ontology Container에 준하는 단위로 취급한다
(Doc/00_개발요건사항.md §10.1 참고).

이 Adapter가 redis-py에 호출하는 명령은 PING/INFO/SLOWLOG/CLIENT LIST/
DBSIZE/SCAN/TYPE/MEMORY USAGE/GET/HGETALL 뿐이며, 쓰기 명령(SET/DEL/EXPIRE
등)은 절대 호출하지 않는다. 가능하면 ACL로 이 명령들만 허용된 계정을
사용한다.

Cluster 모드(ElastiCache Cluster Mode Enabled 등)는 Connection Profile에서
`cluster_mode: true`로 켠다(Doc/01_개발정의서.md §11 — 결정: 지원). 이 경우
`redis.cluster.RedisCluster`를 사용하며, 단일 노드의 SCAN cursor를 다른
샤드에 그대로 넘길 수 없기 때문에(각 노드가 독립된 cursor 공간을 가짐)
`_scan_pattern()`은 수동 cursor 루프 대신 redis-py가 Cluster 전체 노드를
알아서 순회해주는 `scan_iter()`를 쓴다 — 이 방식은 단일 인스턴스에서도
그대로 동작하므로 분기가 필요 없다. CLIENT LIST/SLOWLOG GET처럼 "리스트를
반환하는 명령"은 Cluster에서 기본적으로 노드 하나(default_node)만 조회되므로
`_all_nodes_list()`로 전체 노드 결과를 모아 합친다.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from server.database.base import (
    ActivityCapable,
    BaseAdapter,
    Capability,
    ContainerStatistics,
    DBError,
    ErrorLogCapable,
    IndexStatistics,
    Lock,
    SampleCapable,
    Session,
    SlowOperation,
    SlowOperationCapable,
    StatisticsCapable,
)
from server.database.registry import AdapterRegistry

_SCAN_COUNT_PER_ITERATION = 200
_MAX_SCAN_ITERATIONS = 20  # 전체 Key 공간을 무한 SCAN하지 않도록 상한을 둔다


class RedisAdapter(
    BaseAdapter,
    SlowOperationCapable,
    ActivityCapable,
    ErrorLogCapable,
    StatisticsCapable,
    SampleCapable,
):
    capabilities = {
        Capability.SLOW_OP,
        Capability.ACTIVITY,
        Capability.ERROR_LOG,
        Capability.STATS,
        Capability.SAMPLE,
    }

    def __init__(self, connection_profile: dict[str, Any]):
        super().__init__(connection_profile)
        self._client: Optional[Any] = None

    def _ensure_client(self) -> Any:
        if self._client is None:
            conn = self.connection_profile["connection"]
            if conn.get("cluster_mode"):
                from redis.cluster import ClusterNode, RedisCluster  # 지연 import

                nodes = conn.get("nodes") or [{"host": conn["host"], "port": conn["port"]}]
                startup_nodes = [ClusterNode(n["host"], n["port"]) for n in nodes]
                self._client = RedisCluster(
                    startup_nodes=startup_nodes,
                    password=self.connection_profile.get("password"),
                    socket_connect_timeout=self.connect_timeout_sec,
                    socket_timeout=self.query_timeout_sec,
                    decode_responses=True,
                )
            else:
                import redis  # 지연 import — optional dependency

                self._client = redis.Redis(
                    host=conn["host"],
                    port=conn["port"],
                    db=conn.get("db_number", 0),
                    password=self.connection_profile.get("password"),
                    socket_connect_timeout=self.connect_timeout_sec,
                    # 명령 하나가 이 시간을 넘겨 응답을 기다리면 소켓을 끊는다.
                    socket_timeout=self.query_timeout_sec,
                    decode_responses=True,
                )
        return self._client

    @property
    def _is_cluster(self) -> bool:
        return bool(self.connection_profile["connection"].get("cluster_mode"))

    def _all_nodes_list(self, client: Any, method_name: str, *args: Any, **kwargs: Any) -> list:
        """CLIENT LIST/SLOWLOG GET처럼 리스트를 반환하는 명령을 Cluster
        전체 노드에서 모아 하나의 리스트로 합친다. 단일 인스턴스에서는
        해당 명령을 그대로 호출한 결과를 반환한다."""
        method = getattr(client, method_name)
        if not self._is_cluster:
            return method(*args, **kwargs)

        from redis.cluster import RedisCluster

        per_node = method(*args, target_nodes=RedisCluster.ALL_NODES, **kwargs)
        merged: list = []
        for node_result in per_node.values():
            merged.extend(node_result or [])
        return merged

    def _all_nodes_dict_sum(self, client: Any, section: str) -> dict[str, Any]:
        """INFO <section>처럼 dict를 반환하는 명령을 Cluster 전체 노드에서
        모아, 숫자 값은 합산하고 그 외 값은 마지막 노드 값으로 덮어써 병합한다."""
        if not self._is_cluster:
            return client.info(section)

        from redis.cluster import RedisCluster

        per_node = client.info(section, target_nodes=RedisCluster.ALL_NODES)
        merged: dict[str, Any] = {}
        for node_info in per_node.values():
            for key, value in (node_info or {}).items():
                existing = merged.get(key)
                if isinstance(value, (int, float)) and isinstance(existing, (int, float)):
                    merged[key] = existing + value
                else:
                    merged[key] = value
        return merged

    def ping(self) -> dict[str, Any]:
        try:
            client = self._ensure_client()
            ok = client.ping()
            info = client.info("server")
            return {"connected": ok, "version": info.get("redis_version")}
        except Exception as exc:  # noqa: BLE001
            return {"connected": False, "error": str(exc)}

    # ------------------------------------------------------------------
    # SlowOperationCapable
    # ------------------------------------------------------------------

    def get_slow_operations(
        self, min_duration_sec: float, limit: int
    ) -> list[SlowOperation]:
        client = self._ensure_client()
        entries = self._all_nodes_list(client, "slowlog_get", limit)
        min_duration_us = min_duration_sec * 1_000_000
        results = []
        for entry in entries:
            duration_us = entry.get("duration", 0)
            if duration_us < min_duration_us:
                continue
            results.append(
                SlowOperation(
                    operation_id=str(entry.get("id")),
                    text=" ".join(str(part) for part in entry.get("command", [])),
                    execution_count=1,
                    avg_duration_ms=duration_us / 1000,
                    max_duration_ms=duration_us / 1000,
                    last_seen=datetime.fromtimestamp(entry["start_time"])
                    if entry.get("start_time")
                    else None,
                )
            )
        return results

    # ------------------------------------------------------------------
    # ActivityCapable — Redis는 전통적 Lock이 없어 Blocked Client로 대체.
    # ------------------------------------------------------------------

    def get_locks(self) -> list[Lock]:
        client = self._ensure_client()
        locks = []
        for entry in self._all_nodes_list(client, "client_list"):
            # redis-py client_list()는 raw flags 문자열을 그대로 넘겨준다.
            # 'b' 플래그가 있으면 BLPOP/BRPOP 등으로 blocked 상태임을 의미한다.
            if "b" in (entry.get("flags") or ""):
                locks.append(
                    Lock(
                        holding_session_id=str(entry.get("id")),
                        resource=entry.get("cmd") or "",
                        lock_type="blocked_client",
                    )
                )
        return locks

    def get_sessions(self) -> list[Session]:
        client = self._ensure_client()
        return [
            Session(
                session_id=str(entry.get("id")),
                user=entry.get("user"),
                host=entry.get("addr"),
                current_operation=entry.get("cmd"),
                state="connected",
                duration_ms=(float(entry.get("age", 0)) * 1000) if entry.get("age") else None,
            )
            for entry in self._all_nodes_list(client, "client_list")
        ]

    # ------------------------------------------------------------------
    # ErrorLogCapable
    # ------------------------------------------------------------------

    def get_errors(self, since: Optional[datetime] = None) -> list[DBError]:
        try:
            stats = self._all_nodes_dict_sum(self._ensure_client(), "errorstats")
        except Exception:
            return []
        return [
            DBError(error_code=key, message=f"{key}: {value}", occurred_at=None)
            for key, value in stats.items()
        ]

    # ------------------------------------------------------------------
    # StatisticsCapable — Key 패턴(container) 단위로 대체.
    # ------------------------------------------------------------------

    def get_table_statistics(
        self, namespace: Optional[str], container: str
    ) -> ContainerStatistics:
        client = self._ensure_client()
        keys = self._scan_pattern(client, container)
        total_bytes = 0
        for key in keys:
            try:
                total_bytes += client.memory_usage(key) or 0
            except Exception:  # noqa: BLE001
                continue
        return ContainerStatistics(row_count_estimate=len(keys), size_bytes=total_bytes)

    def get_index_statistics(
        self, namespace: Optional[str], container: str
    ) -> list[IndexStatistics]:
        # Redis에는 Index 개념이 없다 (Doc/00_개발요건사항.md §7.2 Capability 매트릭스).
        return []

    # ------------------------------------------------------------------
    # SampleCapable — SCAN(MATCH=container) 후 대표 값 Sample.
    # ------------------------------------------------------------------

    def sample_rows(
        self,
        namespace: Optional[str],
        container: str,
        limit: int,
        mask: Optional[list[str]] = None,
    ) -> list[dict[str, Any]]:
        limit = min(limit, 1000)
        client = self._ensure_client()
        keys = self._scan_pattern(client, container, max_keys=limit)

        results = []
        for key in keys:
            key_type = client.type(key)
            if key_type == "string":
                value: Any = client.get(key)
            elif key_type == "hash":
                value = client.hgetall(key)
            elif key_type == "list":
                value = client.lrange(key, 0, 10)
            elif key_type == "set":
                value = list(client.smembers(key))[:10]
            elif key_type == "zset":
                value = client.zrange(key, 0, 10, withscores=True)
            else:
                value = None

            if mask and key in mask:
                value = "***"
            results.append({"key": key, "type": key_type, "value": value})
        return results

    def _scan_pattern(
        self, client: Any, pattern: str, max_keys: Optional[int] = None
    ) -> list[str]:
        # scan_iter()는 redis-py가 내부적으로 cursor 루프를 돌려주고,
        # RedisCluster에서는 노드별 독립된 cursor 공간까지 알아서 순회해
        # 합쳐준다 — 수동 SCAN cursor 루프는 단일 노드 가정이라 Cluster에서
        # 틀린 결과를 내므로 쓰지 않는다.
        limit = max_keys or (_MAX_SCAN_ITERATIONS * _SCAN_COUNT_PER_ITERATION)
        keys: list[str] = []
        for key in client.scan_iter(match=pattern, count=_SCAN_COUNT_PER_ITERATION):
            keys.append(key)
            if len(keys) >= limit:
                break
        return keys

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:  # noqa: BLE001 — RedisCluster의 close() 지원 여부는 버전에 따라 다르다
                pass
            self._client = None


AdapterRegistry.register("redis", RedisAdapter, engine_family="keyvalue")
