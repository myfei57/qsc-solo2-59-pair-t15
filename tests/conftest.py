"""测试夹具：所有用例都从干净存储和可推进时钟起步。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from batterystore.app import build_platform  # noqa: E402
from batterystore.clock import ManualClock  # noqa: E402
from batterystore.config import StationConfig, default_config  # noqa: E402
from dataclasses import replace as dc_replace  # noqa: E402


@pytest.fixture()
def clock() -> ManualClock:
    return ManualClock()


@pytest.fixture()
def config():
    return default_config()


def _with_bms(config, **bms_overrides):
    return dc_replace(config, bms=dc_replace(config.bms, **bms_overrides))


@pytest.fixture()
def platform_factory(tmp_path, clock):
    """造平台的工厂，允许覆盖 BMS 参数（如恢复策略、停留时长）。"""

    def _build(rack_count: int = 2, **bms_overrides):
        config = default_config()
        if bms_overrides:
            config = _with_bms(config, **bms_overrides)
        return build_platform(config, clock=clock, storage_root=str(tmp_path / "state"), rack_count=rack_count)

    return _build


@pytest.fixture()
def platform(platform_factory):
    return platform_factory()
