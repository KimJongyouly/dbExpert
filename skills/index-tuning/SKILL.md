---
name: index-tuning
description: 대상 SQL/Table의 Index 사용 현황을 분석해 Missing/Unused Index를 찾고 개선안을 제안하는 Tuner Skill
---

# Index Tuning

이 Skill은 이미 발생한 장애를 진단하는 것이 아니라, 구조적 성능 개선을 목표로 하는
**Tuner** 성격의 Skill이다. 전체 분류는 `Doc/00_개발요건사항.md` §5.3 참고.

## 언제 사용하는가

"인덱스 추천해줘", "이 쿼리 더 빠르게", "사용하지 않는 인덱스 있어?" 처럼
특정 장애 언급 없이 사전 점검/개선을 요청할 때 사용한다.

## 절차

1. 대상 Table의 기존 Index 목록과 Cardinality를 `get_table_statistics` /
   `get_index_statistics`로 확인한다.
2. 대상 SQL이 있다면 WHERE/JOIN/ORDER BY 절의 컬럼을 추출한다.
3. 기존 Index로 커버되지 않는 컬럼(Missing Index 후보)을 확인한다.
4. Composite Index가 필요한 경우 컬럼 순서(Selectivity 기준)를 결정한다.
5. 거의 사용되지 않는 기존 Index(Drop 후보)를 확인한다.
6. Index 추가/삭제가 쓰기 성능·저장 공간에 미치는 Trade-off를 평가한다.
7. `index_recommend`로 종합 결과를 받고, 가능하면 `query_explain`으로
   Before/After 실행계획을 비교해 근거를 제시한다.
8. 실제 DDL(`CREATE INDEX` 등)은 **절대 실행하지 않고** "제안"으로만 제시한다.
   적용은 사용자의 명시적 승인과 별도 절차를 거친다.

## Engine별 유의사항

- Elasticsearch/OpenSearch, Redis처럼 전통적 Index 개념이 없는 Engine에서는
  Index DDL 대신 Mapping 최적화 또는 Key 설계/TTL/메모리 조언으로 결과를 대체한다
  (`Doc/00_개발요건사항.md` §7.2 Capability 매트릭스 참고).
- Hypothetical Index 분석(HypoPG 등)이 없는 환경에서는 이론적 판단만 제공하며
  추정의 한계를 답변에 명시한다.

## 원칙

Read Only. 실행 결과는 자동으로 `log_qa`를 통해 기록된다.
