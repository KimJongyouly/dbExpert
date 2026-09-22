"""summarize_query_patterns / get_query_patterns Tool 구현.

Tool 시그니처, QueryPattern Schema: Doc/01_개발정의서.md §3.6, §4.5.
저장 위치: data/qa_logs/<connection>/patterns.json (Server당 1개).

Embedding Provider가 아직 확정되지 않아(Doc/01_개발정의서.md §11), 1차
구현으로는 (Skill, 참조 객체 집합)을 클러스터 키로 쓰는 결정적(deterministic)
그룹핑을 사용한다 — 진짜 임베딩 기반 유사도로 바꾸더라도 저장 포맷과
get_query_patterns의 인터페이스는 그대로 유지할 수 있다.

주의: 이 모듈은 규칙/모델 파라미터를 재학습(Fine-tuning)하지 않는다. 누적된
Q&A 로그를 정리해 Claude가 참고할 Context를 만드는 Retrieval 기반 요약이다
(Doc/00_개발요건사항.md §11.3).
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

DATA_DIR = Path("data/qa_logs")

# Connection별로 "마지막으로 요약에 반영한 로그의 timestamp"를 기록해 둔다.
# since를 지정하지 않고 반복 호출해도(= Daily 배치의 기본 동작) 이미 반영한
# 로그를 또 세어 frequency가 중복 누적되지 않도록 하기 위함이다
# (Doc/01_개발정의서.md §3.6 "미지정 시 마지막 배치 이후 전체").
_CURSOR_FILENAME = "_last_summarized_at.txt"


def summarize_query_patterns(
    since: Optional[str] = None, until: Optional[str] = None
) -> dict[str, Any]:
    """지정 기간의 Q&A 로그를 Server별로 클러스터링해 대표 패턴을 upsert한다.

    Returns: { "patterns_upserted": int, "connections_processed": str[] }
    """
    if not DATA_DIR.exists():
        return {"patterns_upserted": 0, "connections_processed": []}

    explicit_since_dt = datetime.fromisoformat(since) if since else None
    until_dt = datetime.fromisoformat(until) if until else None

    connections_processed = []
    total_upserted = 0
    for connection_dir in sorted(p for p in DATA_DIR.iterdir() if p.is_dir()):
        cursor_path = connection_dir / _CURSOR_FILENAME
        since_dt = explicit_since_dt if explicit_since_dt is not None else _read_cursor(cursor_path)

        entries = _load_entries(connection_dir, since_dt, until_dt)
        if not entries:
            continue

        clusters = _cluster(entries)
        upserted = _upsert_patterns(connection_dir, clusters)
        total_upserted += upserted
        connections_processed.append(connection_dir.name)

        _write_cursor(cursor_path, max(e["timestamp"] for e in entries))

    return {"patterns_upserted": total_upserted, "connections_processed": connections_processed}


def _read_cursor(cursor_path: Path) -> Optional[datetime]:
    if not cursor_path.exists():
        return None
    text = cursor_path.read_text(encoding="utf-8").strip()
    return datetime.fromisoformat(text) if text else None


def _write_cursor(cursor_path: Path, timestamp: str) -> None:
    cursor_path.write_text(timestamp, encoding="utf-8")


def get_query_patterns(
    connection_name: str, limit: int = 20, related_object: Optional[str] = None
) -> dict[str, Any]:
    """Server별 누적 쿼리 패턴을 조회한다.

    Returns: { "patterns": QueryPattern[] }
    """
    patterns_path = DATA_DIR / connection_name / "patterns.json"
    if not patterns_path.exists():
        return {"patterns": []}

    patterns = json.loads(patterns_path.read_text(encoding="utf-8"))
    if related_object:
        patterns = [p for p in patterns if related_object in p.get("related_objects", [])]
    patterns.sort(key=lambda p: p.get("frequency", 0), reverse=True)
    return {"patterns": patterns[:limit]}


def _load_entries(
    connection_dir: Path, since_dt: Optional[datetime], until_dt: Optional[datetime]
) -> list[dict[str, Any]]:
    entries = []
    for log_file in sorted(connection_dir.glob("*.jsonl")):
        for line in log_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            entry = json.loads(line)
            ts = datetime.fromisoformat(entry["timestamp"])
            # since_dt는 "마지막으로 반영된 시각" 기준 배타적(exclusive) 경계로
            # 다룬다 — 그래야 커서에 저장된 시각과 정확히 같은 entry가 다음
            # 배치에서 다시 카운트되지 않는다.
            if since_dt and ts <= since_dt:
                continue
            if until_dt and ts > until_dt:
                continue
            entries.append(entry)
    return entries


def _cluster(entries: list[dict[str, Any]]) -> dict[tuple[str, tuple[str, ...]], dict[str, Any]]:
    clusters: dict[tuple[str, tuple[str, ...]], dict[str, Any]] = {}
    for entry in entries:
        objects_key = tuple(sorted(entry.get("objects_referenced", [])))
        key = (entry.get("skill", "unknown"), objects_key)
        cluster = clusters.get(key)
        if cluster is None:
            clusters[key] = {
                "representative_question": entry["question"],
                "frequency": 1,
                "last_seen": entry["timestamp"],
                "related_objects": list(objects_key),
                "representative_sql": (entry.get("sql_used") or [None])[0],
                "related_skill": entry.get("skill"),
                "typical_recommendation": None,
            }
        else:
            cluster["frequency"] += 1
            if entry["timestamp"] > cluster["last_seen"]:
                cluster["last_seen"] = entry["timestamp"]
    return clusters


def _upsert_patterns(
    connection_dir: Path, clusters: dict[tuple[str, tuple[str, ...]], dict[str, Any]]
) -> int:
    patterns_path = connection_dir / "patterns.json"
    existing = (
        json.loads(patterns_path.read_text(encoding="utf-8")) if patterns_path.exists() else []
    )
    existing_by_id = {p["pattern_id"]: p for p in existing}

    for key, cluster in clusters.items():
        pattern_id = _pattern_id(key)
        prior = existing_by_id.get(pattern_id)
        if prior is None:
            existing_by_id[pattern_id] = {
                "connection": connection_dir.name,
                "pattern_id": pattern_id,
                **cluster,
            }
        else:
            prior["frequency"] += cluster["frequency"]
            if cluster["last_seen"] > prior["last_seen"]:
                prior["last_seen"] = cluster["last_seen"]

    patterns_path.write_text(
        json.dumps(list(existing_by_id.values()), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return len(clusters)


def _pattern_id(key: tuple[str, tuple[str, ...]]) -> str:
    skill, objects = key
    suffix = "-".join(objects) if objects else "general"
    return f"{skill}-{suffix}"[:80]
