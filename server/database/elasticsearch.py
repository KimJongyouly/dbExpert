"""Elasticsearch Adapter (Engine Family: search).

공통 REST 호출 로직은 `search_base.SearchAdapterBase`에 있다. 이 파일은
elasticsearch-py Client 생성만 담당한다 — OpenSearch에는 `opensearch.py`를
쓴다(Doc/01_개발정의서.md §11 — ES/OpenSearch 클라이언트 분리 결정 참고).
"""
from __future__ import annotations

from typing import Any

from server.database.registry import AdapterRegistry
from server.database.search_base import SearchAdapterBase


class ElasticsearchAdapter(SearchAdapterBase):
    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client

        from elasticsearch import Elasticsearch  # 지연 import (optional dependency)

        conn = self.connection_profile["connection"]
        auth = None
        if self.connection_profile.get("user"):
            auth = (self.connection_profile["user"], self.connection_profile.get("password"))

        self._client = Elasticsearch(
            [{"host": conn["host"], "port": conn["port"]}],
            basic_auth=auth,
            request_timeout=self.query_timeout_sec,
        )
        return self._client


AdapterRegistry.register("elasticsearch", ElasticsearchAdapter, engine_family="search")
