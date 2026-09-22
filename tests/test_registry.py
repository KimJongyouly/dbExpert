"""Adapter Registry / Capability 선언에 대한 스모크 테스트.

DB 연결 없이 확인 가능한 것만 검증한다: 8개 Engine이 모두 등록되는지,
Doc/00_개발요건사항.md §7.2 Capability 매트릭스와 Adapter의 capabilities
선언이 일치하는지.
"""
from __future__ import annotations

from server.database.base import Capability
from server.database.registry import AdapterRegistry, register_builtin_adapters

EXPECTED_ENGINE_FAMILY = {
    "mysql": "rdb",
    "postgresql": "rdb",
    "tibero": "rdb",
    "mssql": "rdb",
    "clickhouse": "dw",
    "mongodb": "document",
    "elasticsearch": "search",
    "opensearch": "search",
    "redis": "keyvalue",
}

# Doc/00_개발요건사항.md §7.2 Capability 매트릭스에서 "O"인 항목만 True.
EXPECTED_RELATIONSHIP_SUPPORT = {
    "mysql": True,
    "postgresql": True,
    "tibero": True,
    "mssql": True,
    "clickhouse": False,
    "mongodb": False,
    "elasticsearch": False,
    "opensearch": False,
    "redis": False,
}


def setup_module() -> None:
    register_builtin_adapters()


def test_all_engines_registered() -> None:
    registered = {row["db_type"] for row in AdapterRegistry.list_registered()}
    assert set(EXPECTED_ENGINE_FAMILY) <= registered


def test_engine_family_matches_doc() -> None:
    for db_type, expected_family in EXPECTED_ENGINE_FAMILY.items():
        assert AdapterRegistry.engine_family_of(db_type) == expected_family


def test_relationship_capability_matches_capability_matrix() -> None:
    for db_type, expected in EXPECTED_RELATIONSHIP_SUPPORT.items():
        adapter_class = AdapterRegistry.resolve(db_type)
        assert (Capability.RELATIONSHIP in adapter_class.capabilities) == expected


def test_every_adapter_declares_at_least_one_capability() -> None:
    for row in AdapterRegistry.list_registered():
        assert len(row["capabilities"]) > 0, row["db_type"]
