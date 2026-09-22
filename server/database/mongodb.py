"""MongoDB Adapter (Engine Family: document).

pymongo의 MongoClient는 자체적으로 스레드-세이프 커넥션 풀을 관리하므로,
mysql.py/postgresql.py처럼 스레드-로컬 연결을 직접 구현할 필요가 없다
(dao.py 구조 중 "지연 연결 + 재사용" 개념만 가져오고, 스레드별 분리는
Driver에 위임한다).

Schema-less Document Store이므로 SqlCapable은 구현하지 않는다. get_schema는
공식 comment가 없어 `$jsonSchema` validator의 description(있으면)을 쓰고,
없으면 `$sample` 기반 Sampling으로 Field를 추론한다(SchemaField.inferred=True).

관계(FK)는 선언 메커니즘이 없으므로 RelationshipCapable을 구현하지 않으며,
필요 시 server/catalog/erd_builder.py 가 `_id` 참조 패턴으로 "inferred" 관계를
추정한다.

Driver: pymongo. 쓰기 계열 명령(insert/update/delete/drop 등)은 호출하지
않는다 — 이 Adapter가 pymongo에 요청하는 명령은 find/aggregate/list*/command
(ping, buildInfo, collStats, currentOp, serverStatus) 뿐이다.

Replica Set/`mongodb+srv://` 지원(Doc/01_개발정의서.md §11 — 결정: 지원):
Connection Profile에 `connection.uri`(완전한 연결 문자열)를 주면 그대로
MongoClient에 전달한다. pymongo의 MongoClient는 콤마로 구분된 seed list나
`mongodb+srv://` DNS SRV 방식을 `host` 인자(또는 URI)로 그대로 받으므로
Adapter 코드에서 별도로 파싱하지 않는다. `uri`가 없으면 기존처럼
`connection.uri_host`/`port` 단일 호스트 접속으로 동작한다(하위 호환).
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
from server.database.registry import AdapterRegistry

_SAMPLE_SIZE_FOR_SCHEMA_INFERENCE = 200


class MongoDBAdapter(
    BaseAdapter,
    SlowOperationCapable,
    ActivityCapable,
    ErrorLogCapable,
    SchemaCapable,
    StatisticsCapable,
    SampleCapable,
):
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
        self._client: Optional[Any] = None

    def _ensure_client(self) -> Any:
        if self._client is None:
            from pymongo import MongoClient  # 지연 import — optional dependency

            conn = self.connection_profile["connection"]
            timeouts = dict(
                connectTimeoutMS=self.connect_timeout_sec * 1000,
                serverSelectionTimeoutMS=self.connect_timeout_sec * 1000,
                # 쿼리(find/aggregate 등) 하나가 이 시간을 넘기면 소켓을 끊는다.
                socketTimeoutMS=self.query_timeout_sec * 1000,
            )
            if conn.get("uri"):
                # Replica Set(mongodb://host1,host2,host3/?replicaSet=rs0)
                # 또는 mongodb+srv://... 완전한 연결 문자열. 인증정보가 URI에
                # 이미 포함돼 있으면 아래 username/password는 무시된다.
                self._client = MongoClient(
                    conn["uri"],
                    username=self.connection_profile.get("user"),
                    password=self.connection_profile.get("password"),
                    **timeouts,
                )
            else:
                self._client = MongoClient(
                    host=conn["uri_host"],
                    port=conn["port"],
                    username=self.connection_profile.get("user"),
                    password=self.connection_profile.get("password"),
                    **timeouts,
                )
        return self._client

    def _db(self):
        client = self._ensure_client()
        conn = self.connection_profile["connection"]
        if conn.get("database"):
            return client[conn["database"]]
        # uri에 기본 DB가 포함된 경우(mongodb+srv://.../mydb 등)를 위한 fallback.
        return client.get_default_database()

    def ping(self) -> dict[str, Any]:
        try:
            db = self._db()
            db.command("ping")
            build_info = db.command("buildInfo")
            return {"connected": True, "version": build_info.get("version")}
        except Exception as exc:  # noqa: BLE001
            return {"connected": False, "error": str(exc)}

    # ------------------------------------------------------------------
    # SlowOperationCapable — Database Profiler(system.profile) 기반.
    # 프로파일링이 꺼져 있으면(level=0) 빈 결과가 정상이다.
    # ------------------------------------------------------------------

    def get_slow_operations(
        self, min_duration_sec: float, limit: int
    ) -> list[SlowOperation]:
        db = self._db()
        cursor = (
            db["system.profile"]
            .find({"millis": {"$gte": min_duration_sec * 1000}})
            .sort("millis", -1)
            .limit(limit)
        )
        results = []
        for doc in cursor:
            results.append(
                SlowOperation(
                    operation_id=str(doc.get("_id")),
                    text=str(doc.get("command") or doc.get("query") or ""),
                    execution_count=1,  # profiler는 호출 1건당 1 row이며 집계는 하지 않음
                    avg_duration_ms=float(doc.get("millis", 0)),
                    max_duration_ms=float(doc.get("millis", 0)),
                    rows_examined=doc.get("docsExamined"),
                    rows_returned=doc.get("nreturned"),
                    last_seen=doc.get("ts"),
                )
            )
        return results

    # ------------------------------------------------------------------
    # ActivityCapable — currentOp() 기반 (버전에 따라 필드 구성이 다를 수 있음).
    # ------------------------------------------------------------------

    def get_locks(self) -> list[Lock]:
        client = self._ensure_client()
        result = client.admin.command("currentOp", True)
        locks = []
        for op in result.get("inprog", []):
            if op.get("waitingForLock"):
                locks.append(
                    Lock(
                        holding_session_id=str(op.get("opid")),
                        resource=str(op.get("ns") or ""),
                        lock_type=str(op.get("lockStats") or "unknown"),
                        wait_time_ms=None,
                    )
                )
        return locks

    def get_sessions(self) -> list[Session]:
        client = self._ensure_client()
        result = client.admin.command("currentOp", True)
        sessions = []
        for op in result.get("inprog", []):
            sessions.append(
                Session(
                    session_id=str(op.get("opid")),
                    user=(op.get("effectiveUsers") or [{}])[0].get("user")
                    if op.get("effectiveUsers")
                    else None,
                    host=op.get("client"),
                    current_operation=str(op.get("ns") or op.get("op") or ""),
                    state=op.get("op", "unknown"),
                    duration_ms=(op.get("secs_running") or 0) * 1000,
                )
            )
        return sessions

    # ------------------------------------------------------------------
    # ErrorLogCapable — serverStatus의 실패 카운터 기반 근사치.
    # ------------------------------------------------------------------

    def get_errors(self, since: Optional[datetime] = None) -> list[DBError]:
        try:
            status = self._db().command("serverStatus")
            failed = status.get("metrics", {}).get("commands", {}).get("failed")
        except Exception:
            return []
        if not failed:
            return []
        return [
            DBError(
                error_code="command_failed",
                message=f"serverStatus 기준 누적 실패 명령 수 근사치: {failed}",
                occurred_at=None,
            )
        ]

    # ------------------------------------------------------------------
    # SchemaCapable — $jsonSchema description 우선, 없으면 Sampling 추론.
    # ------------------------------------------------------------------

    def get_schema(self, scope: dict[str, Any]) -> list[SchemaObject]:
        db = self._db()
        container = scope.get("container")
        collections = [container] if container else db.list_collection_names()

        results = []
        for name in collections:
            validator_props = self._jsonschema_properties(db, name)
            fields = self._infer_fields(db, name, validator_props)
            stats = self._safe_coll_stats(db, name)
            results.append(
                SchemaObject(
                    namespace=db.name,
                    container=name,
                    container_comment=None,  # Collection 자체에 대한 comment 메커니즘 없음
                    row_count_estimate=stats.get("count") if stats else None,
                    fields=fields,
                )
            )
        return results

    def _jsonschema_properties(self, db, collection_name: str) -> dict[str, Any]:
        try:
            info = list(db.list_collections(filter={"name": collection_name}))
        except Exception:
            return {}
        if not info:
            return {}
        validator = info[0].get("options", {}).get("validator", {})
        return validator.get("$jsonSchema", {}).get("properties", {})

    def _infer_fields(
        self, db, collection_name: str, validator_props: dict[str, Any]
    ) -> list[SchemaField]:
        try:
            sample_docs = list(
                db[collection_name].aggregate(
                    [{"$sample": {"size": _SAMPLE_SIZE_FOR_SCHEMA_INFERENCE}}]
                )
            )
        except Exception:
            return []

        field_types: dict[str, set[str]] = {}
        for doc in sample_docs:
            for key, value in doc.items():
                field_types.setdefault(key, set()).add(type(value).__name__)

        fields = []
        for name, types in field_types.items():
            prop = validator_props.get(name, {})
            fields.append(
                SchemaField(
                    name=name,
                    type="/".join(sorted(types)),
                    comment=prop.get("description"),
                    is_pk=(name == "_id"),
                    inferred=not bool(prop.get("description")),
                )
            )
        return fields

    def _safe_coll_stats(self, db, collection_name: str) -> Optional[dict[str, Any]]:
        try:
            return db.command("collStats", collection_name)
        except Exception:
            return None

    # ------------------------------------------------------------------
    # StatisticsCapable
    # ------------------------------------------------------------------

    def get_table_statistics(
        self, namespace: Optional[str], container: str
    ) -> ContainerStatistics:
        stats = self._safe_coll_stats(self._db(), container)
        if not stats:
            return ContainerStatistics()
        return ContainerStatistics(
            row_count_estimate=stats.get("count"),
            size_bytes=stats.get("size"),
        )

    def get_index_statistics(
        self, namespace: Optional[str], container: str
    ) -> list[IndexStatistics]:
        db = self._db()
        try:
            usage_by_name = {
                doc["name"]: doc.get("accesses", {}).get("ops")
                for doc in db[container].aggregate([{"$indexStats": {}}])
            }
        except Exception:
            usage_by_name = {}

        results = []
        for spec in db[container].list_indexes():
            results.append(
                IndexStatistics(
                    index_name=spec["name"],
                    columns=list(spec["key"].keys()),
                    usage_count=usage_by_name.get(spec["name"]),
                    is_unique=bool(spec.get("unique", False)),
                )
            )
        return results

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
        docs = list(self._db()[container].aggregate([{"$sample": {"size": limit}}]))
        for doc in docs:
            doc["_id"] = str(doc.get("_id"))
            if mask:
                for col in mask:
                    if col in doc:
                        doc[col] = "***"
        return docs

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None


AdapterRegistry.register("mongodb", MongoDBAdapter, engine_family="document")
