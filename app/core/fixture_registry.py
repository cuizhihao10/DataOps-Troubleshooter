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
    """保存已通过 Schema 校验且按 `scenario_id` 唯一索引的合成场景。

    注册表把磁盘解析限制在启动边界，MCP 工具执行阶段只读取不可变映射中的领域对象，
    从而获得确定性响应并避免每次调用重复 I/O。未知场景和空注册表都显式失败，不会退回
    一个看似成功的默认场景。
    """

    def __init__(self, scenarios: dict[str, ScenarioFixture]) -> None:
        """接收已校验场景映射，并拒绝无法提供任何工具响应的空注册表。

        参数键应由 `from_directory` 从模型中的 scenario_id 建立；构造器保留最小不变量检查，
        使测试或依赖注入直接构造注册表时也不能绕过“至少一个场景”的运行前提。
        """

        if not scenarios:
            raise ValueError("at least one scenario fixture is required")
        self._scenarios = scenarios

    @classmethod
    def from_directory(cls, directory: Path) -> FixtureRegistry:
        """从目录加载全部 JSON Fixture，完成解析、Schema 校验与唯一性检查。

        文件按名称排序以保证异常顺序和测试结果可重复；每份 JSON 先转成 `ScenarioFixture`，
        再以模型内的 ID 建索引。目录缺失、JSON 非法、Schema 不合法或 ID 重复都会直接抛出，
        防止 MCP 服务在运行中才发现合成数据损坏。
        """

        if not directory.is_dir():
            raise FileNotFoundError(f"fixture directory does not exist: {directory}")

        scenarios: dict[str, ScenarioFixture] = {}
        # 排序不是业务需要，而是为了让加载顺序、首个错误和演示重放结果跨平台稳定。
        for fixture_path in sorted(directory.glob("*.json")):
            payload = json.loads(fixture_path.read_text(encoding="utf-8"))
            scenario = ScenarioFixture.model_validate(payload)
            if scenario.scenario_id in scenarios:
                raise ValueError(f"duplicate scenario_id: {scenario.scenario_id}")
            scenarios[scenario.scenario_id] = scenario

        return cls(scenarios)

    def get(self, scenario_id: str) -> ScenarioFixture:
        """按稳定 ID 返回一个已校验场景，并把底层 KeyError 转成领域化错误。

        不提供模糊匹配或默认值，因为错误场景会让工具证据与用户问题错配。异常使用链式
        `raise ... from` 保留原始查找原因，便于日志调试且不会吞掉失败上下文。
        """

        try:
            return self._scenarios[scenario_id]
        except KeyError as exc:
            raise KeyError(f"unknown scenario_id: {scenario_id}") from exc

    @property
    def scenario_ids(self) -> tuple[str, ...]:
        """返回排序后的不可变场景 ID 快照，供健康检查和引用完整性验证使用。

        tuple 阻止调用方修改注册表内部状态，排序保证 API、测试快照和启动错误在不同文件
        系统遍历顺序下仍保持一致；该属性不暴露完整 Fixture 内容。
        """

        return tuple(sorted(self._scenarios))

    def __len__(self) -> int:
        """返回已加载场景数量，支持健康检查以公开可验证但不敏感的规模信息。

        方法直接委托字典长度，不触发磁盘读取或重新校验，因此可安全用于高频探针和测试
        断言；数量为零已在构造时被禁止。
        """

        return len(self._scenarios)

    def __iter__(self) -> Iterator[ScenarioFixture]:
        """按注册表插入顺序迭代已校验场景对象，不复制可能较大的响应内容。

        插入顺序来自排序后的文件加载，因此正常路径可重复；返回迭代器而不是内部字典，
        避免调用方接触索引结构或意外替换场景。
        """

        return iter(self._scenarios.values())


def load_golden_cases(path: Path) -> list[GoldenCaseSpec]:
    """加载 Golden Case 列表并同时验证元素 Schema 与 `case_id` 唯一性。

    `TypeAdapter` 用于校验顶层列表，因为它不是单独的 BaseModel；随后进行跨元素唯一性检查，
    防止评测聚合时后一个案例覆盖同名结果。文件或 JSON 错误保持原异常，重复 ID 则抛出
    明确 ValueError，调用方应在启动阶段将其视为不可恢复的基线问题。
    """

    if not path.is_file():
        raise FileNotFoundError(f"golden case file does not exist: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    cases = TypeAdapter(list[GoldenCaseSpec]).validate_python(payload)

    # Pydantic 负责单元素字段，集合负责只有跨整个列表才能判断的唯一性不变量。
    case_ids: set[str] = set()
    for case in cases:
        if case.case_id in case_ids:
            raise ValueError(f"duplicate golden case id: {case.case_id}")
        case_ids.add(case.case_id)
    return cases
