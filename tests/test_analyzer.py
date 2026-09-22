"""analyzer/*.py의 4개 Tool 구현에 대한 단위 테스트.

실제 DB 대신 Capability 메서드가 미리 정해둔 값을 반환하는 Fake Adapter를
써서, "Adapter가 반환한 사실을 Tool 출력 형태로 올바르게 구조화하는지"만
검증한다(판단 문장 생성은 Claude의 몫이라 여기서 검증할 대상이 아니다).
"""
from __future__ import annotations

from server.analyzer.error import db_error_analyze
from server.analyzer.index_advisor import index_recommend
from server.analyzer.query_rewriter import query_rewrite_suggest
from server.analyzer.slow_query import slow_query_list
from server.database.base import (
    ActivityCapable,
    BaseAdapter,
    Capability,
    DBError,
    ErrorLogCapable,
    ExplainPlan,
    IndexStatistics,
    Lock,
    Session,
    SlowOperation,
    SlowOperationCapable,
    SqlCapable,
    StatisticsCapable,
)


class _FakeFullAdapter(
    BaseAdapter,
    SqlCapable,
    SlowOperationCapable,
    ActivityCapable,
    ErrorLogCapable,
    StatisticsCapable,
):
    capabilities = {
        Capability.SQL,
        Capability.SLOW_OP,
        Capability.ACTIVITY,
        Capability.ERROR_LOG,
        Capability.STATS,
    }

    def ping(self):
        return {"connected": True}

    def execute_readonly(self, sql, params=None):
        return []

    def explain(self, sql):
        return ExplainPlan(raw={"plan": "fake"}, scan_type="ALL", estimated_cost=12.5, estimated_rows=100)

    def get_slow_operations(self, min_duration_sec, limit):
        return [
            SlowOperation(
                operation_id="1",
                text="SELECT * FROM orders",
                execution_count=5,
                avg_duration_ms=120.0,
                max_duration_ms=300.0,
            )
        ][:limit]

    def get_locks(self):
        return [Lock(holding_session_id="10", resource="orders", lock_type="X")]

    def get_sessions(self):
        return [
            Session(
                session_id="10",
                state="running",
                current_operation="SELECT * FROM orders WHERE id = 1",
            )
        ]

    def get_errors(self, since=None):
        return [
            DBError(
                error_code="1213",
                message="Deadlock found when trying to get lock",
                occurred_at=None,
            )
        ]

    def get_table_statistics(self, namespace, container):
        raise NotImplementedError

    def get_index_statistics(self, namespace, container):
        return [
            IndexStatistics(index_name="PRIMARY", columns=["id"], is_unique=True, usage_count=100),
            IndexStatistics(index_name="idx_status", columns=["status"], is_unique=False, usage_count=0),
        ]


class _FakeNoCapabilityAdapter(BaseAdapter):
    capabilities: set[Capability] = set()

    def ping(self):
        return {"connected": True}


def _adapter() -> _FakeFullAdapter:
    return _FakeFullAdapter({"connection": {}})


# ---------------------------------------------------------------------------
# db_error_analyze
# ---------------------------------------------------------------------------


def test_db_error_analyze_capability_not_supported():
    result = db_error_analyze(_FakeNoCapabilityAdapter({}))
    assert result["status"] == "CAPABILITY_NOT_SUPPORTED"


def test_db_error_analyze_matches_given_error_message():
    result = db_error_analyze(_adapter(), error="Deadlock found when trying to get lock")
    assert result["error_code"] == "1213"
    assert result["error_message"] == "Deadlock found when trying to get lock"
    assert len(result["related_sessions"]) == 1
    assert result["related_sessions"][0]["session_id"] == "10"
    assert len(result["related_locks"]) == 1
    assert result["running_sql"] == ["SELECT * FROM orders WHERE id = 1"]
    assert len(result["recent_slow_queries"]) == 1


def test_db_error_analyze_defaults_to_latest_error_without_message():
    result = db_error_analyze(_adapter())
    assert result["error_code"] == "1213"
    assert result["error_message"] == "Deadlock found when trying to get lock"


# ---------------------------------------------------------------------------
# slow_query_list
# ---------------------------------------------------------------------------


def test_slow_query_list_capability_not_supported():
    result = slow_query_list(_FakeNoCapabilityAdapter({}), min_duration=5)
    assert result["status"] == "CAPABILITY_NOT_SUPPORTED"


def test_slow_query_list_serializes_operations():
    result = slow_query_list(_adapter(), min_duration=1, limit=500)
    assert len(result["operations"]) == 1
    op = result["operations"][0]
    assert op["operation_id"] == "1"
    assert op["avg_duration_ms"] == 120.0


# ---------------------------------------------------------------------------
# index_recommend
# ---------------------------------------------------------------------------


def test_index_recommend_capability_not_supported():
    result = index_recommend(_FakeNoCapabilityAdapter({}), target_table="orders")
    assert result["status"] == "CAPABILITY_NOT_SUPPORTED"


def test_index_recommend_detects_missing_index_from_where_clause():
    result = index_recommend(
        _adapter(),
        target_table="orders",
        sql="SELECT * FROM orders WHERE customer_id = 1 ORDER BY created_at DESC",
    )
    missing_columns = {c["columns"][0] for c in result["missing_index_candidates"]}
    assert "customer_id" in missing_columns
    assert "created_at" in missing_columns
    assert "id" not in missing_columns  # PRIMARY 인덱스가 이미 커버
    assert "CREATE INDEX" in result["ddl_suggestion"]
    assert result["explain_plan"]["scan_type"] == "ALL"


def test_index_recommend_flags_unused_index():
    result = index_recommend(_adapter(), target_table="orders")
    assert "idx_status" in result["unused_index_candidates"]
    assert "PRIMARY" not in result["unused_index_candidates"]


def test_index_recommend_without_sql_skips_explain():
    result = index_recommend(_adapter(), target_table="orders")
    assert result["explain_plan"] is None
    assert result["missing_index_candidates"] == []


# ---------------------------------------------------------------------------
# query_rewrite_suggest
# ---------------------------------------------------------------------------


def test_query_rewrite_suggest_capability_not_supported():
    result = query_rewrite_suggest(_FakeNoCapabilityAdapter({}), sql="SELECT 1")
    assert result["status"] == "CAPABILITY_NOT_SUPPORTED"


def test_query_rewrite_suggest_detects_select_star_and_or():
    result = query_rewrite_suggest(
        _adapter(), sql="SELECT * FROM orders WHERE a = 1 OR b = 2"
    )
    patterns = {issue["pattern"] for issue in result["issues"]}
    assert "select_star" in patterns
    assert "or_condition" in patterns
    assert result["explain_plan"]["estimated_cost"] == 12.5
    assert result["suggested_sql"] == ""


def test_query_rewrite_suggest_detects_function_on_column():
    result = query_rewrite_suggest(
        _adapter(), sql="SELECT id FROM orders WHERE DATE(created_at) = '2026-09-09'"
    )
    patterns = {issue["pattern"] for issue in result["issues"]}
    assert "function_on_column" in patterns
