"""Tibero 번들 JDBC 드라이버 버전 경고에 대한 단위 테스트.

실제 JVM을 띄우지 않기 위해 `sys.modules["jpype"]`를 stub으로 바꿔치기한다
(jpype.isJVMStarted()가 True를 반환하게 해서 startJVM()까지 가지 않게 함).
"""
from __future__ import annotations

import sys
import warnings

import pytest

import server.database.tibero as tibero_module


class _JPypeStub:
    @staticmethod
    def isJVMStarted() -> bool:
        return True


def test_bundled_driver_warning_emitted_on_first_use(monkeypatch):
    monkeypatch.setattr(tibero_module, "_driver_warning_emitted", False)
    monkeypatch.setitem(sys.modules, "jpype", _JPypeStub())
    monkeypatch.setitem(sys.modules, "jpype.imports", object())

    with pytest.warns(RuntimeWarning, match="tibero6-jdbc.jar"):
        tibero_module._ensure_jvm_started()


def test_bundled_driver_warning_not_repeated_on_second_use(monkeypatch):
    monkeypatch.setattr(tibero_module, "_driver_warning_emitted", False)
    monkeypatch.setitem(sys.modules, "jpype", _JPypeStub())
    monkeypatch.setitem(sys.modules, "jpype.imports", object())

    tibero_module._ensure_jvm_started()  # 1회차 — 경고 발생, 플래그를 True로 바꿔둔다

    with warnings.catch_warnings():
        warnings.simplefilter("error")  # 경고가 또 뜨면 테스트가 실패하도록
        tibero_module._ensure_jvm_started()  # 2회차 — 경고 없이 조용히 통과해야 한다


def test_warning_mentions_verified_server_version():
    assert "Tibero 5.0" in tibero_module._BUNDLED_DRIVER_WARNING


def test_ensure_jvm_started_actually_enables_java_import_with_real_jpype():
    """실제 JPype/JVM으로 회귀 테스트 — `import jpype`만 하고 `import
    jpype.imports`를 빠뜨리면, JVM이 정상적으로 떠 있어도(isJVMStarted()==True)
    `from java.sql import DriverManager`가 `ModuleNotFoundError: No module
    named 'java'`로 실패한다(실제 Tibero 서버 접속 테스트 중 재현됨). Mock이
    아니라 진짜 JPype로 검증해야 이 문제를 잡을 수 있다."""
    pytest.importorskip("jpype")
    if not tibero_module._DRIVER_JAR_PATH.exists():
        pytest.skip("번들 Tibero JDBC jar가 없어 건너뜀")

    try:
        tibero_module._ensure_jvm_started()
    except Exception as exc:  # noqa: BLE001 — JVM을 못 띄우는 환경이면 건너뜀
        pytest.skip(f"이 환경에서 JVM을 띄울 수 없음: {exc}")

    from java.sql import DriverManager  # noqa: E402 — JVM 기동 후에만 import 가능

    assert DriverManager is not None


def test_ping_includes_driver_warning_even_on_connection_failure(monkeypatch):
    """실제 JVM/JDBC 경로는 타지 않고 ping()의 예외 처리 로직만 검증한다."""
    adapter = tibero_module.TiberoAdapter(
        {"connection": {"host": "unreachable-host", "port": 8629}, "user": "u", "password": "p"}
    )
    monkeypatch.setattr(
        adapter,
        "execute_readonly",
        lambda *args, **kwargs: (_ for _ in ()).throw(tibero_module.DBConnectionError("boom")),
    )

    result = adapter.ping()
    assert result["connected"] is False
    assert "driver_warning" in result
    assert "Tibero 5.0" in result["driver_warning"]
