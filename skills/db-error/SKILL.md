---
name: db-error
description: DB Error 메시지를 분석하고 관련 DB 상태(Session/Lock/Slow Query)를 조회하여 장애 원인을 추론하는 Maintenance Skill
---

# DB Error Analysis

이 Skill은 이미 발생한 DB 장애/에러를 진단하는 **Maintenance** 성격의 Skill이다.
전체 Skill 분류는 `Doc/00_개발요건사항.md` §5.3을 참고한다.

## 언제 사용하는가

사용자가 에러 메시지를 직접 붙여넣거나, "장애", "에러 분석해줘", "왜 실패했지" 같은
표현으로 특정 사고(incident)의 원인을 묻는 경우 이 Skill을 사용한다.

대상 Table/Column이 무엇인지 모호하면 먼저 `search_object_catalog`로 대상을 확정한다.

## 절차

1. 사용자가 제공한 에러 메시지(또는 최근 시간 범위)를 확인한다.
2. `db_error_analyze`를 호출하여 Error Code/Message, 발생 시간, 관련 Session, Lock,
   실행 중 SQL, 최근 Slow Query, DB 상태를 함께 조회한다.
3. Deadlock/Timeout/Connection 계열 등 에러 유형별로 관련 정보를 교차 검증한다.
   - Deadlock: 관련된 두 Session이 서로 어떤 리소스를 대기하는지 `get_locks`로 확인
   - Timeout: 동시간대 `slow_query_list` 결과와 상관관계 확인
4. 원인을 다음 형식으로 정리한다.

   ```text
   [원인]
   ...
   [근거]
   ...
   [관련 SQL]
   ...
   [권장 조치]
   ...
   ```

5. 근거가 부족해 확신할 수 없는 부분은 추측으로 단정하지 말고 "추가 확인이 필요하다"고 명시한다.
6. 대상 Engine이 `ERROR_LOG`/`ACTIVITY` Capability를 지원하지 않으면(`CAPABILITY_NOT_SUPPORTED`)
   해당 사실을 사용자에게 안내하고 가능한 정보만으로 분석한다.

## 원칙

- Read Only. 어떤 경우에도 `KILL`, `TERMINATE SESSION` 등 운영 명령을 직접 실행하지 않는다.
- 실행 결과는 자동으로 `log_qa`를 통해 기록된다.
- 여러 증상이 얽혀 있어 "왜 DB가 느려졌는지" 수준의 통합 진단이 필요하면
  `slow-query` Skill과 함께 조합해서 사용한다.
