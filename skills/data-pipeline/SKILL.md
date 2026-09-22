---
name: data-pipeline
description: Raw Data 소스를 탐색하고 Target으로 옮기는 ETL/ELT Pipeline을 설계·검증하는 DataEngineer Skill
---

# Data Pipeline Design

**DataEngineer** 성격의 Skill로, Maintenance/Tuner와 달리 데이터의 이동/변환 자체를 다룬다.

## 언제 사용하는가

"원본 데이터 찾아줘", "~로 옮기는 파이프라인 설계해줘", "적재해줘" 등
데이터 이동/변환 요청에 사용한다.

## 절차

1. Source(Raw Data)의 위치, 스키마, 데이터 품질을 탐색한다.
   - `list_raw_sources`로 후보를 나열
   - `get_schema`로 스키마 확인 (대상이 불명확하면 `search_object_catalog` 우선 사용)
   - `profile_data`로 Null 비율/Cardinality/샘플(마스킹 적용)을 확인
2. Target 스키마를 조회하고 Source-Target 컬럼을 매핑한다. 매핑되지 않는 컬럼과
   타입 불일치를 명시적으로 식별한다.
3. 필요한 변환(클렌징/정규화/조인/집계/파생 컬럼) 로직을 설계한다.
4. Full Load / Incremental Load 여부와 증분 기준 컬럼(`updated_at` 등)을 결정한다.
5. `generate_pipeline_plan`으로 Extract → Transform → Load 설계안을 만든다.
6. 스케줄링, 오류 처리/재시도 전략(실패 레코드 격리, 알림)을 제안한다.
7. Idempotency(재실행 시 중복 적재 방지) 확보 방안을 포함한다.
8. `validate_pipeline`으로 Dry-run/Sample 검증을 수행하여 Target 제약조건
   위반 가능성, 캐스팅 실패 가능성을 확인한다.
9. 실제 Pipeline 실행/배포는 수행하지 않고 "설계안"으로만 결과를 제시한다.
   적용은 사용자가 별도 CI/CD 또는 오케스트레이션 도구(Airflow/dbt 등)로 진행한다.

## 원칙

- Raw Data는 민감 정보를 포함할 수 있으므로 Sample 조회 시 최소 건수 제한과
  필요 시 마스킹을 적용한다.
- Read Only. Target에 대한 실제 적재(쓰기)는 절대 수행하지 않는다.
- 실행 결과는 자동으로 `log_qa`를 통해 기록된다.
