"""Ontology/RAG용 HuggingFace 임베딩 모델을 미리 내려받는다.

dbExpert의 Ontology 검색(`search_object_catalog`, `build_ontology_index`)은
sentence-transformers 다국어 모델을 로컬에서 실행한다. 모델 가중치(약 460MB)는
`uv sync` 시점이 아니라 Ontology Tool을 처음 호출하는 순간 HuggingFace Hub에서
받아오기 때문에, 실제 작업 중 첫 호출이 느려지거나(네트워크에 따라 수 분)
프록시/폐쇄망에서는 아예 실패할 수 있다. 이 스크립트로 미리 받아두면 그
지연을 없앨 수 있다.

사용법 (Plugin 루트에서):

    uv run --no-sync python scripts/download_embedding_model.py
    uv run --no-sync python scripts/download_embedding_model.py --save-to ./models/multilingual-minilm

- 기본 동작: `server/catalog/embeddings.py`와 동일한 모델을 HuggingFace 캐시
  (`$HF_HOME` 또는 `~/.cache/huggingface`)에 받고, 출력 차원이 코드가 기대하는
  값(EMBEDDING_DIM)과 같은지 확인한다.
- `--save-to DIR`: 캐시와 별개로 모델을 지정 디렉터리에 저장한다. 폐쇄망 PC로
  옮겨 `DBEXPERT_EMBEDDING_MODEL=<그 디렉터리 절대경로>`로 쓰는 용도다
  (Doc/02_설치.md §4.6).

환경변수:
- DBEXPERT_EMBEDDING_MODEL : 받을 모델(repo id 또는 로컬 경로). 기본값은
  embeddings.py의 DEFAULT_MODEL_NAME.
- HF_HOME                  : HuggingFace 캐시 디렉터리 변경.
- HF_ENDPOINT              : 미러를 쓸 때 Hub 주소 변경.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

# Plugin 루트를 sys.path에 넣어 `server.catalog.embeddings`를 import한다.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _human(n_bytes: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n_bytes < 1024:
            return f"{n_bytes:.0f}{unit}"
        n_bytes /= 1024
    return f"{n_bytes:.1f}TB"


def _dir_size(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--save-to",
        metavar="DIR",
        help="캐시 외에 모델을 이 디렉터리에도 저장한다(폐쇄망 이동용).",
    )
    args = parser.parse_args()

    try:
        from sentence_transformers import SentenceTransformer
    except ImportError:
        print(
            "sentence-transformers가 설치되어 있지 않습니다. "
            "`uv sync --extra ontology` (또는 `--extra all`) 후 다시 실행하세요.",
            file=sys.stderr,
        )
        return 1

    from server.catalog import embeddings

    model_name = embeddings.MODEL_NAME
    expected_dim = embeddings.EMBEDDING_DIM
    hf_home = os.environ.get("HF_HOME") or str(Path.home() / ".cache" / "huggingface")

    print(f"모델      : {model_name}")
    print(f"기대 차원 : {expected_dim}")
    print(f"HF 캐시   : {hf_home}")
    print("다운로드/로드 중... (최초 1회는 약 460MB를 받습니다)")

    started = time.monotonic()
    model = SentenceTransformer(model_name)
    elapsed = time.monotonic() - started

    actual_dim = model.get_sentence_embedding_dimension()
    print(f"완료 ({elapsed:.1f}초). 실제 출력 차원: {actual_dim}")

    if actual_dim != expected_dim:
        print(
            f"⚠️ 출력 차원 불일치: 코드는 {expected_dim}을 기대하지만 모델은 {actual_dim}입니다. "
            "기본 모델이 아닌 모델을 지정했다면 .env에 "
            f"DBEXPERT_EMBEDDING_DIM={actual_dim} 을 함께 설정하고, 기존 "
            "data/ontology_index/<connection>/ 를 지운 뒤 재구축하세요.",
            file=sys.stderr,
        )
        return 2

    # 동작 확인: 한/영 문장을 실제로 임베딩해본다.
    vectors = embeddings.embed(["고객 주문 테이블", "customer orders table"])
    print(f"임베딩 동작 확인: {len(vectors)}건, 각 {len(vectors[0])}차원")

    if args.save_to:
        target = Path(args.save_to).expanduser().resolve()
        target.mkdir(parents=True, exist_ok=True)
        model.save(str(target))
        print(f"로컬 저장 완료: {target} ({_human(_dir_size(target))})")
        print("옮긴 PC의 .env 예시:")
        print(f"  DBEXPERT_EMBEDDING_MODEL={target}")
        print("  HF_HUB_OFFLINE=1")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
