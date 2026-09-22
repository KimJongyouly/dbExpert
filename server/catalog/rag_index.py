"""Ontology Vector Store — LanceDB 기반 구현.

Doc/01_개발정의서.md §11에서 미결정이던 Vector Store를 LanceDB(임베디드,
로컬 파일 기반 — 별도 서버/인프라 없이 동작)로 확정한다. Embedding
Provider는 server/catalog/embeddings.py 참고(sentence-transformers 로컬
실행).

이전(1차) 구현은 토큰 overlap 기반 어휘 검색이었다 — 공개 인터페이스
(upsert/delete/search/all_ids)는 그대로 유지했으므로 ontology_builder.py,
object_search.py는 이 교체로 인해 코드를 바꿀 필요가 없다.

Vector Store는 Server(connection)별로 분리 저장한다:
data/ontology_index/<connection>/lancedb/
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Optional

from server.catalog.embeddings import EMBEDDING_DIM, embed

DATA_DIR = Path("data/ontology_index")

EmbedFn = Callable[[list[str]], list[list[float]]]

_TABLE_NAME = "documents"


def _pyarrow_schema():
    import pyarrow as pa  # 지연 import — lancedb와 함께 설치되는 optional dependency

    return pa.schema(
        [
            pa.field("id", pa.string()),
            pa.field("connection", pa.string()),
            pa.field("namespace", pa.string()),
            pa.field("container", pa.string()),
            pa.field("container_comment", pa.string()),
            pa.field("row_count_estimate", pa.int64()),
            pa.field("fields_json", pa.string()),  # SchemaField 목록을 JSON 문자열로 보관
            pa.field("embedding_text", pa.string()),
            pa.field("source_hash", pa.string()),
            pa.field("last_indexed_at", pa.string()),
            pa.field("vector", pa.list_(pa.float32(), EMBEDDING_DIM)),
        ]
    )


def _sql_quote(value: str) -> str:
    """LanceDB delete()/filter는 SQL-like 문자열이라 홑따옴표만 이스케이프한다.

    id는 우리가 "{connection}:{namespace}:{container}" 형태로 만든 내부
    문자열이지만, 드물게 스키마/테이블명에 홑따옴표가 섞인 환경도 있을 수
    있어 방어적으로 처리한다.
    """
    return "'" + value.replace("'", "''") + "'"


def _list_table_names(db) -> set[str]:
    """lancedb 버전에 따라 `table_names()`(구버전, 단순 list[str])와
    `list_tables()`(신버전, ListTablesResponse.tables)가 갈려 있어 흡수한다."""
    try:
        return set(db.table_names())
    except AttributeError:
        response = db.list_tables()
        return set(getattr(response, "tables", response))


class RagIndex:
    def __init__(self, connection_name: str, embed_fn: Optional[EmbedFn] = None):
        self.connection_name = connection_name
        self.db_path = DATA_DIR / connection_name / "lancedb"
        # 기본은 실제 Embedding Provider(embeddings.embed). 테스트에서는
        # 무거운 모델을 로드하지 않도록 결정적인 fake 함수를 주입할 수 있다.
        self._embed_fn: EmbedFn = embed_fn or embed

    def _connect(self):
        import lancedb  # 지연 import — optional dependency (Doc/00 §28 ontology extras)

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        return lancedb.connect(str(self.db_path))

    def _open_table(self, create_if_missing: bool):
        db = self._connect()
        if _TABLE_NAME in _list_table_names(db):
            return db.open_table(_TABLE_NAME)
        if create_if_missing:
            return db.create_table(_TABLE_NAME, schema=_pyarrow_schema())
        return None

    def upsert(self, documents: list[dict[str, Any]]) -> int:
        """OntologyDocument 목록을 반영한다.

        Returns: source_hash가 바뀌었거나 새로 생긴 문서 수(신규+갱신).
        """
        if not documents:
            return 0

        existing_table = self._open_table(create_if_missing=False)
        existing_hashes: dict[str, str] = {}
        if existing_table is not None and existing_table.count_rows() > 0:
            df = existing_table.to_pandas()
            existing_hashes = dict(zip(df["id"], df["source_hash"]))

        changed = sum(
            1 for doc in documents if existing_hashes.get(doc["id"]) != doc.get("source_hash")
        )

        vectors = self._embed_fn([doc["embedding_text"] for doc in documents])
        records = [
            {
                "id": doc["id"],
                "connection": doc.get("connection", self.connection_name),
                "namespace": doc.get("namespace"),
                "container": doc["container"],
                "container_comment": doc.get("container_comment"),
                "row_count_estimate": doc.get("row_count_estimate"),
                "fields_json": json.dumps(doc.get("fields", []), ensure_ascii=False),
                "embedding_text": doc["embedding_text"],
                "source_hash": doc.get("source_hash", ""),
                "last_indexed_at": doc.get("last_indexed_at", ""),
                "vector": vector,
            }
            for doc, vector in zip(documents, vectors)
        ]

        table = self._open_table(create_if_missing=True)
        ids_sql = ", ".join(_sql_quote(r["id"]) for r in records)
        table.delete(f"id IN ({ids_sql})")  # Upsert = 기존 것 지우고 새로 추가
        table.add(records)
        return changed

    def delete(self, document_ids: list[str]) -> int:
        if not document_ids:
            return 0
        table = self._open_table(create_if_missing=False)
        if table is None or table.count_rows() == 0:
            return 0

        existing_ids = set(table.to_pandas()["id"])
        to_remove = [doc_id for doc_id in document_ids if doc_id in existing_ids]
        if not to_remove:
            return 0

        ids_sql = ", ".join(_sql_quote(doc_id) for doc_id in to_remove)
        table.delete(f"id IN ({ids_sql})")
        return len(to_remove)

    def all_ids(self) -> set[str]:
        table = self._open_table(create_if_missing=False)
        if table is None or table.count_rows() == 0:
            return set()
        return set(table.to_pandas()["id"])

    def is_empty(self) -> bool:
        """이 Connection의 Vector Index에 문서가 하나도 없는지 확인한다.

        `all_ids()`처럼 `to_pandas()`로 전체를 끌어오지 않고 `count_rows()`
        (메타데이터 조회)만 쓴다 — search_object_catalog가 검색할 때마다
        "채워야 하나?"를 가볍게 확인하기 위한 용도다.
        """
        table = self._open_table(create_if_missing=False)
        return table is None or table.count_rows() == 0

    def search(self, query: str, top_k: int = 5) -> list[dict[str, Any]]:
        """query와 유사한 OntologyDocument를 유사도(score) 순으로 반환한다."""
        table = self._open_table(create_if_missing=False)
        if table is None or table.count_rows() == 0:
            return []

        query_vector = self._embed_fn([query])[0]
        rows = table.search(query_vector).metric("cosine").limit(top_k).to_list()

        results = []
        for row in rows:
            distance = row.get("_distance", 0.0)  # cosine distance = 1 - cosine similarity
            score = max(0.0, min(1.0, 1.0 - distance))
            results.append(
                {
                    "id": row["id"],
                    "connection": row.get("connection"),
                    "namespace": row.get("namespace"),
                    "container": row.get("container"),
                    "container_comment": row.get("container_comment"),
                    "row_count_estimate": row.get("row_count_estimate"),
                    "fields": json.loads(row.get("fields_json") or "[]"),
                    "embedding_text": row.get("embedding_text"),
                    "source_hash": row.get("source_hash"),
                    "last_indexed_at": row.get("last_indexed_at"),
                    "score": round(score, 4),
                }
            )
        return results
