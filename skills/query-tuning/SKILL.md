---
name: query-tuning
description: 비효율적인 SQL 패턴을 탐지하고 더 효율적인 대안 SQL을 제안하는 Tuner Skill
---

# Query Rewrite Tuning

`index-tuning`과 함께 **Tuner** Skill에 속한다. Index를 바꾸는 대신
SQL 자체의 구조를 개선하는 데 초점을 둔다.

## 언제 사용하는가

Index 문제가 아니라 SQL 작성 방식 자체가 비효율적인 것으로 의심될 때,
또는 사용자가 명시적으로 "쿼리를 다시 짜줘/개선해줘"라고 요청할 때 사용한다.

## 절차 — "학습된 패턴 우선, 없으면 새로 분석"

0. **기존 튜닝 이력을 먼저 확인한다.** 대상 SQL의 참조 객체로
   `get_query_patterns(connection, related_object=...)`를 조회하거나,
   자연어 질문 그대로 `find_query_pattern`을 호출한다.
   - 주의: 이 이력은 LanceDB가 아니라 `data/qa_logs/<connection>/patterns.json`에
     저장되어 있다 — LanceDB(`data/ontology_index/`)는 테이블/컬럼 Ontology
     검색 전용이며 쿼리 튜닝 이력과는 별개다.
   - `score`가 높거나(대략 0.8 이상) `typical_recommendation`/`representative_sql`이
     이미 있으면, 그 Before/After 정보를 그대로 활용해 4번 형식으로 바로
     답하고 1~3번(재분석)은 생략한다 — "다음엔 더 빠르게 답한다" 경로.
   - 매치가 없거나 점수가 애매하면(0.5~0.8) 참고만 하고 1번부터 새로 진행한다.
1. 대상 SQL을 확인하고 다음 패턴을 탐지한다.
   - `SELECT *` 사용
   - 불필요한 Subquery / Correlated Subquery
   - 비효율적인 `OR` 조건 (`UNION` 전환 가능성)
   - 컬럼에 함수를 적용해 Index를 못 타는 패턴 (`WHERE DATE(col) = ...` 등)
   - 불필요한 `DISTINCT` / `ORDER BY`
   - 과도하거나 비효율적인 JOIN 순서
2. `query_rewrite_suggest`로 문제점과 실행계획을 받는다. 개선 방향이 하나로
   좁혀지지 않으면(예: OR→UNION 분리, 함수 제거를 전제로 한 신규 Index,
   컬럼 최소화만 적용하는 보수적 안 등 Trade-off가 다른 접근이 여럿이면),
   최대 3개의 개선안을 준비한다. 방향이 명확히 하나뿐이면 1안만 제시해도 된다 —
   억지로 3개를 채우지 않는다.
3. 각 개선안 SQL에 대해 `query_explain`을 재실행하여 실행계획 변화를 확인하고,
   Before/After Cost·Scan Type을 비교해 안별로 개선 효과와 Trade-off를 설명한다.
4. 결과를 다음 형식으로 정리한다 — 개선안이 여러 개면 안별로 반복한다.

   ```text
   [현재 SQL의 문제점]
   ...
   [1안] <한 줄 요약>
     - 개선 SQL: ...
     - Before/After: Cost ... → ..., Scan Type ... → ...
     - 예상 효과 / Trade-off: ...
   [2안] (있는 경우, 형식 동일)
   [3안] (있는 경우, 형식 동일)
   [권장]: N안 권장 — 이유
   [검증 방법]
   ...
   ```
5. 응답을 마치면 `log_qa`의 `sql_used`에 채택/권장한 개선 SQL을 포함해
   기록한다. 이 로그가 (수동 또는 주기적) `summarize_query_patterns` 실행을
   거쳐 `patterns.json`에 쌓이면, 다음에 같은/비슷한 SQL을 물었을 때 0번에서
   바로 재사용된다 — 실시간 자동 반영은 아니며 요약 배치가 실행되어야 반영된다.

## 원칙

- 제안된 SQL을 Claude가 직접 운영 DB나 애플리케이션 코드에 적용하지 않는다.
  사용자가 검토 후 반영한다.
- MongoDB/Elasticsearch/Redis처럼 `SQL` Capability가 없는 Engine에서는
  이 Tool이 `CAPABILITY_NOT_SUPPORTED`를 반환하므로, 필요 시 Query DSL/Aggregation
  최적화 조언으로 안내를 대체한다(향후 별도 Tool로 확장 검토 —
  `Doc/01_개발정의서.md` §11 미결정 사항).
- Read Only. 실행 결과는 자동으로 `log_qa`를 통해 기록된다.
- 개선안 개수는 형식을 채우기 위한 목표가 아니라 실제로 서로 다른 Trade-off가
  있을 때만 늘린다. 근거 없는 안을 억지로 만들어내지 않는다.
