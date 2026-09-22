---
name: slow-query
description: Slow Query를 조회하고 실행계획을 분석하여 Full Scan/Missing Index/Join 문제 등 성능 저하 원인을 진단하는 Maintenance Skill
---

# Slow Query Analysis

## 언제 사용하는가

"느려", "slow query", "5초 이상 걸리는 쿼리" 등 이미 나타난 성능 증상을 진단할 때 사용한다.
장애/에러 메시지가 있는 경우는 `db-error` Skill을 우선 사용한다.

## 절차

1. `slow_query_list`로 대상 기간/임계값에 해당하는 SQL 목록을 조회한다
   (기본: 최근 구간, 5초 이상, 최대 20건).
2. 다음 순서로 분석한다.
   1. 실행 빈도를 확인한다.
   2. 평균/최대 실행 시간을 확인한다.
   3. Rows Examined와 Rows Sent를 비교한다.
   4. `query_explain`으로 실행계획을 확인한다.
   5. Index 사용 여부를 확인한다.
   6. Full Scan 여부를 확인한다.
   7. Join/Sort/Temporary Table 사용 여부를 확인한다.
   8. DB 전체 부하에 미치는 영향을 평가한다.
   9. 원인과 해결방안을 구분해서 제시한다.
3. Missing Index가 의심되면 결론에서 단정하지 말고, 구조적 개선이 필요하면
   `index-tuning`/`query-tuning` Skill로 이어서 진행할 것을 제안한다.
4. 대상 테이블/컬럼이 불명확하면 `search_object_catalog`로 먼저 확인한다.

## 원칙

- Read Only. `EXPLAIN ANALYZE`처럼 실제 실행을 유발하는 옵션은 기본적으로 사용하지 않는다.
- 대상 Engine이 `SLOW_OP` Capability를 지원하지 않으면 있을 수 없다(모든 Engine이
  어떤 형태로든 Slow Operation 조회를 지원 — `Doc/00_개발요건사항.md` §7.2 참고).
  다만 결과 형태(Slow Query vs Profiler vs Slow Log vs SLOWLOG)는 Engine마다 다르므로
  사용자에게 어떤 소스 기반 분석인지 명시한다.
- 실행 결과는 자동으로 `log_qa`를 통해 기록되고, 누적되면 `get_query_patterns`로
  같은 서버의 반복 이슈를 참고할 수 있다.
