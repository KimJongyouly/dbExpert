"""main.py의 Tool들이 실제로 refresh_stale_tables()를 호출하는지에 대한
배선(wiring) 테스트 — auto_refresh 자체의 동작(당일 1회 규칙 등)은
tests/test_auto_refresh.py에서 이미 검증했으므로, 여기서는 "어떤 Tool이
어떤 Table 이름으로 호출하는지"만 가로채서 확인한다.
"""
from __future__ import annotations

import server.main as main_module


def test_query_explain_triggers_refresh_with_tables_from_sql(monkeypatch):
    calls: list[tuple[str, set]] = []
    monkeypatch.setattr(
        main_module,
        "_refresh_stale_tables",
        lambda conn, adapter, tables: calls.append((conn, set(tables))),
    )

    class FakeAdapter:
        def supports(self, capability):
            return True

        def explain(self, sql):
            return {"raw": {}}

    monkeypatch.setattr(
        main_module.connection_manager, "get_connection", lambda name: FakeAdapter()
    )

    main_module.query_explain(
        connection="prod-aurora",
        sql="SELECT * FROM orders o JOIN customers c ON c.id = o.customer_id",
    )

    assert calls == [("prod-aurora", {"orders", "customers"})]


def test_get_table_statistics_triggers_refresh_with_the_given_table(monkeypatch):
    calls: list[tuple[str, set]] = []
    monkeypatch.setattr(
        main_module,
        "_refresh_stale_tables",
        lambda conn, adapter, tables: calls.append((conn, set(tables))),
    )
    monkeypatch.setattr(
        main_module, "_get_table_statistics", lambda adapter, table, schema=None: {}
    )
    monkeypatch.setattr(
        main_module.connection_manager, "get_connection", lambda name: object()
    )

    main_module.get_table_statistics(connection="prod-aurora", table="orders")

    assert calls == [("prod-aurora", {"orders"})]


def test_index_recommend_triggers_refresh_with_target_table_and_sql_tables(monkeypatch):
    calls: list[tuple[str, set]] = []
    monkeypatch.setattr(
        main_module,
        "_refresh_stale_tables",
        lambda conn, adapter, tables: calls.append((conn, set(tables))),
    )
    monkeypatch.setattr(
        main_module,
        "_index_recommend",
        lambda adapter, target_table, sql=None: {},
    )
    monkeypatch.setattr(
        main_module.connection_manager, "get_connection", lambda name: object()
    )

    main_module.index_recommend(
        connection="prod-aurora",
        target_table="orders",
        sql="SELECT * FROM orders JOIN customers ON customers.id = orders.customer_id",
    )

    assert calls == [("prod-aurora", {"orders", "customers"})]
