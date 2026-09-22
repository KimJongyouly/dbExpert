---
name: query-builder
description: 자연어 질문에서 테이블/컬럼명을 유추해 조회 쿼리를 구성·실행하고, 실행 이력을 학습해 비슷한 질문에는 다음부터 더 빠르게 답하는 공통 Skill
---

# Query Builder

Maintenance/Tuner/DataEngineer 어디에도 속하지 않는 **공통** Skill이다.
"이 테이블 통계 보여줘" 같은 정해진 목적이 아니라, **"이런 조건의 데이터
보여줘"류의 자유로운 자연어 질문**에 답하는 것이 목적이다. SQL 자체를
구성하는 것은 Claude의 몫이고(`Doc/00_개발요건사항.md` §6 원칙 — Python
Tool은 사실만 제공, 판단/생성은 Claude가 한다), 이 Skill은 그 과정을
빠르고 안전하게 반복하도록 돕는다.

## 언제 사용하는가

"OO한 회원 목록 보여줘", "최근 일주일간 OO 데이터 몇 건이야" 처럼 특정
Tool(느린 쿼리, 통계, 파이프라인 등)에 딱 맞지 않는 임의의 조회 질문에
사용한다. 이미 발생한 장애 진단이면 `db-error`/`slow-query`를, 구조적
튜닝이면 `index-tuning`/`query-tuning`을 우선 고려한다.

## 절차 — "학습된 패턴 우선, 없으면 새로 유추"

1. **`find_query_pattern`을 가장 먼저 호출한다.** 자연어 질문 그대로
   넘기면, 임베딩 기반 유사도로 이미 비슷한 질문에 답한 적이 있는지 찾아
   대표 SQL(`representative_sql`)과 유사도 점수(`score`)를 함께 준다.
   - `score`가 높은(대략 0.8 이상) 매치가 있으면, 그 SQL을 이번 질문의
     구체적인 조건(값, 날짜 범위 등)에 맞게 **조정만** 해서 바로
     3번으로 넘어간다 — 테이블/컬럼을 처음부터 다시 찾을 필요가 없다.
   - 애매하거나(0.5~0.8) 없으면 2번으로 진행한다.
2. 대상 테이블/컬럼이 불명확하면 `search_object_catalog`로 후보를 찾고,
   필요하면 `get_schema`로 정확한 컬럼명/타입/comment를 확인한 뒤 SQL을
   새로 구성한다. 식별자(테이블/컬럼명)는 실제 조회된 이름만 쓰고
   추측하지 않는다.
3. `execute_readonly_query`로 실행한다.
   - SQL에는 가능하면 **직접 LIMIT을 포함**시킨다 — 이 Tool은 결과를
     Python 쪽에서 자르기 때문에(`sample_rows`와 동일한 방식), SQL 자체에
     LIMIT이 없으면 서버가 전체 결과를 다 처리할 때까지 기다리게 된다.
   - 민감할 수 있는 컬럼(개인정보 등)은 `mask` 파라미터로 가린다.
4. 결과를 사용자에게 설명한다. 응답 종료 시 `log_qa`를 호출할 때
   `sql_used`에 **실제로 실행한 SQL**을 반드시 넣는다 — 이 로그가
   나중에 `summarize_query_patterns`로 요약되어 1번의 학습된 패턴이
   된다("다음엔 더 빠르게 답한다"의 핵심 고리).

## 원칙

- Read Only. `execute_readonly_query`는 기존 Read Only 3중 방어(텍스트
  가드 + 서버 세션 레벨 강제 + Query Timeout)를 그대로 쓴다 — 별도 방어를
  새로 추가하지 않았다.
- MongoDB/Elasticsearch·OpenSearch/Redis처럼 SQL Capability가 없는
  Engine은 `execute_readonly_query`가 `CAPABILITY_NOT_SUPPORTED`를
  반환한다 — 이 경우 각 Engine에 맞는 기존 Tool(`sample_rows` 등)로
  대체한다.
- `find_query_pattern`은 `data/qa_logs/<connection>/patterns.json`이
  아직 없으면(한 번도 학습되지 않았으면) 빈 결과를 반환한다 — 정상이며,
  이 경우 2번부터 시작하면 된다. 학습은 `summarize_query_patterns`가
  주기적으로(또는 수동 호출로) 누적해야 쌓인다.
