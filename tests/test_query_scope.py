"""SQL에서 FROM/JOIN Table명 추출(query_scope.py)에 대한 단위 테스트."""
from __future__ import annotations

from server.catalog.query_scope import extract_table_names


def test_extracts_single_table_from_simple_select():
    assert extract_table_names("SELECT * FROM orders WHERE id = 1") == ["orders"]


def test_extracts_multiple_tables_from_joins():
    sql = """
        SELECT o.id, c.name
        FROM orders o
        JOIN customers c ON c.id = o.customer_id
        LEFT JOIN warehouses w ON w.id = o.warehouse_id
    """
    assert extract_table_names(sql) == ["orders", "customers", "warehouses"]


def test_strips_schema_prefix():
    assert extract_table_names("SELECT * FROM appdb.orders") == ["orders"]


def test_strips_quoting_characters():
    assert extract_table_names("SELECT * FROM `orders`") == ["orders"]
    assert extract_table_names('SELECT * FROM "orders"') == ["orders"]
    assert extract_table_names("SELECT * FROM [orders]") == ["orders"]


def test_dedupes_repeated_table_references():
    sql = "SELECT * FROM orders o1 JOIN orders o2 ON o1.parent_id = o2.id"
    assert extract_table_names(sql) == ["orders"]


def test_empty_or_no_from_returns_empty_list():
    assert extract_table_names("") == []
    assert extract_table_names("SELECT 1") == []


def test_case_insensitive_keywords():
    assert extract_table_names("select * from Orders") == ["Orders"]
