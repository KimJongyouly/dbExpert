"""server/main.py가 Plugin 루트의 .env를 실제로 auto-load하는지에 대한
회귀 테스트.

실제 겪은 문제: Claude Code가 MCP Server를 하위 프로세스로 띄울 때는
사용자의 로그인 쉘 프로파일을 거치지 않아, 거기서만 export해둔 환경변수를
서버 프로세스가 못 물려받았다 — 그 결과 ConnectionManager.load()가
자격증명을 하나도 못 찾아 서버 전체가 기동 직후 죽었다(Claude Code
쪽에는 "MCP 서버 연결 끊김"으로만 보임). server/main.py가 모듈 import
시점에 python-dotenv로 .env를 직접 읽게 해서, 쉘을 거치지 않아도 항상
같은 값을 보게 고쳤다.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path


def test_main_module_loads_dotenv_from_plugin_root(monkeypatch):
    captured: list[Path] = []

    import dotenv

    def fake_load_dotenv(path, *args, **kwargs):
        captured.append(Path(path))
        return False  # 실제로 os.environ을 바꾸지 않는다(다른 테스트에 영향 없게)

    monkeypatch.setattr(dotenv, "load_dotenv", fake_load_dotenv)

    # server.main이 이미 다른 테스트에서 import돼 있으면 모듈 최상단 코드가
    # 다시 실행되지 않으므로, 강제로 reload해서 load_dotenv 호출을 다시 태운다.
    if "server.main" in sys.modules:
        importlib.reload(sys.modules["server.main"])
    else:
        importlib.import_module("server.main")

    assert len(captured) == 1
    called_path = captured[0]
    assert called_path.name == ".env"
    # server/main.py 기준 부모의 부모 = Plugin 루트
    expected_root = Path(__file__).resolve().parent.parent
    assert called_path.parent == expected_root
