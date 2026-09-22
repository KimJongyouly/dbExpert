"""find_query_pattern Tool 구현 (`query-builder` Skill).

자연어 질문을 이미 학습된 QueryPattern
(`server/learning/pattern_summarizer.py`가 쌓아온
`data/qa_logs/<connection>/patterns.json`)과 임베딩 기반 유사도로 비교해,
비슷한 질문에 답한 적이 있으면 그때 쓴 SQL을 먼저 찾아준다 — "다음엔 더
빠르게 답한다"의 핵심 경로다.

`get_query_patterns()`의 `related_object` 정확 일치 검색과 달리, 표현이
달라도(동의어 등) 찾을 수 있도록 Ontology 검색과 동일한 임베딩
(`embeddings.embed()`)을 재사용한다. 패턴 개수가 Connection당 많아야
수백 건 수준이라는 전제로, Ontology(Table 수백~수천 개)처럼 별도 Vector
Index를 새로 만들어 유지하는 대신 호출 시점에 그때그때 임베딩하는 단순한
방식을 쓴다 — 매번 재계산해도 부담되지 않는 규모다.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Optional

from server.catalog.embeddings import embed as _default_embed

DATA_DIR = Path("data/qa_logs")
DEFAULT_MIN_SCORE = 0.5

EmbedFn = Callable[[list[str]], list[list[float]]]


def find_query_pattern(
    connection_name: str,
    question: str,
    top_k: int = 5,
    min_score: float = DEFAULT_MIN_SCORE,
    embed_fn: Optional[EmbedFn] = None,
) -> dict[str, Any]:
    """이미 학습된 QueryPattern 중 이 질문과 의미상 비슷한 것을 찾는다.

    Returns: { "matches": (QueryPattern & {"score": float})[] }
    """
    patterns = _load_patterns(connection_name)
    if not patterns:
        return {"matches": []}

    embed = embed_fn or _default_embed
    texts = [p["representative_question"] for p in patterns] + [question]
    vectors = embed(texts)
    query_vector = vectors[-1]
    pattern_vectors = vectors[:-1]

    scored = [
        (_cosine_similarity(query_vector, vector), pattern)
        for vector, pattern in zip(pattern_vectors, patterns)
    ]
    scored.sort(key=lambda item: item[0], reverse=True)

    matches = [
        {**pattern, "score": round(score, 4)}
        for score, pattern in scored[:top_k]
        if score >= min_score
    ]
    return {"matches": matches}


def _load_patterns(connection_name: str) -> list[dict[str, Any]]:
    patterns_path = DATA_DIR / connection_name / "patterns.json"
    if not patterns_path.exists():
        return []
    return json.loads(patterns_path.read_text(encoding="utf-8"))


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    # embeddings.embed()가 이미 L2-정규화된 벡터를 반환하므로 내적이 곧 cosine 유사도다.
    return sum(x * y for x, y in zip(a, b))
