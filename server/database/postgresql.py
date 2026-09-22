"""PostgreSQL Adapter (Engine Family: rdb).

Aurora PostgreSQL과 단독 설치 PostgreSQL을 모두 대상으로 한다. 구조는
mysql.py와 동일하게 server/database/sql_base.py(dao.py 참고 구조)를 따른다.

Driver: psycopg(3), row_factory=dict_row 로 PropLBS dao.py의
psycopg2.extras.DictCursor와 동일한 결과 형태(dict)를 얻는다.
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


class PostgreSQLAdapter(
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

    paramstyle = "pyformat"
    engine_name = "postgresql"

    def _connect(self) -> Any:
        import psycopg  # 지연 import — Extras로만 설치되는 optional dependency
        from psycopg.rows import dict_row

        conn = self.connection_profile["connection"]
        return psycopg.connect(
            host=conn["host"],
            port=conn["port"],
            dbname=conn["database"],
            user=self.connection_profile["user"],
            password=self.connection_profile["password"],
            connect_timeout=self.connect_timeout_sec,
            # statement_timeout / default_transaction_read_only 모두
            # PostgreSQL 서버가 세션 단위로 직접 강제하는 GUC다.
            # default_transaction_read_only=on은 관리자 계정을 쓰더라도
            # 이 세션의 모든 트랜잭션에서 쓰기를 서버가 거부하게 만든다 —
            # dblink/함수 호출에 숨겨진 쓰기 부작용까지 막는 세 번째 방어선
            # (assert_readonly_sql의 텍스트 검사와 독립적, Doc/00 §29 참고).
            options=(
                f"-c statement_timeout={int(self.query_timeout_sec * 1000)} "
                "-c default_transaction_read_only=on"
            ),
            row_factory=dict_row,
            autocommit=True,
        )

    def _is_closed(self, conn: Any) -> bool:
        return conn.closed

    def _ping_sql(self) -> str:
        return "SELECT version() AS version"

    def _default_schema(self) -> str:
        return self.connection_profile.get("schema", "public")

    # ------------------------------------------------------------------
    # SqlCapable
    # ------------------------------------------------------------------

    def explain(self, sql: str) -> ExplainPlan:
        rows = self.execute_readonly(f"EXPLAIN (FORMAT JSON, ANALYZE FALSE) {sql}")
        raw = rows[0]["QUERY PLAN"][0] if rows else {}
        plan = raw.get("Plan", {})
        return ExplainPlan(
            raw=raw,
            scan_type=plan.get("Node Type"),
            estimated_cost=_to_float(plan.get("Total Cost")),
            estimated_rows=plan.get("Plan Rows"),
        )

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
            )
            for r in rows
        ]

    def get_locks(self) -> list[Lock]:
        rows = self._run_template("locks")
        return [
            Lock(
                holding_session_id=str(r["holding_session_id"]),
                waiting_session_id=(
                    str(r["waiting_session_id"])
                    if r.get("waiting_session_id") is not None
                    else None
                ),
                resource=r.get("resource") or "",
                lock_type=r.get("lock_type") or "",
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
        # PostgreSQL은 기본적으로 SQL로 조회 가능한 Error Log 테이블이 없다
        # (보통 파일 기반 로그 또는 CloudWatch 등 외부 수집 사용).
        # pg_stat_database의 오류성 카운터만 근사치로 사용한다.
        sql = """
            SELECT datname, deadlocks, xact_rollback
            FROM pg_stat_database
            WHERE datname = current_database()
        """
        try:
            rows = self.execute_readonly(sql)
        except Exception:
            return []
        if not rows:
            return []
        r = rows[0]
        return [
            DBError(
                error_code="deadlocks",
                message=f"누적 Deadlock 수: {r.get('deadlocks')}, "
                f"Rollback 수: {r.get('xact_rollback')} (pg_stat_database 근사치)",
                occurred_at=None,
            )
        ]

    # ------------------------------------------------------------------
    # SchemaCapable
    # ------------------------------------------------------------------

    def get_schema(self, scope: dict[str, Any]) -> list[SchemaObject]:
        schema = validate_identifier(scope.get("schema") or self._default_schema())
        container = scope.get("container")

        sql = """
            SELECT c.table_schema AS table_schema,
                   c.table_name AS table_name,
                   d.description AS table_comment,
                   NULL AS row_count_estimate,
                   c.column_name AS column_name,
                   c.data_type AS data_type,
                   pgd.description AS column_comment,
                   (kcu.column_name IS NOT NULL) AS is_pk
            FROM information_schema.columns c
            LEFT JOIN pg_catalog.pg_statio_all_tables st
                   ON st.schemaname = c.table_schema AND st.relname = c.table_name
            LEFT JOIN pg_catalog.pg_description d
                   ON d.objoid = st.relid AND d.objsubid = 0
            LEFT JOIN pg_catalog.pg_description pgd
                   ON pgd.objoid = st.relid AND pgd.objsubid = c.ordinal_position
            LEFT JOIN information_schema.table_constraints tc
                   ON tc.table_schema = c.table_schema AND tc.table_name = c.table_name
                  AND tc.constraint_type = 'PRIMARY KEY'
            LEFT JOIN information_schema.key_column_usage kcu
                   ON kcu.constraint_name = tc.constraint_name
                  AND kcu.column_name = c.column_name
            WHERE c.table_schema = :schema
        """
        params: dict[str, Any] = {"schema": schema}
        if container:
            sql += " AND c.table_name = :container"
            params["container"] = validate_identifier(container)
        sql += " ORDER BY c.table_name, c.ordinal_position"

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
        return [
            IndexStatistics(
                index_name=r["index_name"],
                columns=[],  # pg_index 컬럼 목록까지 붙이려면 unnest(indkey) 추가 조인 필요
                usage_count=r.get("usage_count"),
                is_unique=bool(r.get("is_unique")),
            )
            for r in rows
        ]

    # ------------------------------------------------------------------
    # RelationshipCapable
    # ------------------------------------------------------------------

    def get_relationships(self, containers: list[str]) -> list[Relationship]:
        if not containers:
            return []
        for c in containers:
            validate_identifier(c)
        schema = self._default_schema()
        placeholders, in_params = build_in_clause("c", containers)
        sql = f"""
            SELECT
                tc.table_name AS from_container,
                kcu.column_name AS from_field,
                ccu.table_name AS to_container,
                ccu.column_name AS to_field
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON kcu.constraint_name = tc.constraint_name AND kcu.table_schema = tc.table_schema
            JOIN information_schema.constraint_column_usage ccu
              ON ccu.constraint_name = tc.constraint_name AND ccu.table_schema = tc.table_schema
            WHERE tc.constraint_type = 'FOREIGN KEY'
              AND tc.table_schema = :schema
              AND tc.table_name IN ({placeholders})
        """
        rows = self.execute_readonly(sql, {"schema": schema, **in_params})
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
        sql = f'SELECT * FROM "{schema}"."{table}" LIMIT :limit'
        rows = self.execute_readonly(sql, {"limit": limit})
        if mask:
            for row in rows:
                for col in mask:
                    if col in row:
                        row[col] = "***"
        return rows


def _to_float(value: Any) -> Optional[float]:
    return float(value) if value is not None else None


AdapterRegistry.register("postgresql", PostgreSQLAdapter, engine_family="rdb")
