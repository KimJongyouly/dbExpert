"""OpenSearch Adapter (Engine Family: search).

공통 REST 호출 로직은 `search_base.SearchAdapterBase`에 있다. 이 파일은
opensearch-py Client 생성만 담당한다. AWS OpenSearch Service/ElastiCache류
OpenSearch 서버는 반드시 이 Adapter로 붙인다 — elasticsearch-py 8.x는
Elastic 제품인지 확인하는 product-check를 수행해 OpenSearch에 붙이면
거부/경고가 발생할 수 있다(Doc/01_개발정의서.md §11 참고).
"""
from __future__ import annotations

from typing import Any

from server.database.registry import AdapterRegistry
from server.database.search_base import SearchAdapterBase


class OpenSearchAdapter(SearchAdapterBase):
    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client

        from opensearchpy import OpenSearch  # 지연 import (optional dependency)

        conn = self.connection_profile["connection"]
        auth = None
        if self.connection_profile.get("user"):
            auth = (self.connection_profile["user"], self.connection_profile.get("password"))

        # opensearch-py는 connect/request timeout을 따로 나눈 생성자 인자가
        # 없어, query_timeout_sec를 요청 전체(연결+응답 대기) 제한으로 쓴다.
        self._client = OpenSearch(
            hosts=[{"host": conn["host"], "port": conn["port"]}],
            http_auth=auth,
            timeout=self.query_timeout_sec,
            use_ssl=conn.get("use_ssl", False),
        )
        return self._client


AdapterRegistry.register("opensearch", OpenSearchAdapter, engine_family="search")
