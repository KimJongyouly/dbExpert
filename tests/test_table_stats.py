"""get_table_statistics / get_index_statistics Tool 구현(table_stats.py)에
대한 단위 테스트."""
from __future__ import annotations

from server.analyzer.table_stats import get_index_statistics, get_table_statistics
from server.database.base import (
    BaseAdapter,
    Capability,
    ContainerStatistics,
    IndexStatistics,
    StatisticsCapable,
)


class _FakeAdapter(BaseAdapter, StatisticsCapable):
    capabilities = {Capability.STATS}

    def __init__(self, stats=None, indexes=None):
        super().__init__({"connection": {}})
        self._stats = stats or ContainerStatistics()
        self._indexes = indexes or []

    def ping(self):
        return {"connected": True}

    def get_table_statistics(self, namespace, container):
        assert container == "orders"
        return self._stats

    def get_index_statistics(self, namespace, container):
        assert container == "orders"
        return self._indexes


class _NoCapabilityAdapter(BaseAdapter):
    capabilities: set[Capability] = set()

    def ping(self):
        return {"connected": True}


def test_get_table_statistics_capability_not_supported():
    result = get_table_statistics(_NoCapabilityAdapter({}), table="orders")
    assert result["status"] == "CAPABILITY_NOT_SUPPORTED"


def test_get_table_statistics_returns_serialized_stats():
    adapter = _FakeAdapter(stats=ContainerStatistics(row_count_estimate=1000, size_bytes=2048))
    result = get_table_statistics(adapter, table="orders", schema="appdb")
    assert result["row_count_estimate"] == 1000
    assert result["size_bytes"] == 2048


def test_get_index_statistics_capability_not_supported():
    result = get_index_statistics(_NoCapabilityAdapter({}), table="orders")
    assert result["status"] == "CAPABILITY_NOT_SUPPORTED"


def test_get_index_statistics_returns_serialized_indexes():
    adapter = _FakeAdapter(
        indexes=[IndexStatistics(index_name="PRIMARY", columns=["id"], is_unique=True)]
    )
    result = get_index_statistics(adapter, table="orders")
    assert result["indexes"][0]["index_name"] == "PRIMARY"
    assert result["indexes"][0]["is_unique"] is True
