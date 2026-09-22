"""Elasticsearch/OpenSearch 공통 로직 (Engine Family: search).

elasticsearch-py 8.x는 클러스터가 진짜 Elastic인지 확인하는 product-check를
수행해 OpenSearch에 붙이면 거부/경고가 발생할 수 있다. 반대로 opensearch-py는
Elasticsearch에 안전하게 붙는다는 보장이 없다 — 즉 "완전 호환"이 아니라
"클라이언트 라이브러리를 서버 종류에 맞게 고정해야 안전한" 비대칭 관계다.
그래서 겸용 단일 클래스 대신, REST API 호출부(이 파일)는 공유하고 Client
생성부만 `elasticsearch.py`/`opensearch.py`로 분리한다
(Doc/01_개발정의서.md §11 — 결정: 분리).

읽기 전용 API(_search, _mapping, _stats, _cat, _tasks)만 사용하며,
색인/삭제(_doc, _bulk, _delete) API는 절대 호출하지 않는다. FK/관계
개념이 없으므로 RelationshipCapable도 구현하지 않는다.
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
    SchemaCapable,
    SchemaField,
    SchemaObject,
    Session,
    SlowOperation,
    SlowOperationCapable,
    StatisticsCapable,
)


class SearchAdapterBase(
    BaseAdapter,
    SlowOperationCapable,
    ActivityCapable,
    ErrorLogCapable,
    SchemaCapable,
    StatisticsCapable,
    SampleCapable,
):
    """Elasticsearch/OpenSearch 공통 REST 호출 로직.

    두 client 라이브러리(elasticsearch-py, opensearch-py)는 이 파일이 쓰는
    범위(indices/cluster/tasks 네임스페이스, search())의 메서드 시그니처가
    동일하므로 `_ensure_client()`만 하위 클래스가 채우면 나머지는 그대로 쓴다.
    """

    capabilities = {
        Capability.SLOW_OP,
        Capability.ACTIVITY,
        Capability.ERROR_LOG,
        Capability.SCHEMA,
        Capability.STATS,
        Capability.SAMPLE,
    }

    def __init__(self, connection_profile: dict[str, Any]):
        super().__init__(connection_profile)
        self._client: Any = None

    def _ensure_client(self) -> Any:  # pragma: no cover - abstract
        raise NotImplementedError

    def ping(self) -> dict[str, Any]:
        try:
            client = self._ensure_client()
            info = client.info()
            return {
                "connected": True,
                "version": info.get("version", {}).get("number"),
                "cluster_name": info.get("cluster_name"),
            }
        except Exception as exc:  # noqa: BLE001
            return {"connected": False, "error": str(exc)}

    # ------------------------------------------------------------------
    # SlowOperationCapable — Slow Log는 파일로만 남고 조회 API가 없어,
    # 현재 설정된 임계값만 "느린 요청 감지 기준"으로 반환한다. 실제 발생
    # 이력이 필요하면 별도로 색인된 Slow Log를 조회해야 한다.
    # ------------------------------------------------------------------

    def get_slow_operations(
        self, min_duration_sec: float, limit: int
    ) -> list[SlowOperation]:
        client = self._ensure_client()
        settings = client.indices.get_settings(index="*")
        results = []
        for index_name, cfg in list(settings.items())[:limit]:
            slowlog_cfg = (
                cfg.get("settings", {}).get("index", {}).get("search", {}).get("slowlog", {})
            )
            if not slowlog_cfg:
                continue
            results.append(
                SlowOperation(
                    operation_id=index_name,
                    text=f"slowlog threshold config: {slowlog_cfg}",
                    execution_count=0,
                    avg_duration_ms=0.0,
                    max_duration_ms=0.0,
                )
            )
        return results

    # ------------------------------------------------------------------
    # ActivityCapable
    # ------------------------------------------------------------------

    def get_locks(self) -> list[Lock]:
        # 전통적인 Row/Table Lock 개념이 없다.
        return []

    def get_sessions(self) -> list[Session]:
        client = self._ensure_client()
        tasks = client.tasks.list(detailed=True).get("nodes", {})
        results = []
        for node in tasks.values():
            for task_id, task in node.get("tasks", {}).items():
                results.append(
                    Session(
                        session_id=str(task_id),
                        user=None,
                        host=node.get("host"),
                        current_operation=task.get("action"),
                        state="running",
                        duration_ms=(task.get("running_time_in_nanos") or 0) / 1e6,
                    )
                )
        return results

    # ------------------------------------------------------------------
    # ErrorLogCapable
    # ------------------------------------------------------------------

    def get_errors(self, since: Optional[datetime] = None) -> list[DBError]:
        try:
            health = self._ensure_client().cluster.health()
        except Exception:
            return []
        if health.get("status") == "green":
            return []
        return [
            DBError(
                error_code=health.get("status"),
                message=(
                    f"Cluster status={health.get('status')}, "
                    f"unassigned_shards={health.get('unassigned_shards')}"
                ),
                occurred_at=None,
            )
        ]

    # ------------------------------------------------------------------
    # SchemaCapable — Mapping 기반. comment는 `_meta`에 있으면 사용.
    # ------------------------------------------------------------------

    def get_schema(self, scope: dict[str, Any]) -> list[SchemaObject]:
        client = self._ensure_client()
        container = scope.get("container")
        index_pattern = container or "*"
        mappings = client.indices.get_mapping(index=index_pattern)

        results = []
        for index_name, body in mappings.items():
            mapping = body.get("mappings", {})
            meta = mapping.get("_meta", {}) or {}
            properties = mapping.get("properties", {}) or {}
            fields = [
                SchemaField(
                    name=field_name,
                    type=field_def.get("type", "object"),
                    comment=meta.get(field_name),
                    is_pk=False,
                )
                for field_name, field_def in properties.items()
            ]
            results.append(
                SchemaObject(
                    namespace=None,
                    container=index_name,
                    container_comment=meta.get("description"),
                    fields=fields,
                )
            )
        return results

    # ------------------------------------------------------------------
    # StatisticsCapable
    # ------------------------------------------------------------------

    def get_table_statistics(
        self, namespace: Optional[str], container: str
    ) -> ContainerStatistics:
        client = self._ensure_client()
        stats = client.indices.stats(index=container)
        total = stats.get("_all", {}).get("primaries", {})
        return ContainerStatistics(
            row_count_estimate=total.get("docs", {}).get("count"),
            size_bytes=total.get("store", {}).get("size_in_bytes"),
        )

    def get_index_statistics(
        self, namespace: Optional[str], container: str
    ) -> list[IndexStatistics]:
        # 명시적 Index 개념이 없다 — Mapping/Shard 정보는 get_schema/get_table_statistics로 대체.
        return []

    # ------------------------------------------------------------------
    # SampleCapable
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
        response = client.search(index=container, query={"match_all": {}}, size=limit)
        docs = [hit["_source"] for hit in response["hits"]["hits"]]
        if mask:
            for doc in docs:
                for col in mask:
                    if col in doc:
                        doc[col] = "***"
        return docs

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:  # noqa: BLE001
                pass
            self._client = None
