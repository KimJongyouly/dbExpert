"""쿼리가 실제로 접근한 Table의 Ontology를 "당일 최초 1회만" 자동 갱신한다.

사용자 요청: "어느 서버 어느 DB에 접속해서 쿼리를 하는 순간 그 서버, 그
DB, 그 테이블의 정보가 (당일 처음 한 번만) 업데이트되게 해줘. 쿼리가 여러
테이블로 구성돼 있으면 그 테이블들 전부."

Doc/00_개발요건사항.md §10 Ontology/RAG의 Daily Batch(§11 "Daily 배치 실행
방식", 아직 스케줄러 자체는 미구현)와는 별개의, 사용량 기반 트리거다 —
새벽 배치를 기다리지 않고 Claude가 실제로 그 Table에 쿼리를 실행하는
순간(Tool 호출 시점) 즉시 반영하되, 같은 날 이미 갱신한 Table은 건너뛴다.
`build_ontology_index`(Connection 전체 스캔)보다 훨씬 가볍다 — Table
단위로만 `get_schema({"container": table})`를 호출한다.

호출한 Tool의 응답을 지연시키지 않도록 백그라운드 스레드에서 실행하고,
실패해도 예외를 삼켜 원래 Tool 호출에 영향을 주지 않는다(best-effort
부수효과 — Ontology 데이터가 아니라 DB 자체에 영향을 주는 게 아니므로
실패해도 안전하다).
"""
from __future__ import annotations

import json
import threading
from datetime import date
from pathlib import Path
from typing import Iterable, Optional

from server.catalog.ontology_builder import to_ontology_document
from server.catalog.rag_index import RagIndex
from server.database.base import BaseAdapter, Capability

DATA_DIR = Path("data/ontology_index")

_locks_guard = threading.Lock()
_connection_locks: dict[str, threading.Lock] = {}


def _lock_for(connection_name: str) -> threading.Lock:
    with _locks_guard:
        return _connection_locks.setdefault(connection_name, threading.Lock())


def _tracker_path(connection_name: str) -> Path:
    return DATA_DIR / connection_name / "table_refresh_dates.json"


def _load_tracker(connection_name: str) -> dict[str, str]:
    path = _tracker_path(connection_name)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_tracker(connection_name: str, tracker: dict[str, str]) -> None:
    path = _tracker_path(connection_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(tracker, ensure_ascii=False), encoding="utf-8")


def refresh_stale_tables(
    connection_name: str, adapter: BaseAdapter, table_names: Iterable[str]
) -> Optional[threading.Thread]:
    """table_names 중 오늘 아직 갱신되지 않은 것만 백그라운드로 다시
    수집해 Ontology Index에 upsert한다. 즉시 반환한다(non-blocking).

    Returns: 실제로 스레드를 띄웠으면 그 Thread(테스트에서 join()으로 완료를
    기다릴 수 있도록), 갱신할 대상이 없거나 Capability가 없으면 None.
    """
    names = sorted({t for t in table_names if t})
    if not names or not adapter.supports(Capability.SCHEMA):
        return None

    thread = threading.Thread(
        target=_refresh_worker,
        args=(connection_name, adapter, names),
        daemon=True,
        name="ontology-auto-refresh",
    )
    thread.start()
    return thread


def _refresh_worker(connection_name: str, adapter: BaseAdapter, names: list[str]) -> None:
    lock = _lock_for(connection_name)
    with lock:
        today = date.today().isoformat()
        tracker = _load_tracker(connection_name)
        stale = [t for t in names if tracker.get(t) != today]
        if not stale:
            return

        index = RagIndex(connection_name)
        for table in stale:
            try:
                schema_objects = adapter.get_schema({"container": table})
                if not schema_objects:
                    continue
                documents = [
                    to_ontology_document(connection_name, obj) for obj in schema_objects
                ]
                index.upsert(documents)
            except Exception:  # noqa: BLE001 — best-effort 부수효과, 실패해도 원 호출에 영향 없음
                continue
            tracker[table] = today

        _save_tracker(connection_name, tracker)
