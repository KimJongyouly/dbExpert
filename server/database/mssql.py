"""MSSQL(SQL Server) Adapter (Engine Family: rdb).

MySQL/PostgreSQL과 동일한 sql_base.py 구조를 사용한다. Paramstyle은 qmark
(`?`)이며, pymssql은 execute_readonly() 공통 로직이 IN-list까지 그대로
처리한다(:name → ? 치환은 param_binding.convert_named_params가 담당).

Driver: pymssql. ODBC Driver 설치가 필요 없는 FreeTDS 기반이라 pyodbc보다
운영체제 종속성이 적다(Doc/01_개발정의서.md §11 미결정 사항 참고 — 그래도
pyodbc가 필요해지는 환경이 있는지는 검증이 필요하다).

⚠️ Read Only 세 번째 방어선 없음: MySQL/PostgreSQL/ClickHouse/Tibero와 달리
T-SQL에는 세션 전체를 Read Only로 강제하는 표준 수단이 없다(`ApplicationIntent
=ReadOnly`는 Always On 라우팅 힌트일 뿐 Standalone 서버에서는 쓰기를 막지
않는다). 따라서 관리자 계정으로 접속하는 경우 MSSQL은
`sql_guard.assert_readonly_sql()`(텍스트 검사)이 유일한 방어선이다 — 실제
운영에서는 이 Engine만큼은 DB 계정 자체를 Read Only 권한으로 발급하는 것을
강력히 권장한다(Doc/00 §29).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from server.database.base import (
    ActivityCapable,
    Capability,
    ContainerStatistics,
    DBError,
    ErrorLogCapable,
    ExplainPlan,
    IndexStatistics,
    Lock,
    Relationship,
    RelationshipCapable,
    SampleCapable,
    SchemaCapable,
    SchemaObject,
    Session,
    SlowOperation,
    SlowOperationCapable,
    StatisticsCapable,
)
from server.database.param_binding import build_in_clause
from server.database.registry import AdapterRegistry
from server.database.sql_base import SqlAdapterBase, rows_to_schema_objects, validate_identifier
from server.database.sql_guard import assert_readonly_sql


class MSSQLAdapter(
    SqlAdapterBase,
    ActivityCapable,
    ErrorLogCapable,
    SchemaCapable,
    StatisticsCapable,
    RelationshipCapable,
    SampleCapable,
):
    capabilities = {
        Capability.SQL,
        Capability.SLOW_OP,
        Capability.ACTIVITY,
        Capability.ERROR_LOG,
        Capability.SCHEMA,
        Capability.STATS,
        Capability.RELATIONSHIP,
        Capability.SAMPLE,
    }

    paramstyle = "qmark"
    engine_name = "mssql"

    def _connect(self) -> Any:
        import pymssql  # 지연 import — Extras로만 설치되는 optional dependency

        conn = self.connection_profile["connection"]
        return pymssql.connect(
            server=conn["host"],
            port=conn["port"],
            database=conn["database"],
            user=self.connection_profile["user"],
            password=self.connection_profile["password"],
            login_timeout=self.connect_timeout_sec,
            timeout=self.query_timeout_sec,  # pymssql의 query timeout 파라미터
            autocommit=True,
        )

    def _is_closed(self, conn: Any) -> bool:
        # pymssql은 closed 플래그가 없어 커서 생성 시도로 확인한다.
        try:
            conn.cursor().close()
            return False
        except Exception:
            return True

    def _make_cursor(self, conn: Any) -> Any:
        return conn.cursor(as_dict=True)

    def _ping_sql(self) -> str:
        return "SELECT @@VERSION AS version"

    def _default_schema(self) -> str:
        return self.connection_profile.get("schema", "dbo")

    # ------------------------------------------------------------------
    # SqlCapable
    # ------------------------------------------------------------------

    def explain(self, sql: str) -> ExplainPlan:
        # SET SHOWPLAN_XML은 세션 단위 옵션이라 execute_readonly의
        # "단일 SELECT/EXPLAIN 문만 허용" 가드와 별도 경로로 처리하지만,
        # 대상 sql 자체는 여전히 assert_readonly_sql로 한 번 더 검증한다
        # (SHOWPLAN_XML ON 상태에서는 실제로 실행되지 않지만, 방어는 이중으로 유지).
        assert_readonly_sql(sql)
        conn = self._ensure_connection()
        cursor = self._make_cursor(conn)
        try:
            cursor.execute("SET SHOWPLAN_XML ON")
            cursor.execute(sql)
            row = cursor.fetchone()
            xml_plan = row[list(row.keys())[0]] if row else None
        finally:
            cursor.execute("SET SHOWPLAN_XML OFF")
            cursor.close()
        return ExplainPlan(raw={"showplan_xml": xml_plan})

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
                rows_returned=r.get("rows_returned"),
                last_seen=r.get("last_seen"),
            )
            for r in rows
        ]

    def get_locks(self) -> list[Lock]:
        rows = self._run_template("locks")
        return [
            Lock(
                holding_session_id=str(r["holding_session_id"]),
                waiting_session_id=str(r["waiting_session_id"]),
                resource=r.get("resource") or "",
                lock_type=r.get("lock_type") or "",
                wait_time_ms=_to_float(r.get("wait_time_ms")),
            )
            for r in rows
        ]

    def get_sessions(self) -> list[Session]:
        rows = self._run_template("sessions")
        return [
            Session(
                session_id=str(r["session_id"]),
                user=r.get("user"),
                host=r.get("host"),
                current_operation=r.get("current_operation"),
                state=r.get("state") or "",
                duration_ms=_to_float(r.get("duration_ms")),
            )
            for r in rows
        ]

    # ------------------------------------------------------------------
    # ErrorLogCapable
    # ------------------------------------------------------------------

    def get_errors(self, since: Optional[datetime] = None) -> list[DBError]:
        # sp_readerrorlog는 시스템 프로시저 실행(EXEC)이 필요해 execute_readonly의
        # 가드를 통과하지 못한다 — 별도 경로로, 실패 시 빈 배열로 방어한다.
        conn = self._ensure_connection()
        cursor = self._make_cursor(conn)
        try:
            cursor.execute("EXEC sp_readerrorlog")
            rows = cursor.fetchall()
        except Exception:
            return []
        finally:
            cursor.close()
        return [
            DBError(
                error_code=None,
                message=str(r.get("Text") or r),
                occurred_at=r.get("LogDate"),
            )
            for r in rows[:50]
        ]

    # ------------------------------------------------------------------
    # SchemaCapable
    # ------------------------------------------------------------------

    def get_schema(self, scope: dict[str, Any]) -> list[SchemaObject]:
        schema = validate_identifier(scope.get("schema") or self._default_schema())
        container = scope.get("container")

        sql = """
            SELECT sc.name AS table_schema,
                   t.name AS table_name,
                   CAST(ep.value AS NVARCHAR(MAX)) AS table_comment,
                   NULL AS row_count_estimate,
                   c.name AS column_name,
                   ty.name AS data_type,
                   CAST(epc.value AS NVARCHAR(MAX)) AS column_comment,
                   (ic.column_id IS NOT NULL) AS is_pk
            FROM sys.tables t
            JOIN sys.schemas sc ON sc.schema_id = t.schema_id
            JOIN sys.columns c ON c.object_id = t.object_id
            JOIN sys.types ty ON ty.user_type_id = c.user_type_id
            LEFT JOIN sys.extended_properties ep
                   ON ep.major_id = t.object_id AND ep.minor_id = 0 AND ep.name = 'MS_Description'
            LEFT JOIN sys.extended_properties epc
                   ON epc.major_id = t.object_id AND epc.minor_id = c.column_id AND epc.name = 'MS_Description'
            LEFT JOIN sys.index_columns ic
                   ON ic.object_id = t.object_id AND ic.column_id = c.column_id AND ic.key_ordinal > 0
            WHERE sc.name = :schema
        """
        params: dict[str, Any] = {"schema": schema}
        if container:
            sql += " AND t.name = :container"
            params["container"] = validate_identifier(container)
        sql += " ORDER BY t.name, c.column_id"

        rows = self.execute_readonly(sql, params)
        return rows_to_schema_objects(rows)

    # ------------------------------------------------------------------
    # StatisticsCapable
    # ------------------------------------------------------------------

    def get_table_statistics(
        self, namespace: Optional[str], container: str
    ) -> ContainerStatistics:
        schema = validate_identifier(namespace or self._default_schema())
        rows = self._run_template(
            "table_statistics", {"schema": schema, "table": validate_identifier(container)}
        )
        if not rows:
            return ContainerStatistics()
        r = rows[0]
        return ContainerStatistics(
            row_count_estimate=r.get("row_count_estimate"),
            size_bytes=r.get("size_bytes"),
            last_analyzed_at=r.get("last_analyzed_at"),
        )

    def get_index_statistics(
        self, namespace: Optional[str], container: str
    ) -> list[IndexStatistics]:
        schema = validate_identifier(namespace or self._default_schema())
        rows = self._run_template(
            "index_statistics", {"schema": schema, "table": validate_identifier(container)}
        )
        grouped: dict[str, IndexStatistics] = {}
        for r in rows:
            name = r["index_name"]
            idx = grouped.get(name)
            if idx is None:
                idx = IndexStatistics(
                    index_name=name,
                    columns=[],
                    is_unique=bool(r.get("is_unique")),
                    usage_count=r.get("usage_count"),
                )
                grouped[name] = idx
            idx.columns.append(r["column_name"])
        return list(grouped.values())

    # ------------------------------------------------------------------
    # RelationshipCapable
    # ------------------------------------------------------------------

    def get_relationships(self, containers: list[str]) -> list[Relationship]:
        if not containers:
            return []
        for c in containers:
            validate_identifier(c)
        placeholders, in_params = build_in_clause("c", containers)
        sql = f"""
            SELECT
                OBJECT_NAME(fk.parent_object_id) AS from_container,
                COL_NAME(fkc.parent_object_id, fkc.parent_column_id) AS from_field,
                OBJECT_NAME(fk.referenced_object_id) AS to_container,
                COL_NAME(fkc.referenced_object_id, fkc.referenced_column_id) AS to_field
            FROM sys.foreign_keys fk
            JOIN sys.foreign_key_columns fkc ON fkc.constraint_object_id = fk.object_id
            WHERE OBJECT_NAME(fk.parent_object_id) IN ({placeholders})
        """
        rows = self.execute_readonly(sql, in_params)
        return [
            Relationship(
                from_container=r["from_container"],
                from_field=r["from_field"],
                to_container=r["to_container"],
                to_field=r["to_field"],
                cardinality="1:N",
                confidence="declared",
            )
            for r in rows
        ]

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
        schema = validate_identifier(namespace or self._default_schema())
        table = validate_identifier(container)
        limit = min(limit, 1000)
        sql = f"SELECT TOP (:limit) * FROM [{schema}].[{table}]"
        rows = self.execute_readonly(sql, {"limit": limit})
        if mask:
            for row in rows:
                for col in mask:
                    if col in row:
                        row[col] = "***"
        return rows


def _to_float(value: Any) -> Optional[float]:
    return float(value) if value is not None else None


AdapterRegistry.register("mssql", MSSQLAdapter, engine_family="rdb")
