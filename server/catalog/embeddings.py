"""Ontology 검색용 Embedding Provider.

Doc/01_개발정의서.md §11에서 미결정이던 Embedding Provider를 확정한다 —
sentence-transformers 다국어 모델을 로컬에서 직접 실행한다(외부 API 호출
없음). Table/Column comment가 한글 위주라 다국어(한/영) 지원 모델을 쓴다.

모델: paraphrase-multilingual-MiniLM-L12-v2 (출력 차원 384)
  - server/catalog/rag_index.py의 LanceDB 스키마(vector 컬럼 차원)가 이
    값(EMBEDDING_DIM)에 고정돼 있다 — 모델을 바꾸면 기존
    data/ontology_index/<connection>/lancedb/ 를 지우고 재구축해야 한다.
  - 최초 실행 시 HuggingFace Hub에서 모델을 1회 다운로드한다(약 460MB).
    `scripts/download_embedding_model.py`로 미리 받아둘 수 있고, 폐쇄망이면
    환경변수 DBEXPERT_EMBEDDING_MODEL에 로컬 디렉터리 경로를 주면 된다
    (Doc/02_설치.md §4 참고).
  - 다른 모델로 바꾸려면 DBEXPERT_EMBEDDING_MODEL과 함께
    DBEXPERT_EMBEDDING_DIM(출력 차원)도 같이 지정해야 한다.
"""
from __future__ import annotations

import os
from typing import Optional

DEFAULT_MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
DEFAULT_EMBEDDING_DIM = 384

# HuggingFace Hub repo id 또는 로컬 디렉터리 경로. 기본 모델 외의 값을 쓰면
# 기존 data/ontology_index/<connection>/lancedb/ 는 재구축해야 한다.
MODEL_NAME = os.environ.get("DBEXPERT_EMBEDDING_MODEL", DEFAULT_MODEL_NAME)
EMBEDDING_DIM = int(os.environ.get("DBEXPERT_EMBEDDING_DIM", DEFAULT_EMBEDDING_DIM))

_model: Optional[object] = None  # 지연 로드 후 프로세스 전체에서 재사용


def _get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer  # 지연 import — optional dependency

        _model = SentenceTransformer(MODEL_NAME)
    return _model


def embed(texts: list[str]) -> list[list[float]]:
    """텍스트 목록을 L2-정규화된 임베딩 벡터 목록으로 변환한다.

    정규화(normalize_embeddings=True)해 두면 Cosine 유사도가 단순 내적과
    같아져 LanceDB의 cosine metric과 자연스럽게 맞물린다.
    """
    if not texts:
        return []
    model = _get_model()
    vectors = model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
    return vectors.tolist()


def embed_one(text: str) -> list[float]:
    return embed([text])[0]
