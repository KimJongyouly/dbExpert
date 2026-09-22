"""Read-Only SQL 가드에 대한 단위 테스트. DB 연결이 전혀 필요 없다."""
from __future__ import annotations

import pytest

from server.database.errors import ReadOnlyViolationError
from server.database.sql_guard import assert_readonly_sql


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM orders",
        "  select id from orders where id = :id",
        "WITH t AS (SELECT 1) SELECT * FROM t",
        "SHOW TABLES",
        "EXPLAIN SELECT * FROM orders",
        "SELECT * FROM orders;",  # 끝의 세미콜론 하나는 허용
    ],
)
def test_allows_readonly_statements(sql: str) -> None:
    assert_readonly_sql(sql)  # 예외가 나지 않으면 성공


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO orders VALUES (1)",
        "UPDATE orders SET status = 'X'",
        "DELETE FROM orders",
        "DROP TABLE orders",
        "CREATE TABLE t (id INT)",
        "SELECT * FROM orders; DROP TABLE orders;",  # statement stacking
        "",
        "   ",
        # SELECT로 시작하지만 서버에 쓰기 부작용을 일으키는 구문 (관리자
        # 계정 사용 시 텍스트 검사가 유일한 방어선이 되는 케이스, §29 참고)
        "SELECT * FROM orders INTO OUTFILE '/tmp/x.csv'",  # MySQL 파일 쓰기
        "SELECT * FROM orders INTO DUMPFILE '/tmp/x'",
        "SELECT * INTO backup_orders FROM orders",  # MSSQL SELECT...INTO로 테이블 생성
        "SELECT dblink_exec('dbname=x', 'INSERT INTO t VALUES (1)')",  # 문자열 안에도 INSERT가 그대로 노출됨
    ],
)
def test_blocks_non_readonly_statements(sql: str) -> None:
    with pytest.raises(ReadOnlyViolationError):
        assert_readonly_sql(sql)


def test_blocks_forbidden_keyword_hidden_inside_with_clause() -> None:
    with pytest.raises(ReadOnlyViolationError):
        assert_readonly_sql(
            "WITH t AS (INSERT INTO orders VALUES (1) RETURNING id) SELECT * FROM t"
        )
