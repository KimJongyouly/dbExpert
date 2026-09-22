"""dbExpert Plugin — MCP Server 진입점.

Doc/00_개발요건사항.md §27 MCP 설정, Doc/01_개발정의서.md §6.2 참고.
Tool 등록은 Doc/01_개발정의서.md §3 MCP Tool 명세를 그대로 따른다.

실행: `uv run python server/main.py` (.mcp.json이 이 경로로 기동한다)
"""
from __future__ import annotations

import atexit
import functools
from pathlib import Path
from typing import Any, Callable, Optional, TypeVar

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

from server import activity
from server.analyzer.error import db_error_analyze as _db_error_analyze
from server.analyzer.index_advisor import index_recommend as _index_recommend
from server.analyzer.query_rewriter import query_rewrite_suggest as _query_rewrite_suggest
from server.analyzer.slow_query import slow_query_list as _slow_query_list
from server.catalog.auto_refresh import refresh_stale_tables as _refresh_stale_tables
from server.catalog.erd_builder import get_table_relationships as _get_table_relationships
from server.catalog.object_search import search_object_catalog as _search_object_catalog
from server.catalog.ontology_builder import build_ontology_index as _build_ontology_index
from server.catalog.query_scope import extract_table_names as _extract_table_names
from server.connection.manager import ConnectionManager
from server.database.base import Capability
from server.database.registry import AdapterRegistry, register_builtin_adapters
from server.learning.background_writer import QALogWriter
from server.learning.pattern_summarizer import (
    get_query_patterns as _get_query_patterns,
)
from server.learning.pattern_summarizer import (
    summarize_query_patterns as _summarize_query_patterns,
)
from server.learning.qa_logger import build_qa_entry as _build_qa_entry
from server.learning.qa_logger import write_qa_entry as _write_qa_entry
from server.analyzer.session_monitor import get_locks as _get_locks
from server.analyzer.session_monitor import get_sessions as _get_sessions
from server.analyzer.table_stats import get_index_statistics as _get_index_statistics
from server.analyzer.table_stats import get_table_statistics as _get_table_statistics
from server.pipeline.data_profiler import profile_data as _profile_data
from server.pipeline.pipeline_builder import generate_pipeline_plan as _generate_pipeline_plan
from server.pipeline.pipeline_validator import validate_pipeline as _validate_pipeline
from server.pipeline.source_discovery import get_schema as _get_schema
from server.pipeline.source_discovery import list_raw_sources as _list_raw_sources
from server.query_builder.executor import execute_readonly_query as _execute_readonly_query
from server.query_builder.pattern_search import find_query_pattern as _find_query_pattern
from server.utils import to_serializable

# config/connections.yaml의 user_env/password_env가 실제로 가리키는 값들을
# Plugin 루트의 .env(커밋되지 않음, .gitignore 참고)에서 읽어온다 — Claude
# Code가 MCP Server를 하위 프로세스로 띄울 때는 사용자의 로그인 쉘
# 프로파일(~/.zshrc 등)을 거치지 않아 거기 내보낸(export) 환경변수를 못
# 물려받는 경우가 있다(실제로 이 문제로 서버가 기동 직후 자격증명을 하나도
# 못 찾아 죽은 적이 있음, Doc/02_설치.md §5 참고). load_dotenv()는 이
# 파일이 있으면 셸을 거치지 않고 직접 os.environ에 넣어주므로, 어떤
# 방식으로 MCP Server가 기동되든 항상 같은 값을 읽는다. ConnectionManager가
# 환경변수를 읽기 전(=connection_manager.load() 호출 전)이기만 하면 되므로
# 모듈 import 시점인 여기서 한 번만 실행하면 충분하다.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

mcp = FastMCP("db-analyzer")
connection_manager = ConnectionManager()
# log_qa는 이 Queue에 쌓였다가 컴퓨터가 한가하고(CPU 여유) Claude 작업이
# 없을 때만 실제로 디스크에 쓰인다 (server/learning/background_writer.py).
qa_log_writer = QALogWriter(write_entry=_write_qa_entry)

_F = TypeVar("_F", bound=Callable[..., Any])


def _tool(fn: _F) -> _F:
    """`@mcp.tool()`을 대체하는 등록 데코레이터 — 등록과 동시에, Tool이
    호출될 때마다 `activity.touch()`로 "최근 활동 시각"을 갱신한다. 이 값이
    QALogWriter가 "Claude 작업이 없다"를 판단하는 유일한 근거다."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        activity.touch()
        return fn(*args, **kwargs)

    return mcp.tool()(wrapper)  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Common
# ---------------------------------------------------------------------------


@_tool
def db_ping(connection: str) -> dict[str, Any]:
    adapter = connection_manager.get_connection(connection)
    return adapter.ping()


@_tool
def query_explain(connection: str, sql: str, analyze: bool = False) -> dict[str, Any]:
    adapter = connection_manager.get_connection(connection)
    if not adapter.supports(Capability.SQL):
        return {"status": "CAPABILITY_NOT_SUPPORTED", "capability": "sql"}
    if analyze:
        # EXPLAIN ANALYZE류는 기본 비활성 — 명시적 승인 플래그 필요 (§29)
        return {"status": "REQUIRES_EXPLICIT_APPROVAL"}
    _refresh_stale_tables(connection, adapter, _extract_table_names(sql))
    return to_serializable(adapter.explain(sql))


# ---------------------------------------------------------------------------
# Maintenance
# ---------------------------------------------------------------------------


@_tool
def db_error_analyze(connection: str, error: Optional[str] = None) -> dict[str, Any]:
    adapter = connection_manager.get_connection(connection)
    result = _db_error_analyze(adapter, error=error)
    tables: set[str] = set()
    for sql in result.get("running_sql") or []:
        tables.update(_extract_table_names(sql))
    _refresh_stale_tables(connection, adapter, tables)
    return result


@_tool
def slow_query_list(connection: str, min_duration: float, limit: int = 20) -> dict[str, Any]:
    adapter = connection_manager.get_connection(connection)
    result = _slow_query_list(adapter, min_duration=min_duration, limit=limit)
    tables: set[str] = set()
    for op in result.get("operations") or []:
        tables.update(_extract_table_names(op.get("text") or ""))
    _refresh_stale_tables(connection, adapter, tables)
    return result


@_tool
def get_locks(connection: str) -> dict[str, Any]:
    adapter = connection_manager.get_connection(connection)
    return _get_locks(adapter)


@_tool
def get_sessions(connection: str, status: str = "all") -> dict[str, Any]:
    adapter = connection_manager.get_connection(connection)
    return _get_sessions(adapter, status=status)


# ---------------------------------------------------------------------------
# Tuner
# ---------------------------------------------------------------------------


@_tool
def get_table_statistics(connection: str, table: str, schema: Optional[str] = None) -> dict[str, Any]:
    adapter = connection_manager.get_connection(connection)
    _refresh_stale_tables(connection, adapter, [table])
    return _get_table_statistics(adapter, table=table, schema=schema)


@_tool
def get_index_statistics(connection: str, table: str, schema: Optional[str] = None) -> dict[str, Any]:
    adapter = connection_manager.get_connection(connection)
    _refresh_stale_tables(connection, adapter, [table])
    return _get_index_statistics(adapter, table=table, schema=schema)


@_tool
def index_recommend(
    connection: str, target_table: str, sql: Optional[str] = None
) -> dict[str, Any]:
    adapter = connection_manager.get_connection(connection)
    tables = {target_table} | set(_extract_table_names(sql or ""))
    _refresh_stale_tables(connection, adapter, tables)
    return _index_recommend(adapter, target_table=target_table, sql=sql)


@_tool
def query_rewrite_suggest(connection: str, sql: str) -> dict[str, Any]:
    adapter = connection_manager.get_connection(connection)
    _refresh_stale_tables(connection, adapter, _extract_table_names(sql))
    return _query_rewrite_suggest(adapter, sql=sql)


# ---------------------------------------------------------------------------
# DataEngineer
# ---------------------------------------------------------------------------


@_tool
def list_raw_sources(connection: str, scope: str) -> dict[str, Any]:
    adapter = connection_manager.get_connection(connection)
    return _list_raw_sources(adapter, scope=scope)


@_tool
def get_schema(connection: str, scope: str) -> dict[str, Any]:
    adapter = connection_manager.get_connection(connection)
    return _get_schema(adapter, scope=scope)


@_tool
def profile_data(connection: str, table: str, sample_limit: int = 100) -> dict[str, Any]:
    adapter = connection_manager.get_connection(connection)
    _refresh_stale_tables(connection, adapter, [table])
    return _profile_data(adapter, container=table, sample_limit=sample_limit)


@_tool
def generate_pipeline_plan(
    source_connection: str,
    source: dict[str, str],
    target_connection: str,
    target: dict[str, str],
) -> dict[str, Any]:
    source_adapter = connection_manager.get_connection(source_connection)
    target_adapter = connection_manager.get_connection(target_connection)
    return _generate_pipeline_plan(source_adapter, source, target_adapter, target)


@_tool
def validate_pipeline(
    source_connection: str,
    target_connection: str,
    plan: dict[str, Any],
    sample_limit: int = 100,
) -> dict[str, Any]:
    source_adapter = connection_manager.get_connection(source_connection)
    target_adapter = connection_manager.get_connection(target_connection)
    return _validate_pipeline(source_adapter, target_adapter, plan, sample_limit)


# ---------------------------------------------------------------------------
# Ontology / ERD
# ---------------------------------------------------------------------------


@_tool
def build_ontology_index(connection: str, mode: str = "incremental") -> dict[str, Any]:
    adapter = connection_manager.get_connection(connection)
    return _build_ontology_index(connection, adapter, mode=mode)


@_tool
def search_object_catalog(connection: str, query: str, top_k: int = 5) -> dict[str, Any]:
    adapter = connection_manager.get_connection(connection)
    return _search_object_catalog(connection, query=query, adapter=adapter, top_k=top_k)


@_tool
def get_table_relationships(connection: str, scope: dict[str, Any]) -> dict[str, Any]:
    adapter = connection_manager.get_connection(connection)
    engine_family = AdapterRegistry.engine_family_of(adapter.connection_profile["db_type"])
    return _get_table_relationships(adapter, engine_family, scope)


# ---------------------------------------------------------------------------
# Learning (Q&A Logging / Pattern)
# ---------------------------------------------------------------------------


@_tool
def log_qa(
    connection: str,
    skill: str,
    question: str,
    tools_used: list[str],
    objects_referenced: list[str],
    answer_summary: str,
    duration_ms: float,
    sql_used: Optional[list[str]] = None,
) -> dict[str, Any]:
    # 조립은 즉시(빠르고 I/O 없음), 실제 디스크 쓰기는 컴퓨터가 한가하고
    # Claude 작업이 없을 때로 미룬다 — 그래서 이 Tool 호출은 파일을 직접
    # 쓰지 않고 Queue에 넣기만 한다(qa_log_writer, background_writer.py).
    entry = _build_qa_entry(
        connection,
        skill,
        question,
        tools_used,
        objects_referenced,
        answer_summary,
        duration_ms,
        sql_used,
    )
    qa_log_writer.enqueue(entry)
    return {"logged": True, "log_id": entry["log_id"], "queued": True}


@_tool
def summarize_query_patterns(
    since: Optional[str] = None, until: Optional[str] = None
) -> dict[str, Any]:
    return _summarize_query_patterns(since=since, until=until)  # type: ignore[arg-type]


@_tool
def get_query_patterns(
    connection: str, limit: int = 20, related_object: Optional[str] = None
) -> dict[str, Any]:
    return _get_query_patterns(connection, limit=limit, related_object=related_object)


# ---------------------------------------------------------------------------
# Query Builder (자연어 → 테이블/컬럼 유추 → 조회 실행, 학습된 패턴 우선 재사용)
# ---------------------------------------------------------------------------


@_tool
def find_query_pattern(
    connection: str, question: str, top_k: int = 5, min_score: float = 0.5
) -> dict[str, Any]:
    return _find_query_pattern(connection, question=question, top_k=top_k, min_score=min_score)


@_tool
def execute_readonly_query(
    connection: str, sql: str, limit: int = 100, mask: Optional[list[str]] = None
) -> dict[str, Any]:
    adapter = connection_manager.get_connection(connection)
    _refresh_stale_tables(connection, adapter, _extract_table_names(sql))
    return _execute_readonly_query(adapter, sql=sql, limit=limit, mask=mask)


def main() -> None:
    register_builtin_adapters()
    connection_manager.load()
    # dao.py의 atexit.register(obj_db.close)와 동일한 의도 — 프로세스 종료 시
    # 모든 Connection(SQL 연결/SSH Tunnel 포함)을 정리한다.
    atexit.register(connection_manager.close_all)
    # log_qa Queue를 비우는 백그라운드 스레드 시작 — flush_all()의
    # atexit 등록은 QALogWriter.start() 내부에서 함께 처리된다.
    qa_log_writer.start()
    mcp.run()


if __name__ == "__main__":
    main()
