"""SQL 기반 Engine(MySQL/PostgreSQL/Tibero/MSSQL/ClickHouse) 공통 연결/쿼리 구조.

PropLBS(app/db/dao.py)의 `Database` 클래스 구조를 참고해 Read-Only 전용으로
재구성했다. 대응 관계는 다음과 같다.

    dao.py                                  dbExpert (본 파일)
    ------------------------------------    ------------------------------------
    threading.local() 로 conn/cursor 보관    SqlAdapterBase._local (동일)
    connect()                                _connect() (Engine별 Hook)
    _ensure_connection() (재연결)             _ensure_connection() (동일 개념)
    _register_conn() + close() (전체 정리)   _register_conn() + close() (동일)
    query(sql, params) → dict 리스트         execute_readonly(sql, params) (동일,
                                              단 Read Only 가드가 항상 선행된다)
    self.product 로 MySQL/PostgreSQL 분기    Engine마다 별도 서브클래스 (mysql.py 등)

dao.py와의 가장 큰 차이는 "쓰기 경로 자체가 없다"는 것이다 — `_write()`,
`bulk_insert()`, `insert/update/delete/command` 에 대응하는 메서드를 아예
두지 않았고, `execute_readonly()`는 매 호출마다 `assert_readonly_sql()`로
구문을 검사한다(Doc/00_개발요건사항.md §29 이중 방어의 두 번째 방어선).
"""
from __future__ import annotations

import abc
import re
import threading
from pathlib import Path
from typing import Any, Optional

from server.database.base import (
    BaseAdapter,
    ExplainPlan,
    SchemaField,
    SchemaObject,
    SqlCapable,
)
from server.database.errors import DatabaseQueryError, QueryTimeoutError
from server.database.param_binding import ParamStyle, convert_named_params
from server.database.sql_guard import assert_readonly_sql

SQL_DIR = Path(__file__).resolve().parent.parent / "sql"

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


def load_sql_template(engine: str, name: str) -> str:
    """server/sql/<engine>/<name>.sql 을 읽어 반환한다.

    쿼리 문자열을 Adapter 코드에 하드코딩하지 않고 파일에서 로드한다
    (Doc/00_개발요건사항.md §30 SQL Injection 방지 원칙).
    """
    path = SQL_DIR / engine / f"{name}.sql"
    return path.read_text(encoding="utf-8")


def validate_identifier(name: str) -> str:
    """Table/Column/Schema명은 Parameter Binding이 불가능한(=식별자) 자리에만
    쓰인다. 값이 아니라 식별자이므로, 값처럼 Bind하는 대신 형식을 엄격히
    검증한 뒤에만 SQL 문자열에 직접 사용한다(허용 문자 외에는 전부 거부).
    """
    if not _IDENTIFIER_RE.match(name):
        raise ValueError(f"허용되지 않는 식별자입니다: {name!r}")
    return name


def rows_to_schema_objects(rows: list[dict[str, Any]]) -> list[SchemaObject]:
    """get_schema 구현이 공통으로 사용하는 flat row → SchemaObject 그룹핑.

    각 row는 다음 키를 표준으로 갖는다고 가정한다(엔진별 SQL이 이 이름으로
    별칭을 맞춘다): table_schema, table_name, table_comment,
    row_count_estimate, column_name, data_type, column_comment, is_pk.
    """
    grouped: dict[tuple[Optional[str], str], SchemaObject] = {}
    for row in rows:
        key = (row.get("table_schema"), row["table_name"])
        obj = grouped.get(key)
        if obj is None:
            obj = SchemaObject(
                namespace=row.get("table_schema"),
                container=row["table_name"],
                container_comment=row.get("table_comment") or None,
                row_count_estimate=row.get("row_count_estimate"),
            )
            grouped[key] = obj
        if row.get("column_name"):
            obj.fields.append(
                SchemaField(
                    name=row["column_name"],
                    type=row.get("data_type") or "",
                    comment=row.get("column_comment") or None,
                    is_pk=bool(row.get("is_pk")),
                )
            )
    return list(grouped.values())


class SqlAdapterBase(BaseAdapter, SqlCapable, abc.ABC):
    """Read-Only SQL Adapter 공통 기반.

    서브클래스(mysql.py 등)는 다음 Hook만 구현하면 된다:
      - _connect()       : 실제 DB-API 연결 객체 생성
      - _is_closed(conn) : 연결이 끊겼는지 확인
      - _ping_sql()       : 헬스체크용 SQL (기본값 "SELECT 1")
      - paramstyle        : "pyformat" | "qmark"
      - engine_name        : server/sql/<engine_name>/ 디렉터리명
    """

    paramstyle: ParamStyle = "pyformat"
    engine_name: str = ""
    # connect_timeout_sec / query_timeout_sec는 BaseAdapter의 프로퍼티를 그대로
    # 쓴다 (config/connections.yaml 값, 없으면 기본 5초/30초) — 여기서
    # 클래스 속성으로 다시 선언하면 프로퍼티를 가려버리므로 선언하지 않는다.

    def __init__(self, connection_profile: dict[str, Any]):
        super().__init__(connection_profile)
        self._local = threading.local()
        self._conns_lock = threading.Lock()
        self._all_conns: list[Any] = []

    # ------------------------------------------------------------------
    # 스레드별 연결 (dao.py의 self._local.conn / self._local.cursor 대응)
    # ------------------------------------------------------------------

    @property
    def _conn(self) -> Any:
        return getattr(self._local, "conn", None)

    @_conn.setter
    def _conn(self, value: Any) -> None:
        self._local.conn = value

    def _register_conn(self, conn: Any) -> None:
        with self._conns_lock:
            self._all_conns.append(conn)

    def _ensure_connection(self) -> Any:
        conn = self._conn
        if conn is None or self._is_closed(conn):
            conn = self._connect()
            self._after_connect(conn)
            self._conn = conn
            self._register_conn(conn)
        return conn

    def _after_connect(self, conn: Any) -> None:
        """새 연결 직후 1회 실행되는 Hook (기본은 아무것도 하지 않음).

        DB 계정 자체가 Read Only가 아닌 경우(관리자 계정 사용 등, Doc/00
        §29 참고)를 대비해, Engine이 세션 단위 Read Only 강제를 지원하면
        서브클래스가 이 Hook에서 켠다 — `assert_readonly_sql()`의 텍스트
        검사와 독립적인 세 번째 방어선이다(mysql.py 참고)."""

    # ------------------------------------------------------------------
    # Engine별 Hook (서브클래스 구현)
    # ------------------------------------------------------------------

    @abc.abstractmethod
    def _connect(self) -> Any:
        """실제 DB-API 연결 객체를 생성해 반환한다."""

    @abc.abstractmethod
    def _is_closed(self, conn: Any) -> bool:
        """연결이 끊겼으면 True. Driver마다 확인 방법이 다르다
        (psycopg: conn.closed, PyMySQL: conn.open 의 반대, pyodbc: try ping 등)."""

    def _ping_sql(self) -> str:
        return "SELECT 1"

    def _make_cursor(self, conn: Any) -> Any:
        """기본은 conn.cursor(). dict 결과를 위해 별도 인자가 필요한 Driver
        (예: pymssql의 as_dict=True)는 서브클래스에서 override 한다."""
        return conn.cursor()

    # ------------------------------------------------------------------
    # 공통 구현 (SqlCapable)
    # ------------------------------------------------------------------

    def ping(self) -> dict[str, Any]:
        try:
            rows = self.execute_readonly(self._ping_sql())
            return {"connected": True, "detail": rows[0] if rows else None}
        except Exception as exc:  # noqa: BLE001 — ping은 실패 사실 자체가 결과
            return {"connected": False, "error": str(exc)}

    def execute_readonly(
        self, sql: str, params: Optional[dict[str, Any]] = None
    ) -> list[dict[str, Any]]:
        assert_readonly_sql(sql)
        bound_sql, bound_params = convert_named_params(sql, params, self.paramstyle)

        conn = self._ensure_connection()
        cursor = self._make_cursor(conn)
        try:
            cursor.execute(bound_sql, bound_params)
            rows = cursor.fetchall()
            if rows and isinstance(rows[0], dict):
                return list(rows)  # DictCursor류(PyMySQL/pymssql as_dict=True)는 이미 dict
            columns = [d[0] for d in cursor.description] if cursor.description else []
            return [dict(zip(columns, row)) for row in rows]
        except Exception as exc:  # noqa: BLE001 — 아래에서 표준 예외로 재포장
            message = str(exc)
            if "timeout" in message.lower():
                raise QueryTimeoutError(message) from exc
            raise DatabaseQueryError(message) from exc
        finally:
            cursor.close()

    def explain(self, sql: str) -> ExplainPlan:
        raise NotImplementedError  # Engine별 EXPLAIN 문법이 달라 서브클래스에서 구현

    def _run_template(
        self, template_name: str, params: Optional[dict[str, Any]] = None
    ) -> list[dict[str, Any]]:
        """server/sql/<engine_name>/<template_name>.sql 을 읽어 실행한다."""
        sql = load_sql_template(self.engine_name, template_name)
        return self.execute_readonly(sql, params)

    # ------------------------------------------------------------------
    # 종료 (dao.py의 Database.close() 대응 — atexit은 ConnectionManager가 호출)
    # ------------------------------------------------------------------

    def close(self) -> None:
        with self._conns_lock:
            conns, self._all_conns = self._all_conns, []
        for conn in conns:
            try:
                conn.close()
            except Exception:  # noqa: BLE001 — 종료 중 에러는 무시하고 계속 정리
                pass
