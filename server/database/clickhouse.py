"""ClickHouse Adapter (Engine Family: dw).

clickhouse-connect의 Client는 DB-API 스타일 cursor가 아니라
`client.query(sql, parameters=...)` 형태이므로, sql_base.py의 공통
execute_readonly()를 그대로 쓰지 않고 이 파일에서 오버라이드한다. 그 외
연결 lifecycle(스레드별 클라이언트, 재연결, close)은 dao.py 참고 구조를
그대로 따른다.

Parameter binding은 `:name`이 아니라 ClickHouse 고유의 `{name:Type}` 문법을
쓰므로(server/sql/clickhouse/*.sql 참고) param_binding 변환기를 거치지
않는다.

FK를 강제하지 않는 Engine이라 RelationshipCapable은 구현하지 않는다 —
관계가 필요하면 server/catalog/erd_builder.py 가 SchemaCapable 결과로부터
"inferred" 관계를 별도로 추정한다.

Driver: clickhouse-connect.
"""
from __future__ import annotations

import threading
from datetime import datetime
from typing import Any, Optional

from server.database.base import (
    ActivityCapable,
    BaseAdapter,
    Capability,
    ContainerStatistics,
    DBError,
    ErrorLogCapable,
    ExplainPlan,
    IndexStatistics,
    Lock,
    SampleCapable,
    SchemaCapable,
    SchemaObject,
    Session,
    SlowOperation,
    SlowOperationCapable,
    SqlCapable,
    StatisticsCapable,
)
from server.database.registry import AdapterRegistry
from server.database.sql_base import load_sql_template, rows_to_schema_objects, validate_identifier
from server.database.sql_guard import assert_readonly_sql


class ClickHouseAdapter(
    BaseAdapter,
    SqlCapable,
    SlowOperationCapable,
    ActivityCapable,
    ErrorLogCapable,
    SchemaCapable,
    StatisticsCapable,
    SampleCapable,
):
    capabilities = {
        Capability.SQL,
        Capability.SLOW_OP,
        Capability.ACTIVITY,
        Capability.ERROR_LOG,
        Capability.SCHEMA,
        Capability.STATS,
        Capability.SAMPLE,
    }

    engine_name = "clickhouse"
    # connect_timeout_sec / query_timeout_sec는 BaseAdapter의 프로퍼티를 그대로 쓴다.

    def __init__(self, connection_profile: dict[str, Any]):
        super().__init__(connection_profile)
        self._local = threading.local()

    @property
    def _client(self) -> Any:
        return getattr(self._local, "client", None)

    @_client.setter
    def _client(self, value: Any) -> None:
        self._local.client = value

    def _ensure_client(self) -> Any:
        client = self._client
        if client is None:
            import clickhouse_connect  # 지연 import — Extras로만 설치되는 optional dependency

            conn = self.connection_profile["connection"]
            client = clickhouse_connect.get_client(
                host=conn["host"],
                port=conn["port"],
                database=conn.get("database"),
                username=self.connection_profile["user"],
                password=self.connection_profile["password"],
                connect_timeout=self.connect_timeout_sec,
                send_receive_timeout=self.query_timeout_sec,
                # ClickHouse Cloud/관리형 인스턴스는 HTTPS(주로 8443/443
                # 포트)로만 접속을 허용하는 경우가 많다 — connection.secure:
                # true로 켠다(생략하면 기존과 동일하게 평문 HTTP).
                secure=bool(conn.get("secure", False)),
                # readonly=1은 ClickHouse 서버가 이 Client의 모든 쿼리에
                # 강제하는 세션 설정이다 — 관리자 계정을 쓰더라도 서버가
                # INSERT/ALTER/DROP/SYSTEM 등 쓰기 계열 쿼리 자체를 거부하게
                # 만드는 세 번째 방어선(assert_readonly_sql의 텍스트 검사와
                # 독립적, Doc/00 §29 참고).
                settings={"readonly": 1},
            )
            self._client = client
        return client

    def ping(self) -> dict[str, Any]:
        try:
            client = self._ensure_client()
            return {"connected": client.ping(), "version": client.server_version}
        except Exception as exc:  # noqa: BLE001
            return {"connected": False, "error": str(exc)}

    def execute_readonly(
        self, sql: str, params: Optional[dict[str, Any]] = None
    ) -> list[dict[str, Any]]:
        assert_readonly_sql(sql)
        client = self._ensure_client()
        result = client.query(sql, parameters=params or {})
        return [dict(zip(result.column_names, row)) for row in result.result_rows]

    def explain(self, sql: str) -> ExplainPlan:
        rows = self.execute_readonly(f"EXPLAIN {sql}")
        return ExplainPlan(raw={"lines": [r for r in rows]})

    def _run_template(
        self, template_name: str, params: Optional[dict[str, Any]] = None
    ) -> list[dict[str, Any]]:
        sql = load_sql_template(self.engine_name, template_name)
        return self.execute_readonly(sql, params)

    # ------------------------------------------------------------------
    # SlowOperationCapable / ActivityCapable
    # ------------------------------------------------------------------

    def get_slow_operations(
        self, min_duration_sec: float, limit: int
    ) -> list[SlowOperation]:
        rows = self._run_template(
            "slow_query", {"min_duration_sec": min_duration_sec, "limit": limit}
        )
        return [
            SlowOperation(
                operation_id=str(r["operation_id"]),
                text=r["text"] or "",
                execution_count=int(r["execution_count"]),
                avg_duration_ms=float(r["avg_duration_ms"] or 0),
                max_duration_ms=float(r["max_duration_ms"] or 0),
                last_seen=r.get("last_seen"),
            )
            for r in rows
        ]

    def get_locks(self) -> list[Lock]:
        # ClickHouse는 전통적인 행/테이블 Lock이 없다 — 병합/뮤테이션 대기로 대체 검토.
        return []

    def get_sessions(self) -> list[Session]:
        rows = self.execute_readonly(
            "SELECT query_id, user, elapsed * 1000 AS duration_ms, query "
            "FROM system.processes"
        )
        return [
            Session(
                session_id=r["query_id"],
                user=r.get("user"),
                current_operation=r.get("query"),
                state="running",
                duration_ms=r.get("duration_ms"),
            )
            for r in rows
        ]

    # ------------------------------------------------------------------
    # ErrorLogCapable
    # ------------------------------------------------------------------

    def get_errors(self, since: Optional[datetime] = None) -> list[DBError]:
        try:
            rows = self.execute_readonly(
                "SELECT name, value, last_error_message, last_error_time "
                "FROM system.errors WHERE value > 0 ORDER BY last_error_time DESC LIMIT 50"
            )
        except Exception:
            return []
        return [
            DBError(
                error_code=r.get("name"),
                message=r.get("last_error_message") or "",
                occurred_at=r.get("last_error_time"),
            )
            for r in rows
        ]

    # ------------------------------------------------------------------
    # SchemaCapable
    # ------------------------------------------------------------------

    def get_schema(self, scope: dict[str, Any]) -> list[SchemaObject]:
        database = validate_identifier(
            scope.get("schema") or self.connection_profile["connection"].get("database", "default")
        )
        container = scope.get("container")

        sql = """
            SELECT t.database AS table_schema,
                   t.name AS table_name,
                   t.comment AS table_comment,
                   t.total_rows AS row_count_estimate,
                   c.name AS column_name,
                   c.type AS data_type,
                   c.comment AS column_comment,
                   (c.is_in_primary_key = 1) AS is_pk
            FROM system.tables t
            JOIN system.columns c ON c.database = t.database AND c.table = t.name
            WHERE t.database = {schema:String}
        """
        params: dict[str, Any] = {"schema": database}
        if container:
            sql += " AND t.name = {container:String}"
            params["container"] = validate_identifier(container)

        rows = self.execute_readonly(sql, params)
        return rows_to_schema_objects(rows)

    # ------------------------------------------------------------------
    # StatisticsCapable
    # ------------------------------------------------------------------

    def get_table_statistics(
        self, namespace: Optional[str], container: str
    ) -> ContainerStatistics:
        database = validate_identifier(
            namespace or self.connection_profile["connection"].get("database", "default")
        )
        rows = self.execute_readonly(
            "SELECT sum(rows) AS row_count_estimate, sum(bytes_on_disk) AS size_bytes "
            "FROM system.parts WHERE database = {schema:String} AND table = {table:String} "
            "AND active = 1",
            {"schema": database, "table": validate_identifier(container)},
        )
        if not rows:
            return ContainerStatistics()
        r = rows[0]
        return ContainerStatistics(
            row_count_estimate=r.get("row_count_estimate"), size_bytes=r.get("size_bytes")
        )

    def get_index_statistics(
        self, namespace: Optional[str], container: str
    ) -> list[IndexStatistics]:
        # ClickHouse는 명시적 Index 대신 ORDER BY(Sort Key) 기준으로 스캔 범위를 줄인다.
        database = validate_identifier(
            namespace or self.connection_profile["connection"].get("database", "default")
        )
        rows = self.execute_readonly(
            "SELECT sorting_key FROM system.tables "
            "WHERE database = {schema:String} AND name = {table:String}",
            {"schema": database, "table": validate_identifier(container)},
        )
        if not rows or not rows[0].get("sorting_key"):
            return []
        columns = [c.strip() for c in rows[0]["sorting_key"].split(",") if c.strip()]
        return [IndexStatistics(index_name="ORDER BY (sorting key)", columns=columns, is_unique=False)]

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
        database = validate_identifier(
            namespace or self.connection_profile["connection"].get("database", "default")
        )
        table = validate_identifier(container)
        limit = min(limit, 1000)
        sql = f"SELECT * FROM `{database}`.`{table}` LIMIT {{limit:UInt32}}"
        rows = self.execute_readonly(sql, {"limit": limit})
        if mask:
            for row in rows:
                for col in mask:
                    if col in row:
                        row[col] = "***"
        return rows

    def close(self) -> None:
        client = self._client
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass


AdapterRegistry.register("clickhouse", ClickHouseAdapter, engine_family="dw")
