"""pipeline/*.py의 4개 Tool 구현에 대한 단위 테스트. Fake Adapter만 사용한다."""
from __future__ import annotations

from server.database.base import (
    BaseAdapter,
    Capability,
    ContainerStatistics,
    SampleCapable,
    SchemaCapable,
    SchemaField,
    SchemaObject,
    StatisticsCapable,
)
from server.pipeline.data_profiler import profile_data
from server.pipeline.pipeline_builder import generate_pipeline_plan
from server.pipeline.pipeline_validator import validate_pipeline
from server.pipeline.source_discovery import get_schema, list_raw_sources


class _FakeAdapter(BaseAdapter, SchemaCapable, StatisticsCapable, SampleCapable):
    capabilities = {Capability.SCHEMA, Capability.STATS, Capability.SAMPLE}

    def __init__(self, schema_objects, sample_rows_by_container=None, stats_by_container=None):
        super().__init__({"connection": {}})
        self._schema_objects = schema_objects
        self._sample_rows_by_container = sample_rows_by_container or {}
        self._stats_by_container = stats_by_container or {}

    def ping(self):
        return {"connected": True}

    def get_schema(self, scope):
        container = scope.get("container")
        if container:
            return [o for o in self._schema_objects if o.container == container]
        return self._schema_objects

    def get_table_statistics(self, namespace, container):
        return self._stats_by_container.get(container, ContainerStatistics())

    def get_index_statistics(self, namespace, container):
        return []

    def sample_rows(self, namespace, container, limit, mask=None):
        rows = self._sample_rows_by_container.get(container, [])
        return [dict(r) for r in rows[:limit]]


class _NoCapabilityAdapter(BaseAdapter):
    capabilities: set[Capability] = set()

    def ping(self):
        return {"connected": True}


# ---------------------------------------------------------------------------
# list_raw_sources
# ---------------------------------------------------------------------------


def test_list_raw_sources_capability_not_supported():
    result = list_raw_sources(_NoCapabilityAdapter({}), scope="schema:raw_landing")
    assert result["status"] == "CAPABILITY_NOT_SUPPORTED"


def test_list_raw_sources_falls_back_to_stats_when_row_count_missing():
    adapter = _FakeAdapter(
        schema_objects=[
            SchemaObject(container="orders_raw", row_count_estimate=None),
            SchemaObject(container="customers_raw", row_count_estimate=42),
        ],
        stats_by_container={"orders_raw": ContainerStatistics(row_count_estimate=999)},
    )
    result = list_raw_sources(adapter, scope="schema:raw_landing")
    by_name = {s["name"]: s for s in result["sources"]}
    assert by_name["orders_raw"]["row_count"] == 999
    assert by_name["customers_raw"]["row_count"] == 42


# ---------------------------------------------------------------------------
# get_schema
# ---------------------------------------------------------------------------


def test_get_schema_capability_not_supported():
    result = get_schema(_NoCapabilityAdapter({}), scope="schema:raw_landing")
    assert result["status"] == "CAPABILITY_NOT_SUPPORTED"


def test_get_schema_returns_all_objects_with_fields():
    adapter = _FakeAdapter(
        schema_objects=[
            SchemaObject(
                container="orders_raw",
                container_comment="주문 원본",
                fields=[SchemaField(name="id", type="bigint", is_pk=True)],
            ),
        ]
    )
    result = get_schema(adapter, scope="schema:raw_landing")
    assert len(result["objects"]) == 1
    obj = result["objects"][0]
    assert obj["container"] == "orders_raw"
    assert obj["container_comment"] == "주문 원본"
    assert obj["fields"][0]["name"] == "id"


# ---------------------------------------------------------------------------
# profile_data
# ---------------------------------------------------------------------------


def test_profile_data_capability_not_supported():
    result = profile_data(_NoCapabilityAdapter({}), container="orders_raw")
    assert result["status"] == "CAPABILITY_NOT_SUPPORTED"


def test_profile_data_computes_null_ratio_cardinality_and_masks_sensitive_columns():
    adapter = _FakeAdapter(
        schema_objects=[],
        sample_rows_by_container={
            "orders_raw": [
                {"id": 1, "email": "a@x.com", "amount": 100},
                {"id": 2, "email": "b@x.com", "amount": None},
            ]
        },
    )
    result = profile_data(adapter, container="orders_raw")
    ratios = {r["column"]: r["ratio"] for r in result["null_ratio"]}
    assert ratios["amount"] == 0.5
    assert ratios["id"] == 0.0
    assert all(row["email"] == "***" for row in result["sample_rows"])


# ---------------------------------------------------------------------------
# generate_pipeline_plan / validate_pipeline (연결해서 검증)
# ---------------------------------------------------------------------------


def _source_and_target_adapters():
    source = _FakeAdapter(
        schema_objects=[
            SchemaObject(
                container="orders_raw",
                fields=[
                    SchemaField(name="id", type="bigint"),
                    SchemaField(name="customer_id", type="bigint"),
                    SchemaField(name="amount", type="varchar"),
                    SchemaField(name="updated_at", type="datetime"),
                    SchemaField(name="legacy_flag", type="boolean"),
                ],
            )
        ],
        sample_rows_by_container={
            "orders_raw": [
                {"id": 1, "customer_id": 10, "amount": "100.5", "updated_at": "2026-01-01", "legacy_flag": False},
                {"id": None, "customer_id": 11, "amount": "abc", "updated_at": "2026-01-02", "legacy_flag": True},
            ]
        },
        stats_by_container={"orders_raw": ContainerStatistics(row_count_estimate=1000)},
    )
    target = _FakeAdapter(
        schema_objects=[
            SchemaObject(
                container="fact_orders",
                fields=[
                    SchemaField(name="id", type="bigint", is_pk=True),
                    SchemaField(name="customer_id", type="bigint"),
                    SchemaField(name="amount", type="decimal"),
                ],
            )
        ]
    )
    return source, target


def test_generate_pipeline_plan_maps_columns_and_detects_incremental():
    source, target = _source_and_target_adapters()
    plan = generate_pipeline_plan(source, {"name": "orders_raw"}, target, {"name": "fact_orders"})

    mapped = {m["source_column"]: m for m in plan["column_mapping"]}
    assert set(mapped) == {"id", "customer_id", "amount"}
    assert mapped["amount"]["transform"] == "CAST(amount AS decimal)"
    assert mapped["id"]["transform"] is None
    assert set(plan["unmapped_columns"]) == {"updated_at", "legacy_flag"}
    assert plan["load_mode"] == "incremental"
    assert plan["incremental_column"] == "updated_at"


def test_validate_pipeline_detects_constraint_violation_and_cast_failure():
    source, target = _source_and_target_adapters()
    plan = generate_pipeline_plan(source, {"name": "orders_raw"}, target, {"name": "fact_orders"})

    result = validate_pipeline(source, target, plan)

    assert {"column": "id", "constraint": "NOT NULL (PK)", "sample_value": None} in result[
        "constraint_violations"
    ]
    cast_failures = {c["column"]: c["count"] for c in result["cast_failures"]}
    assert cast_failures["amount"] == 1
    assert result["predicted_target_row_count"] == 1000


def test_validate_pipeline_requires_source_container_in_plan():
    source, target = _source_and_target_adapters()
    result = validate_pipeline(source, target, plan={})
    assert result["status"] == "INVALID_PLAN"
