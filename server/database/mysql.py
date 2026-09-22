"""MySQL Adapter (Engine Family: rdb).

Aurora MySQL과 단독 설치 MySQL을 모두 대상으로 한다. 연결/쿼리 실행 구조는
PropLBS(app/db/dao.py)의 Database 클래스를 참고해 server/database/sql_base.py
로 일반화했다 — 이 파일은 MySQL 고유의 연결 방법과 EXPLAIN/스키마 조회
SQL만 채운다.

고정된 형태의 Monitoring SQL(Slow Query/Lock/Session/통계)은
server/sql/mysql/*.sql 에서 로드하고, 대상 Table/조건이 호출마다 달라지는
조회(get_schema/get_relationships/sample_rows)는 이 파일에서 값은 항상
Parameter Binding으로, 식별자(Table/Schema명)는 validate_identifier()로
검증한 뒤에만 조합한다.

Driver: PyMySQL.
"""
from __future__ import annotations

import json
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


class MySQLAdapter(
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
    engine_name = "mysql"

    # ------------------------------------------------------------------
    # 연결 (dao.py의 connect()/psycopg2.connect 대응)
    # ------------------------------------------------------------------

    def _connect(self) -> Any:
        import pymysql  # 지연 import — Extras로만 설치되는 optional dependency
        import pymysql.cursors

        conn = self.connection_profile["connection"]
        return pymysql.connect(
            host=conn["host"],
            port=conn["port"],
            database=conn["database"],
            user=self.connection_profile["user"],
            password=self.connection_profile["password"],
            connect_timeout=self.connect_timeout_sec,
            # read/write_timeout은 소켓 레벨 제한이라 "쿼리 하나가 이 시간을
            # 넘기면 끊는다"는 Query Timeout(§33)의 실질적인 강제 수단이 된다.
            read_timeout=self.query_timeout_sec,
            write_timeout=self.query_timeout_sec,
            cursorclass=pymysql.cursors.DictCursor,  # dao.py의 DictCursor와 동일한 의도
            autocommit=True,
        )

    def _is_closed(self, conn: Any) -> bool:
        return not conn.open

    def _after_connect(self, conn: Any) -> None:
        # 관리자 계정을 쓰더라도 세션 자체를 MySQL 서버가 Read Only로
        # 강제하게 만든다 — SELECT 안에 숨은 함수/프로시저 호출의 쓰기
        # 부작용까지 서버가 직접 거부한다(assert_readonly_sql의 텍스트
        # 검사로는 원천적으로 잡을 수 없는 영역, Doc/00 §29 세 번째 방어선).
        cursor = conn.cursor()
        try:
            cursor.execute("SET SESSION TRANSACTION READ ONLY")
        finally:
            cursor.close()

    def _ping_sql(self) -> str:
        return "SELECT VERSION() AS version"

    def _default_schema(self) -> str:
        return self.connection_profile["connection"]["database"]

    # ------------------------------------------------------------------
    # SqlCapable
    # ------------------------------------------------------------------

    def explain(self, sql: str) -> ExplainPlan:
        rows = self.execute_readonly(f"EXPLAIN FORMAT=JSON {sql}")
        raw = json.loads(rows[0]["EXPLAIN"]) if rows else {}
        query_block = raw.get("query_block", {})
        return ExplainPlan(
            raw=raw,
            scan_type=query_block.get("table", {}).get("access_type"),
            estimated_cost=_to_float(query_block.get("cost_info", {}).get("query_cost")),
            estimated_rows=query_block.get("table", {}).get("rows_examined_per_scan"),
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
                rows_examined=r.get("rows_examined"),
                rows_returned=r.get("rows_returned"),
                database=r.get("database_name"),
                last_seen=r.get("last_seen"),
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
        # performance_schema.error_log 는 버전/설정에 따라 없을 수 있어
        # 방어적으로 처리한다 (Aurora 등 관리형 환경은 접근이 제한될 수 있음).
        sql = """
            SELECT LOGGED AS occurred_at, PRIO AS error_code, DATA AS message
            FROM performance_schema.error_log
        """
        params: dict[str, Any] = {}
        if since is not None:
            sql += " WHERE LOGGED >= :since"
            params["since"] = since
        sql += " ORDER BY LOGGED DESC LIMIT 50"
        try:
            rows = self.execute_readonly(sql, params)
        except Exception:
            return []
        return [
            DBError(
                error_code=r.get("error_code"),
                message=r.get("message") or "",
                occurred_at=r.get("occurred_at"),
            )
            for r in rows
        ]

    # ------------------------------------------------------------------
    # SchemaCapable
    # ------------------------------------------------------------------

    def get_schema(self, scope: dict[str, Any]) -> list[SchemaObject]:
        schema = validate_identifier(scope.get("schema") or self._default_schema())
        container = scope.get("container")

        sql = """
            SELECT t.TABLE_SCHEMA AS table_schema,
                   t.TABLE_NAME AS table_name,
                   t.TABLE_COMMENT AS table_comment,
                   t.TABLE_ROWS AS row_count_estimate,
                   c.COLUMN_NAME AS column_name,
                   c.DATA_TYPE AS data_type,
                   c.COLUMN_COMMENT AS column_comment,
                   (c.COLUMN_KEY = 'PRI') AS is_pk
            FROM information_schema.TABLES t
            JOIN information_schema.COLUMNS c
              ON c.TABLE_SCHEMA = t.TABLE_SCHEMA AND c.TABLE_NAME = t.TABLE_NAME
            WHERE t.TABLE_SCHEMA = :schema
        """
        params: dict[str, Any] = {"schema": schema}
        if container:
            sql += " AND t.TABLE_NAME = :container"
            params["container"] = validate_identifier(container)
        sql += " ORDER BY t.TABLE_NAME, c.ORDINAL_POSITION"

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
                    index_name=name, columns=[], is_unique=not bool(r.get("non_unique"))
                )
                grouped[name] = idx
            idx.columns.append(r["column_name"])
            if r.get("cardinality") is not None:
                idx.cardinality = r["cardinality"]
        return list(grouped.values())

    # ------------------------------------------------------------------
    # RelationshipCapable — 선언된 FK만 반환 (추정은 catalog/erd_builder.py 몫)
    # ------------------------------------------------------------------

    def get_relationships(self, containers: list[str]) -> list[Relationship]:
        if not containers:
            return []
        for c in containers:
            validate_identifier(c)
        schema = self._default_schema()
        placeholders, in_params = build_in_clause("c", containers)
        sql = f"""
            SELECT TABLE_NAME AS from_container, COLUMN_NAME AS from_field,
                   REFERENCED_TABLE_NAME AS to_container, REFERENCED_COLUMN_NAME AS to_field
            FROM information_schema.KEY_COLUMN_USAGE
            WHERE TABLE_SCHEMA = :schema
              AND REFERENCED_TABLE_NAME IS NOT NULL
              AND TABLE_NAME IN ({placeholders})
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
        sql = f"SELECT * FROM `{schema}`.`{table}` LIMIT :limit"
        rows = self.execute_readonly(sql, {"limit": limit})
        if mask:
            for row in rows:
                for col in mask:
                    if col in row:
                        row[col] = "***"
        return rows


def _to_float(value: Any) -> Optional[float]:
    return float(value) if value is not None else None


AdapterRegistry.register("mysql", MySQLAdapter, engine_family="rdb")
