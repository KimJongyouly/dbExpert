"""sql_base.py의 식별자 검증/Schema 그룹핑 유틸에 대한 단위 테스트."""
from __future__ import annotations

import pytest

from server.database.sql_base import rows_to_schema_objects, validate_identifier


@pytest.mark.parametrize("name", ["orders", "tb_complex_master", "_hidden", "a1"])
def test_validate_identifier_accepts_normal_names(name: str) -> None:
    assert validate_identifier(name) == name


@pytest.mark.parametrize(
    "name", ["orders; DROP TABLE x", "orders--", "1orders", "orders table", ""]
)
def test_validate_identifier_rejects_unsafe_names(name: str) -> None:
    with pytest.raises(ValueError):
        validate_identifier(name)


def test_rows_to_schema_objects_groups_by_table() -> None:
    rows = [
        {
            "table_schema": "public",
            "table_name": "orders",
            "table_comment": "주문",
            "row_count_estimate": 10,
            "column_name": "id",
            "data_type": "bigint",
            "column_comment": None,
            "is_pk": True,
        },
        {
            "table_schema": "public",
            "table_name": "orders",
            "table_comment": "주문",
            "row_count_estimate": 10,
            "column_name": "customer_id",
            "data_type": "bigint",
            "column_comment": "고객 ID",
            "is_pk": False,
        },
    ]
    objects = rows_to_schema_objects(rows)
    assert len(objects) == 1
    obj = objects[0]
    assert obj.container == "orders"
    assert obj.container_comment == "주문"
    assert [f.name for f in obj.fields] == ["id", "customer_id"]
    assert obj.fields[0].is_pk is True
    assert obj.fields[1].comment == "고객 ID"
