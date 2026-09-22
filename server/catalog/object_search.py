"""search_object_catalog Tool 구현.

Tool 시그니처: Doc/01_개발정의서.md §3.5.
"""
from __future__ import annotations

from typing import Any, Optional

from server.catalog.ontology_builder import build_ontology_index
from server.catalog.rag_index import RagIndex
from server.database.base import BaseAdapter


def search_object_catalog(
    connection_name: str,
    query: str,
    adapter: Optional[BaseAdapter] = None,
    top_k: int = 5,
) -> dict[str, Any]:
    """자연어 query로 Ontology Index를 검색해 Table/Column 후보를 반환한다.

    이 Connection의 Vector Index가 아직 한 번도 채워지지 않았으면(최초
    사용, Daily 배치가 아직 안 돈 경우 등) 검색해봐야 항상 빈 결과만
    나오므로, 여기서 한 번 전체 스캔(`build_ontology_index`)으로 채운 뒤
    검색한다 — 사용자 요청: "object가 없다면 RAG를 채우고 그걸 바탕으로
    쿼리해달라". `adapter`가 없으면(호출자가 안 넘겼거나 Connection 해석에
    실패한 경우) 이 자동 채움은 건너뛰고 기존 인덱스로만 검색한다.

    여러 후보의 score가 근접하면(모호함), Claude가 matches 목록의 score
    차이를 보고 사용자에게 재확인하도록 유도한다 — 모호성 판정 자체를
    이 함수에서 미리 하지 않는다(Doc/00_개발요건사항.md §10.3).

    Returns: { "matches": [...], "index_was_empty_and_rebuilt": bool }
    """
    index = RagIndex(connection_name)

    index_was_empty_and_rebuilt = False
    if adapter is not None and index.is_empty():
        try:
            build_ontology_index(connection_name, adapter)
            index_was_empty_and_rebuilt = True
        except Exception:  # noqa: BLE001 — 채우기 실패해도 검색 자체는 시도한다
            pass

    matches = index.search(query, top_k)
    return {
        "matches": [
            {
                "schema": doc.get("namespace"),
                "table": doc.get("container"),
                "table_comment": doc.get("container_comment"),
                "score": doc.get("score"),
                "columns": [
                    {"name": f["name"], "comment": f.get("comment")}
                    for f in doc.get("fields", [])
                ],
            }
            for doc in matches
        ],
        "index_was_empty_and_rebuilt": index_was_empty_and_rebuilt,
    }
