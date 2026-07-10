"""合成故障场景与 Golden Case 的严格加载器。

注册表在启动阶段一次性解析 JSON 并执行 Pydantic 校验，同时拒绝重复场景和悬空引用。
这样 MCP 服务只读取已验证对象，测试与演示也能依靠 scenario_id 获得确定性结果。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

from pydantic import TypeAdapter

from app.domain.scenarios import GoldenCaseSpec, ScenarioFixture


class FixtureRegistry:
    def __init__(self, scenarios: dict[str, ScenarioFixture]) -> None:
        if not scenarios:
            raise ValueError("at least one scenario fixture is required")
        self._scenarios = scenarios

    @classmethod
    def from_directory(cls, directory: Path) -> FixtureRegistry:
        if not directory.is_dir():
            raise FileNotFoundError(f"fixture directory does not exist: {directory}")

        scenarios: dict[str, ScenarioFixture] = {}
        for fixture_path in sorted(directory.glob("*.json")):
            payload = json.loads(fixture_path.read_text(encoding="utf-8"))
            scenario = ScenarioFixture.model_validate(payload)
            if scenario.scenario_id in scenarios:
                raise ValueError(f"duplicate scenario_id: {scenario.scenario_id}")
            scenarios[scenario.scenario_id] = scenario

        return cls(scenarios)

    def get(self, scenario_id: str) -> ScenarioFixture:
        try:
            return self._scenarios[scenario_id]
        except KeyError as exc:
            raise KeyError(f"unknown scenario_id: {scenario_id}") from exc

    @property
    def scenario_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._scenarios))

    def __len__(self) -> int:
        return len(self._scenarios)

    def __iter__(self) -> Iterator[ScenarioFixture]:
        return iter(self._scenarios.values())


def load_golden_cases(path: Path) -> list[GoldenCaseSpec]:
    if not path.is_file():
        raise FileNotFoundError(f"golden case file does not exist: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    cases = TypeAdapter(list[GoldenCaseSpec]).validate_python(payload)

    case_ids: set[str] = set()
    for case in cases:
        if case.case_id in case_ids:
            raise ValueError(f"duplicate golden case id: {case.case_id}")
        case_ids.add(case.case_id)
    return cases
