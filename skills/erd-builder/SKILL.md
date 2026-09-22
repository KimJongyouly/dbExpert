---
name: erd-builder
description: 테이블 간 관계(PK/FK)를 조회해 Mermaid erDiagram 문법으로 변환하고 Claude Code에서 바로 렌더링되는 ERD로 제시하는 공통 Skill
---

# ERD Builder

Maintenance/Tuner/DataEngineer 어디에도 속하지 않는 **공통 기반** Skill이다
(`Doc/00_개발요건사항.md` §10.4, §5 Skill 라우팅 우선순위 1번 참고). "테이블
관계를 조회한다"는 점에서는 Ontology 검색과 이어지지만, 이 Skill은 그
결과를 **사람이 보는 다이어그램**으로 만드는 것이 목적이다.

## 언제 사용하는가

"ERD 그려줘", "관계도 보여줘", "테이블들 관계가 어떻게 돼", "다이어그램으로
그려줘"처럼 Table 간 구조/관계를 시각적으로 보고 싶어할 때 사용한다.

## 절차

1. 대상 Table이 이름으로 명확하지 않으면(예: "주문 관련 테이블들 ERD로
   그려줘") `search_object_catalog`로 먼저 후보를 확인하고, 결과가
   모호하면(유사도가 근접한 후보가 여럿) 사용자에게 재확인한다.
2. `get_table_relationships`를 호출한다. `scope`는 상황에 맞게 고른다.
   - 사용자가 Table명을 명시했으면: `{ "tables": ["orders", "customers"] }`
   - 특정 Table과 연관된 것 전부: `{ "related_to": "orders" }`
   - Schema 전체: `{ "schema": "public", "limit": 20 }` (Table이 많으면
     상위 N개로 제한되니, 초과 사실을 안내한다)
3. **Tool은 JSON만 반환한다 — Mermaid 변환은 Claude가 직접 한다**(Python이
   변환하지 않는 것이 설계 원칙, `Doc/00_개발요건사항.md` §10.4 참고).
   아래 매핑대로 `tables`/`relationships`를 `erDiagram` 문법으로 옮긴다.

   ```
   tables[].container            → 엔티티 이름 (대문자 권장)
   tables[].fields[].{name,type,is_pk} → 엔티티 블록 안 속성 한 줄씩
                                          (PK인 필드에는 뒤에 PK 표시)
   relationships[].{from_table, from_column, to_table, to_column,
                     cardinality, confidence} → 관계 한 줄
     - cardinality "1:1" → ||--||
     - cardinality "1:N" → ||--o{ (from_table쪽이 "1")
     - confidence가 "inferred"면 관계 라벨 뒤에 "(추정)"을 붙여
       선언된 FK(declared)와 구분해서 표시한다
   ```

   변환 예:

   ```mermaid
   erDiagram
       CUSTOMERS ||--o{ ORDERS : places
       ORDERS ||--o{ ORDER_ITEMS : "contains (추정)"
       CUSTOMERS {
           bigint id PK
           varchar name
           varchar email
       }
       ORDERS {
           bigint id PK
           bigint customer_id FK
           datetime created_at
       }
   ```

4. 완성된 Mermaid를 ` ```mermaid ` 코드 블록으로 응답에 그대로 포함한다 —
   Claude Code가 자동으로 다이어그램을 렌더링한다(별도 이미지 생성 Tool
   없음).
5. Table 수가 많아 다이어그램이 과도하게 복잡해지면, 한 번에 전부 그리는
   대신 관련도가 높은 하위 집합으로 나누거나 사용자에게 범위 축소를
   제안한다. 다수 Table/복잡한 관계라 별도로 공유하기 좋은 형태가 필요하면
   Artifact로 발행하는 것도 제안할 수 있다.

## Engine별 유의사항

- FK/관계 Capability가 없는 Engine(Elasticsearch/OpenSearch, Redis)은
  `relationships`가 항상 빈 배열로 온다 — 이 경우 관계도 대신 Container
  (Index/Key Pattern) 목록만 제시한다.
- MongoDB/ClickHouse는 선언된 FK 자체가 없는 구조라 관계가 나오면 항상
  `confidence: "inferred"`다 — 컬럼명 패턴(`*_id`)과 Ontology comment
  기반 추정이므로, 결과에 추정이라는 사실을 반드시 명시한다.

## 원칙

- Read Only. ERD는 Table/Column 구조(메타데이터)만 다루며 실제 Row 데이터는
  조회/포함하지 않는다.
- 실행 결과는 자동으로 `log_qa`를 통해 기록된다.
