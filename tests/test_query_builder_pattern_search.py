"""find_query_pattern Tool 구현(query_builder/pattern_search.py)에 대한
단위 테스트.

실제 sentence-transformers 모델을 로드하지 않도록, 토큰 겹침 기반의
결정적(deterministic) fake embed 함수를 주입한다(tests/test_catalog.py와
동일한 패턴) — "공유 토큰이 많을수록 유사도가 높다"는 성질만 있으면
이 Tool의 랭킹/임계값 로직을 검증하기에 충분하다.
"""
from __future__ import annotations

import json
import re

from server.query_builder.pattern_search import find_query_pattern

_TOKEN_RE = re.compile(r"[A-Za-z0-9가-힣_]+")
_DIM = 32


def _fake_embed(texts: list[str]) -> list[list[float]]:
    import hashlib

    vectors = []
    for text in texts:
        vec = [0.0] * _DIM
        for token in _TOKEN_RE.findall(text.lower()):
            idx = int(hashlib.md5(token.encode("utf-8")).hexdigest(), 16) % _DIM
            vec[idx] += 1.0
        norm = sum(v * v for v in vec) ** 0.5
        if norm > 0:
            vec = [v / norm for v in vec]
        vectors.append(vec)
    return vectors


def _write_patterns(tmp_path, connection_name, patterns):
    conn_dir = tmp_path / connection_name
    conn_dir.mkdir(parents=True, exist_ok=True)
    (conn_dir / "patterns.json").write_text(
        json.dumps(patterns, ensure_ascii=False), encoding="utf-8"
    )


def test_returns_empty_matches_when_no_patterns_file_exists(tmp_path, monkeypatch):
    import server.query_builder.pattern_search as module

    monkeypatch.setattr(module, "DATA_DIR", tmp_path)

    result = find_query_pattern("no-such-conn", question="아무 질문", embed_fn=_fake_embed)
    assert result == {"matches": []}


def test_finds_semantically_similar_pattern_even_with_different_wording(tmp_path, monkeypatch):
    import server.query_builder.pattern_search as module

    monkeypatch.setattr(module, "DATA_DIR", tmp_path)
    _write_patterns(
        tmp_path,
        "prod-aurora",
        [
            {
                "connection": "prod-aurora",
                "pattern_id": "slow-query-orders",
                "representative_question": "최근 주문 고객 목록 보여줘",
                "frequency": 3,
                "last_seen": "2026-09-01T00:00:00+00:00",
                "related_objects": ["orders", "customers"],
                "representative_sql": "SELECT * FROM orders o JOIN customers c ON c.id = o.customer_id",
                "related_skill": "query-builder",
                "typical_recommendation": None,
            },
            {
                "connection": "prod-aurora",
                "pattern_id": "unrelated",
                "representative_question": "ClickHouse 디스크 사용량 알려줘",
                "frequency": 1,
                "last_seen": "2026-09-01T00:00:00+00:00",
                "related_objects": [],
                "representative_sql": None,
                "related_skill": "slow-query",
                "typical_recommendation": None,
            },
        ],
    )

    result = find_query_pattern(
        "prod-aurora", question="주문 고객 리스트 조회", embed_fn=_fake_embed, min_score=0.1
    )

    assert len(result["matches"]) >= 1
    top = result["matches"][0]
    assert top["pattern_id"] == "slow-query-orders"
    assert "score" in top
    assert top["representative_sql"].startswith("SELECT")


def test_min_score_filters_out_weak_matches(tmp_path, monkeypatch):
    import server.query_builder.pattern_search as module

    monkeypatch.setattr(module, "DATA_DIR", tmp_path)
    _write_patterns(
        tmp_path,
        "prod-aurora",
        [
            {
                "connection": "prod-aurora",
                "pattern_id": "totally-unrelated",
                "representative_question": "완전히 상관없는 다른 주제 질문",
                "frequency": 1,
                "last_seen": "2026-09-01T00:00:00+00:00",
                "related_objects": [],
                "representative_sql": "SELECT 1",
                "related_skill": "query-builder",
                "typical_recommendation": None,
            }
        ],
    )

    result = find_query_pattern(
        "prod-aurora", question="orders customers 조인 쿼리", embed_fn=_fake_embed, min_score=0.9
    )
    assert result["matches"] == []


def test_top_k_limits_number_of_matches(tmp_path, monkeypatch):
    import server.query_builder.pattern_search as module

    monkeypatch.setattr(module, "DATA_DIR", tmp_path)
    patterns = [
        {
            "connection": "prod-aurora",
            "pattern_id": f"p{i}",
            "representative_question": "주문 고객 목록",
            "frequency": 1,
            "last_seen": "2026-09-01T00:00:00+00:00",
            "related_objects": [],
            "representative_sql": "SELECT 1",
            "related_skill": "query-builder",
            "typical_recommendation": None,
        }
        for i in range(10)
    ]
    _write_patterns(tmp_path, "prod-aurora", patterns)

    result = find_query_pattern(
        "prod-aurora", question="주문 고객 목록", embed_fn=_fake_embed, top_k=3, min_score=0.0
    )
    assert len(result["matches"]) == 3
