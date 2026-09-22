"""Capability 기반 Database Adapter 인터페이스.

전체 설계 근거는 Doc/00_개발요건사항.md §7 (지원 Data Store), Doc/01_개발정의서.md
§2.2 (Database Adapter 인터페이스)를 참고한다.

BaseAdapter는 모든 Engine이 구현해야 하는 최소 인터페이스이고, 나머지는
Engine이 실제로 지원하는 Capability만 선택적으로 구현하는 Mixin이다.
"""
from __future__ import annotations

import abc
import enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional


class Capability(enum.Enum):
    """Adapter가 선택적으로 구현하는 기능 단위.

    Doc/00_개발요건사항.md §7.2 Capability 매트릭스의 각 행에 대응한다.
    """

    SQL = "sql"
    SLOW_OP = "slow_op"
    ACTIVITY = "activity"
    ERROR_LOG = "error_log"
    SCHEMA = "schema"
    STATS = "stats"
    RELATIONSHIP = "relationship"
    SAMPLE = "sample"


# ---------------------------------------------------------------------------
# 데이터 모델 (Tool Input/Output 및 Adapter 반환값 공용)
# Doc/01_개발정의서.md §3, §4 의 필드 정의를 그대로 따른다.
# ---------------------------------------------------------------------------


@dataclass
class SchemaField:
    name: str
    type: str
    comment: Optional[str] = None
    is_pk: bool = False
    is_fk: bool = False
    fk_ref: Optional[dict[str, str]] = None
    inferred: bool = False  # Sampling으로 추론된 Field인 경우 True (MongoDB/Redis)


@dataclass
class SchemaObject:
    """Namespace/Container/Field 일반화 계층의 한 Container 단위.

    RDB의 Table, MongoDB의 Collection, Elasticsearch의 Index,
    Redis의 정규화된 Key 패턴에 대응한다.
    """

    container: str
    namespace: Optional[str] = None
    container_comment: Optional[str] = None
    row_count_estimate: Optional[int] = None
    fields: list[SchemaField] = field(default_factory=list)


@dataclass
class SlowOperation:
    operation_id: str
    text: str
    execution_count: int
    avg_duration_ms: float
    max_duration_ms: float
    rows_examined: Optional[int] = None
    rows_returned: Optional[int] = None
    database: Optional[str] = None
    user: Optional[str] = None
    host: Optional[str] = None
    last_seen: Optional[datetime] = None


@dataclass
class Lock:
    holding_session_id: str
    resource: str
    lock_type: str
    waiting_session_id: Optional[str] = None
    wait_time_ms: Optional[float] = None


@dataclass
class Session:
    session_id: str
    state: str
    user: Optional[str] = None
    host: Optional[str] = None
    current_operation: Optional[str] = None
    duration_ms: Optional[float] = None


@dataclass
class DBError:
    error_code: Optional[str]
    message: str
    occurred_at: Optional[datetime] = None


@dataclass
class ContainerStatistics:
    row_count_estimate: Optional[int] = None
    size_bytes: Optional[int] = None
    last_analyzed_at: Optional[datetime] = None


@dataclass
class IndexStatistics:
    index_name: str
    columns: list[str]
    cardinality: Optional[int] = None
    usage_count: Optional[int] = None
    is_unique: bool = False


@dataclass
class Relationship:
    from_container: str
    from_field: str
    to_container: str
    to_field: str
    cardinality: str = "1:N"  # "1:1" | "1:N"
    confidence: str = "declared"  # "declared" | "inferred"


@dataclass
class ExplainPlan:
    raw: dict[str, Any]
    scan_type: Optional[str] = None
    estimated_cost: Optional[float] = None
    estimated_rows: Optional[int] = None
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Timeout 기본값 (Doc/00_개발요건사항.md §33)
# Connection Profile(config/connections.yaml)에 connect_timeout_sec /
# query_timeout_sec를 지정하지 않으면 이 값을 쓴다. YAML에 지정하면 그 값이
# 우선한다 — BaseAdapter.connect_timeout_sec / query_timeout_sec 프로퍼티 참고.
# ---------------------------------------------------------------------------
DEFAULT_CONNECT_TIMEOUT_SEC = 5
DEFAULT_QUERY_TIMEOUT_SEC = 30


# ---------------------------------------------------------------------------
# Adapter 인터페이스
# ---------------------------------------------------------------------------


class BaseAdapter(abc.ABC):
    """모든 Engine Adapter가 구현해야 하는 최소 인터페이스."""

    #: 이 Adapter(서브클래스)가 실제로 구현한 Capability 집합.
    #: 서브클래스에서 클래스 속성으로 override 한다.
    capabilities: set[Capability] = set()

    def __init__(self, connection_profile: dict[str, Any]):
        self.connection_profile = connection_profile

    @abc.abstractmethod
    def ping(self) -> dict[str, Any]:
        """{'version': str, 'uptime_sec': float | None, 'connected': bool} 반환."""

    def supports(self, capability: Capability) -> bool:
        return capability in self.capabilities

    @property
    def connect_timeout_sec(self) -> int:
        """config/connections.yaml의 connect_timeout_sec. 없으면 기본 5초."""
        return self.connection_profile.get("connect_timeout_sec", DEFAULT_CONNECT_TIMEOUT_SEC)

    @property
    def query_timeout_sec(self) -> int:
        """config/connections.yaml의 query_timeout_sec. 없으면 기본 30초.

        각 Engine Adapter의 _connect()/execute_readonly()가 이 값을 실제
        Driver의 Timeout 파라미터로 전달해 강제한다(Engine마다 방식이 다름 —
        예: MySQL은 소켓 read_timeout, PostgreSQL은 statement_timeout GUC,
        MSSQL/ClickHouse/Mongo/Redis/ES는 각 Driver의 timeout 인자).
        """
        return self.connection_profile.get("query_timeout_sec", DEFAULT_QUERY_TIMEOUT_SEC)


class SqlCapable(abc.ABC):
    """Capability.SQL — SELECT/EXPLAIN 실행 가능."""

    @abc.abstractmethod
    def execute_readonly(
        self, sql: str, params: Optional[list[Any]] = None
    ) -> list[dict[str, Any]]:
        """SELECT/SHOW 등 허용된 구문만 실행한다.

        DDL/DML(INSERT/UPDATE/DELETE/CREATE/ALTER/DROP 등)이 감지되면
        ReadOnlyViolationError(에러 코드 DDL_BLOCKED)를 발생시켜야 한다.
        """

    @abc.abstractmethod
    def explain(self, sql: str) -> ExplainPlan:
        """실행계획을 조회한다. `EXPLAIN ANALYZE`류의 실제 실행 옵션은 기본 비활성."""


class SlowOperationCapable(abc.ABC):
    """Capability.SLOW_OP — Slow Query/Profiler/Slow Log/SLOWLOG 등."""

    @abc.abstractmethod
    def get_slow_operations(
        self, min_duration_sec: float, limit: int
    ) -> list[SlowOperation]:
        ...


class ActivityCapable(abc.ABC):
    """Capability.ACTIVITY — Lock/Session/실행 중 작업 조회."""

    @abc.abstractmethod
    def get_locks(self) -> list[Lock]:
        ...

    @abc.abstractmethod
    def get_sessions(self) -> list[Session]:
        ...


class ErrorLogCapable(abc.ABC):
    """Capability.ERROR_LOG — DB 자체 Error Log/통계 조회."""

    @abc.abstractmethod
    def get_errors(self, since: Optional[datetime] = None) -> list[DBError]:
        ...


class SchemaCapable(abc.ABC):
    """Capability.SCHEMA — Namespace/Container/Field 목록 + comment(있는 경우)."""

    @abc.abstractmethod
    def get_schema(self, scope: dict[str, Any]) -> list[SchemaObject]:
        ...


class StatisticsCapable(abc.ABC):
    """Capability.STATS — Table/Index 통계."""

    @abc.abstractmethod
    def get_table_statistics(
        self, namespace: Optional[str], container: str
    ) -> ContainerStatistics:
        ...

    @abc.abstractmethod
    def get_index_statistics(
        self, namespace: Optional[str], container: str
    ) -> list[IndexStatistics]:
        ...


class RelationshipCapable(abc.ABC):
    """Capability.RELATIONSHIP — 선언된 FK 제약 조회 전용.

    컬럼명 패턴 기반 "추정" 관계는 이 Capability의 책임이 아니라
    server/catalog/erd_builder.py 가 SchemaCapable 결과를 바탕으로 별도 수행한다.
    선언된 FK가 없는 Engine(ClickHouse/MongoDB 등)은 이 Capability 자체를
    구현하지 않는다.
    """

    @abc.abstractmethod
    def get_relationships(self, containers: list[str]) -> list[Relationship]:
        ...


class SampleCapable(abc.ABC):
    """Capability.SAMPLE — DataEngineer Raw Data 샘플 조회."""

    @abc.abstractmethod
    def sample_rows(
        self,
        namespace: Optional[str],
        container: str,
        limit: int,
        mask: Optional[list[str]] = None,
    ) -> list[dict[str, Any]]:
        ...
