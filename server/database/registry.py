"""Adapter Registry — db_type ↔ Adapter 클래스 매핑.

Doc/00_개발요건사항.md §7.3 확장 구조, Doc/01_개발정의서.md §2.4 참고.

신규 Engine을 추가할 때 Skill/MCP Tool 코드는 전혀 건드리지 않고,
1) server/database/<engine>.py 에 Adapter 구현
2) 해당 모듈이 import 시점에 `AdapterRegistry.register(...)` 호출
3) register_builtin_adapters() 의 import 목록에 모듈 추가
만 하면 된다.
"""
from __future__ import annotations

from typing import Type

from server.database.base import BaseAdapter


class AdapterRegistry:
    _registry: dict[str, Type[BaseAdapter]] = {}
    _engine_family: dict[str, str] = {}

    @classmethod
    def register(
        cls, db_type: str, adapter_class: Type[BaseAdapter], engine_family: str
    ) -> None:
        cls._registry[db_type] = adapter_class
        cls._engine_family[db_type] = engine_family

    @classmethod
    def resolve(cls, db_type: str) -> Type[BaseAdapter]:
        try:
            return cls._registry[db_type]
        except KeyError as exc:
            raise ValueError(
                f"Unregistered db_type: {db_type!r}. "
                f"등록된 값: {sorted(cls._registry)}"
            ) from exc

    @classmethod
    def engine_family_of(cls, db_type: str) -> str:
        return cls._engine_family[db_type]

    @classmethod
    def list_registered(cls) -> list[dict[str, object]]:
        return [
            {
                "db_type": db_type,
                "engine_family": cls._engine_family[db_type],
                "capabilities": sorted(c.value for c in adapter_class.capabilities),
            }
            for db_type, adapter_class in cls._registry.items()
        ]


def register_builtin_adapters() -> None:
    """내장 Adapter 모듈을 import하여 self-registration을 트리거한다.

    server/main.py 기동 시 1회 호출한다. 각 Adapter 모듈은 파일 하단에서
    `AdapterRegistry.register("mysql", MySQLAdapter, engine_family="rdb")`
    형태로 스스로를 등록한다.
    """
    from server.database import (  # noqa: F401  (import for side-effect)
        clickhouse,
        elasticsearch,
        mongodb,
        mssql,
        mysql,
        opensearch,
        postgresql,
        redis,
        tibero,
    )
