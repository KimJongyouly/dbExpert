"""Tibero Adapter (Engine Family: rdb).

연결 방식은 실제 운영 Tibero 서버에서 검증한 JDBC 접속 샘플을
그대로 참고했다 — MySQL/PostgreSQL 같은 순정 Python DB-API 드라이버가
없어서, JPype로 JVM을 띄우고 Tibero JDBC Driver를 `java.sql.DriverManager`
로 직접 호출하는 방식을 쓴다. 이 파일이 ClickHouse Adapter처럼
SqlAdapterBase(DB-API cursor 가정)를 쓰지 않고 execute_readonly를 직접
구현하는 이유도 JDBC ResultSet이 Python DB-API cursor와 모양이 다르기
때문이다.

⚠️ JDBC 드라이버는 저장소에 포함하지 않는다 (Doc/01_개발정의서.md §11
미결정 사항 → 해결, 단 새로운 주의사항으로 대체됨)
Tibero JDBC 드라이버(tibero6-jdbc.jar)는 TmaxData의 배포물이므로 이
저장소에 번들하지 않는다. 사용자가 대상 서버 버전에 맞는 jar를 직접 구해
drivers/tibero/tibero6-jdbc.jar 경로에 두어야 한다(drivers/tibero/README.md
참고). 이 Adapter는 Tibero 5.0 서버 + 6.0.166454 드라이버 조합에서만
검증되었으므로, 다른 조합에서는 호환성 문제가 생길 수 있다 — 최초 연결 시
RuntimeWarning을 남기고, ping() 응답에도 driver_warning 필드로 노출한다.

Driver: JPype1 (Java ↔ Python 브릿지). 호스트에 JVM(Java Runtime)이 별도로
설치되어 있어야 한다 — pip 의존성과 별개의 운영 준비사항이다.
"""
from __future__ import annotations

import threading
import warnings
from datetime import datetime
from pathlib import Path
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
    Relationship,
    RelationshipCapable,
    SampleCapable,
    SchemaCapable,
    SchemaObject,
    Session,
    SlowOperation,
    SlowOperationCapable,
    SqlCapable,
    StatisticsCapable,
)
from server.database.errors import DatabaseQueryError, DBConnectionError
from server.database.param_binding import build_in_clause, convert_named_params
from server.database.registry import AdapterRegistry
from server.database.sql_base import load_sql_template, rows_to_schema_objects, validate_identifier
from server.database.sql_guard import assert_readonly_sql

_DRIVER_JAR_PATH = (
    Path(__file__).resolve().parent.parent.parent / "drivers" / "tibero" / "tibero6-jdbc.jar"
)
_BUNDLED_DRIVER_VERIFIED_SERVER_VERSION = "Tibero 5.0"

_BUNDLED_DRIVER_WARNING = (
    f"[dbExpert] Tibero Adapter는 {_BUNDLED_DRIVER_VERIFIED_SERVER_VERSION} 서버 + "
    f"{_DRIVER_JAR_PATH.name}(6.0.166454) 조합에서만 접속이 검증되었습니다. "
    "접속 대상 Tibero 서버/드라이버 버전이 다르면 연결/쿼리 호환성 문제가 발생할 수 있습니다. "
    f"드라이버 준비 방법은 {_DRIVER_JAR_PATH.parent}/README.md 를 참고하세요."
)

# JPype는 프로세스당 JVM을 1개만 띄울 수 있어, 여러 TiberoAdapter 인스턴스가
# 있어도 JVM 기동/경고는 프로세스 전체에서 한 번만 수행한다.
_jvm_lock = threading.Lock()
_driver_warning_emitted = False


def _ensure_jvm_started() -> None:
    global _driver_warning_emitted
    import jpype  # 지연 import — Extras로만 설치되는 optional dependency

    # JPype는 `import jpype`만으로는 `from java.xxx import Yyy` 문법을 지원하지
    # 않는다 — `java.*` 패키지를 파이썬 모듈처럼 import할 수 있게 해주는
    # meta path finder는 `jpype.imports`를 명시적으로 import해야 등록된다.
    # 이걸 빠뜨리면 JVM이 정상적으로 떠 있어도(isJVMStarted()==True)
    # `from java.sql import DriverManager`가 `ModuleNotFoundError: No module
    # named 'java'`로 실패한다 — 실제 Tibero 서버로 접속 테스트하다 재현/확인됨.
    import jpype.imports  # noqa: F401

    with _jvm_lock:
        if not _driver_warning_emitted:
            warnings.warn(_BUNDLED_DRIVER_WARNING, RuntimeWarning, stacklevel=3)
            _driver_warning_emitted = True

        if jpype.isJVMStarted():
            return

        if not _DRIVER_JAR_PATH.exists():
            raise DBConnectionError(
                f"Tibero JDBC Driver를 찾을 수 없습니다: {_DRIVER_JAR_PATH} — "
                "드라이버는 저장소에 포함되지 않습니다. drivers/tibero/README.md를 참고해 "
                "대상 서버 버전에 맞는 tibero6-jdbc.jar를 그 경로에 두세요."
            )

        jpype.startJVM(
            jpype.getDefaultJVMPath(),
            f"-Djava.class.path={_DRIVER_JAR_PATH}",
            "--enable-native-access=ALL-UNNAMED",
            convertStrings=False,
        )


class TiberoAdapter(
    BaseAdapter,
    SqlCapable,
    SlowOperationCapable,
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

    engine_name = "tibero"

    def __init__(self, connection_profile: dict[str, Any]):
        super().__init__(connection_profile)
        self._local = threading.local()  # JDBC Connection도 스레드별로 분리 보관

    @property
    def _conn(self) -> Any:
        return getattr(self._local, "conn", None)

    @_conn.setter
    def _conn(self, value: Any) -> None:
        self._local.conn = value

    def _default_schema(self) -> str:
        return self.connection_profile["user"].upper()

    def _ensure_connection(self) -> Any:
        conn = self._conn
        if conn is not None:
            return conn

        _ensure_jvm_started()
        from java.sql import DriverManager  # noqa: E402 — JVM 기동 후에만 import 가능

        # DriverManager.setLoginTimeout은 static(=프로세스 전체 JDBC 연결에
        # 공통 적용)이라 Adapter 인스턴스마다 다른 값을 줄 수는 없지만, 최소한
        # config/connections.yaml의 connect_timeout_sec를 반영은 해 둔다.
        DriverManager.setLoginTimeout(self.connect_timeout_sec)

        conn_info = self.connection_profile["connection"]
        conn_url = f"jdbc:tibero:thin:@{conn_info['host']}:{conn_info['port']}:tibero"
        try:
            conn = DriverManager.getConnection(
                conn_url,
                self.connection_profile["user"],
                self.connection_profile["password"],
            )
        except Exception as exc:  # noqa: BLE001
            raise DBConnectionError(str(exc)) from exc

        # JDBC 표준 setReadOnly(true) — 관리자 계정을 쓰더라도 Driver/서버가
        # 지원하면 세션을 Read Only로 강제하는 세 번째 방어선(Doc/00 §29
        # 참고). Oracle 계열은 이 힌트를 강제로 지키지 않는 구현도 있어
        # best-effort로 처리한다 — 실제 Tibero 서버로 검증되지 않았으므로
        # (§11 "검증 필요" 대상) 실패해도 연결 자체는 계속 진행한다.
        try:
            conn.setReadOnly(True)
        except Exception:  # noqa: BLE001
            pass

        self._conn = conn
        return conn

    # ------------------------------------------------------------------
    # SqlCapable
    # ------------------------------------------------------------------

    def ping(self) -> dict[str, Any]:
        try:
            rows = self.execute_readonly("SELECT * FROM V$VERSION WHERE ROWNUM = 1")
            return {
                "connected": True,
                "detail": rows[0] if rows else None,
                "driver_warning": _BUNDLED_DRIVER_WARNING,
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "connected": False,
                "error": str(exc),
                "driver_warning": _BUNDLED_DRIVER_WARNING,
            }

    def execute_readonly(
        self, sql: str, params: Optional[dict[str, Any]] = None
    ) -> list[dict[str, Any]]:
        assert_readonly_sql(sql)
        bound_sql, bound_params = convert_named_params(sql, params, "qmark")

        conn = self._ensure_connection()
        stmt = None
        result_set = None
        try:
            stmt = conn.prepareStatement(bound_sql)
            stmt.setQueryTimeout(self.query_timeout_sec)  # JDBC 표준 Query Timeout
            for index, value in enumerate(bound_params, start=1):
                stmt.setObject(index, value)
            result_set = stmt.executeQuery()

            meta = result_set.getMetaData()
            column_count = meta.getColumnCount()
            columns = [str(meta.getColumnName(i + 1)) for i in range(column_count)]

            rows: list[dict[str, Any]] = []
            while result_set.next():
                row: dict[str, Any] = {}
                for i, column in enumerate(columns, start=1):
                    # dibero.py와 동일하게 전부 문자열로 받는다 — 타입 정보는
                    # 잃지만(Doc §11 참고 대상), 드라이버별 타입 매핑 불확실성을
                    # 줄이는 실용적 선택이다.
                    value = result_set.getString(i)
                    row[column] = None if value is None else str(value)
                rows.append(row)
            return rows
        except Exception as exc:  # noqa: BLE001
            raise DatabaseQueryError(str(exc)) from exc
        finally:
            if result_set is not None:
                result_set.close()
            if stmt is not None:
                stmt.close()

    def explain(self, sql: str) -> ExplainPlan:
        assert_readonly_sql(sql)
        conn = self._ensure_connection()
        stmt = None
        try:
            stmt = conn.createStatement()
            stmt.setQueryTimeout(self.query_timeout_sec)
            stmt.execute(f"EXPLAIN PLAN FOR {sql}")
        except Exception as exc:  # noqa: BLE001
            raise DatabaseQueryError(str(exc)) from exc
        finally:
            if stmt is not None:
                stmt.close()

        # TODO: Tibero 버전별 DBMS_XPLAN.DISPLAY() 지원 여부 검증 필요
        plan_rows = self.execute_readonly("SELECT * FROM TABLE(DBMS_XPLAN.DISPLAY())")
        return ExplainPlan(raw={"plan_rows": plan_rows})

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
                text=r.get("text") or "",
                execution_count=int(r.get("execution_count") or 0),
                avg_duration_ms=float(r.get("avg_duration_ms") or 0),
                max_duration_ms=float(r.get("max_duration_ms") or 0),
            )
            for r in rows
        ]

    def get_locks(self) -> list[Lock]:
        rows = self._run_template("locks")
        return [
            Lock(
                holding_session_id=str(r["holding_session_id"]),
                resource=r.get("resource") or "",
                lock_type=r.get("lock_type") or "",
            )
            for r in rows
        ]

    def get_sessions(self) -> list[Session]:
        rows = self._run_template("sessions")
        return [
            Session(
                # SID/SERIAL# 조합이 Oracle 계열(Tibero 포함)에서 세션을
                # 유일하게 식별하는 표준 키다(SID만으로는 재사용되어 겹칠 수 있음).
                session_id=f"{r.get('SID')}/{r.get('SERIAL#')}",
                user=r.get("USERNAME"),
                host=r.get("MACHINE"),
                current_operation=r.get("TYPE"),
                state=r.get("STATUS") or "",
            )
            for r in rows
        ]

    # ------------------------------------------------------------------
    # ErrorLogCapable
    # ------------------------------------------------------------------

    def get_errors(self, since: Optional[datetime] = None) -> list[DBError]:
        # TODO: Alert Log/Trace 파일 기반이라 SQL만으로는 접근 불가할 가능성이 높음.
        return []

    # ------------------------------------------------------------------
    # SchemaCapable
    # ------------------------------------------------------------------

    def get_schema(self, scope: dict[str, Any]) -> list[SchemaObject]:
        schema = validate_identifier(scope.get("schema") or self._default_schema())
        container = scope.get("container")

        sql = """
            SELECT t.OWNER AS table_schema,
                   t.TABLE_NAME AS table_name,
                   tc.COMMENTS AS table_comment,
                   t.NUM_ROWS AS row_count_estimate,
                   c.COLUMN_NAME AS column_name,
                   c.DATA_TYPE AS data_type,
                   cc.COMMENTS AS column_comment,
                   (pk.COLUMN_NAME IS NOT NULL) AS is_pk
            FROM ALL_TABLES t
            JOIN ALL_TAB_COLUMNS c ON c.OWNER = t.OWNER AND c.TABLE_NAME = t.TABLE_NAME
            LEFT JOIN ALL_TAB_COMMENTS tc ON tc.OWNER = t.OWNER AND tc.TABLE_NAME = t.TABLE_NAME
            LEFT JOIN ALL_COL_COMMENTS cc
                   ON cc.OWNER = c.OWNER AND cc.TABLE_NAME = c.TABLE_NAME AND cc.COLUMN_NAME = c.COLUMN_NAME
            LEFT JOIN (
                SELECT acc.OWNER, acc.TABLE_NAME, acc.COLUMN_NAME
                FROM ALL_CONSTRAINTS ac
                JOIN ALL_CONS_COLUMNS acc ON acc.CONSTRAINT_NAME = ac.CONSTRAINT_NAME
                WHERE ac.CONSTRAINT_TYPE = 'P'
            ) pk ON pk.OWNER = c.OWNER AND pk.TABLE_NAME = c.TABLE_NAME AND pk.COLUMN_NAME = c.COLUMN_NAME
            WHERE t.OWNER = :schema
        """
        params: dict[str, Any] = {"schema": schema}
        if container:
            sql += " AND t.TABLE_NAME = :container"
            params["container"] = validate_identifier(container)

        rows = self.execute_readonly(sql, params)
        return rows_to_schema_objects(rows)

    # ------------------------------------------------------------------
    # StatisticsCapable
    # ------------------------------------------------------------------

    def get_table_statistics(
        self, namespace: Optional[str], container: str
    ) -> ContainerStatistics:
        schema = validate_identifier(namespace or self._default_schema())
        rows = self.execute_readonly(
            "SELECT NUM_ROWS AS row_count_estimate, "
            "(BLOCKS * 8192) AS size_bytes, LAST_ANALYZED AS last_analyzed_at "
            "FROM ALL_TABLES WHERE OWNER = :schema AND TABLE_NAME = :table",
            {"schema": schema, "table": validate_identifier(container)},
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
        rows = self.execute_readonly(
            "SELECT INDEX_NAME AS index_name, UNIQUENESS AS uniqueness "
            "FROM ALL_INDEXES WHERE OWNER = :schema AND TABLE_NAME = :table",
            {"schema": schema, "table": validate_identifier(container)},
        )
        return [
            IndexStatistics(
                index_name=r["index_name"],
                columns=[],  # TODO: ALL_IND_COLUMNS 조인 추가
                is_unique=(r.get("uniqueness") == "UNIQUE"),
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
                ac.TABLE_NAME AS from_container,
                acc.COLUMN_NAME AS from_field,
                ac_r.TABLE_NAME AS to_container,
                acc_r.COLUMN_NAME AS to_field
            FROM ALL_CONSTRAINTS ac
            JOIN ALL_CONS_COLUMNS acc ON acc.CONSTRAINT_NAME = ac.CONSTRAINT_NAME
            JOIN ALL_CONSTRAINTS ac_r ON ac_r.CONSTRAINT_NAME = ac.R_CONSTRAINT_NAME
            JOIN ALL_CONS_COLUMNS acc_r
                 ON acc_r.CONSTRAINT_NAME = ac_r.CONSTRAINT_NAME AND acc_r.POSITION = acc.POSITION
            WHERE ac.CONSTRAINT_TYPE = 'R'
              AND ac.OWNER = :schema
              AND ac.TABLE_NAME IN ({placeholders})
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
        sql = f'SELECT * FROM "{schema}"."{table}" WHERE ROWNUM <= :limit'
        rows = self.execute_readonly(sql, {"limit": limit})
        if mask:
            for row in rows:
                for col in mask:
                    if col in row:
                        row[col] = "***"
        return rows

    # ------------------------------------------------------------------
    # 종료
    # ------------------------------------------------------------------

    def close(self) -> None:
        conn = self._conn
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
            self._conn = None


AdapterRegistry.register("tibero", TiberoAdapter, engine_family="rdb")
