"""实现五项固定运行时 capability 的确定性选择与合并。

注册表没有动态加载入口：定义集合在构造时由代码固定并执行完整性审计。选择过程只根据强类型
意图、组件范围和历史触发条件组合数据，不执行 I/O，因此可在启动时和单元测试中完全重放。
"""

from __future__ import annotations

from functools import lru_cache
from types import MappingProxyType
from typing import TypeVar

from app.capabilities.base import (
    CAPABILITY_CONTRACT_ID,
    CapabilityDefinition,
    CapabilityName,
    CapabilitySelection,
    CapabilitySelectionRequest,
    DiagnosisIntent,
    HistoryTrigger,
)
from app.capabilities.history import HISTORY_CASE_MATCHING
from app.capabilities.reporting import RISK_ASSESSMENT, STRUCTURED_REPORTING
from app.capabilities.troubleshooting import (
    CROSS_COMPONENT_CHAIN_TRACING,
    SINGLE_COMPONENT_DIAGNOSIS,
)
from app.domain.models import Component
from app.domain.tooling import ToolName

_Item = TypeVar("_Item")

_FIXED_DEFINITIONS = (
    SINGLE_COMPONENT_DIAGNOSIS,
    CROSS_COMPONENT_CHAIN_TRACING,
    HISTORY_CASE_MATCHING,
    RISK_ASSESSMENT,
    STRUCTURED_REPORTING,
)


def _stable_unique(items: tuple[_Item, ...]) -> tuple[_Item, ...]:
    """按首次出现顺序去重多个 capability 合并出的可哈希配置项。

    输入是工具、枚举或规则字符串元组，输出保持声明优先级的去重元组。该函数不排序，因为排序
    会破坏调查优先级；所有当前项都可哈希，若未来传入不可哈希对象会显式抛出 TypeError。
    """

    seen: set[_Item] = set()
    result: list[_Item] = []
    for item in items:
        # 只有第一次出现时才保留，既合并共享输入又不改变前一个能力声明的优先级。
        if item not in seen:
            seen.add(item)
            result.append(item)
    return tuple(result)


class CapabilityRegistry:
    """持有并审计产品批准的五项固定 capability 定义。

    构造函数不接受外部定义，避免把 registry 误用为插件注入点。`select` 根据已校验请求生成冻结
    的 Planner 上下文；任何定义缺失、重复或意图边界错误都会在启动或请求校验阶段显式失败。
    """

    def __init__(self) -> None:
        """构建只读名称映射并验证集合与产品基线完全一致。

        输入为空，输出是内部不可变映射；若代码误删、复制或增加 capability，构造立即抛出
        ValueError，FastAPI lifespan 因而拒绝发布一个部分可用或未经批准的运行时。
        """

        definitions_by_name = {definition.name: definition for definition in _FIXED_DEFINITIONS}
        expected_names = set(CapabilityName)
        # 字典会覆盖重复键，因此先比较长度，才能同时识别重复定义和缺失定义。
        if len(definitions_by_name) != len(_FIXED_DEFINITIONS):
            raise ValueError("runtime capability definitions contain duplicate names")
        if set(definitions_by_name) != expected_names:
            raise ValueError("runtime capability definitions must match the fixed product baseline")
        self._definitions = MappingProxyType(definitions_by_name)

    @property
    def contract_id(self) -> str:
        """返回当前 capability 选择结果采用的稳定契约版本。

        属性没有外部输入且不会改变状态；调用方应记录返回值而不是硬编码。若字段或合并语义发生
        不兼容改变，需要提升常量版本并同步健康检查、Prompt 契约和测试。
        """

        return CAPABILITY_CONTRACT_ID

    def definitions(self) -> tuple[CapabilityDefinition, ...]:
        """按产品固定顺序返回全部只读能力定义。

        返回元组而不是内部映射，既保留演示和健康检查的稳定顺序，又防止调用方增删注册项；
        元素自身也是冻结 Pydantic 模型，因此读取操作不会改变后续选择结果。
        """

        return tuple(self._definitions[name] for name in CapabilityName)

    def get(self, name: CapabilityName) -> CapabilityDefinition:
        """按强类型名称取得一项冻结能力定义。

        输入必须先通过 `CapabilityName`，返回注册表中的唯一对象；无效字符串会在枚举构造阶段
        失败，缺失合法名称则暴露内部完整性错误，而不会静默返回空策略。
        """

        return self._definitions[name]

    def select(self, request: CapabilitySelectionRequest) -> CapabilitySelection:
        """按意图、组件范围和历史触发条件合并 Planner 能力上下文。

        单组件或跨组件调查能力首先加入，历史能力仅在显式触发时追加，风险和报告始终收尾。
        输出稳定去重工具、输入和规则；函数不执行任何 I/O，输入非法时由请求模型提前失败。
        """

        if request.intent is DiagnosisIntent.SINGLE_COMPONENT_DIAGNOSIS:
            primary_name = CapabilityName.SINGLE_COMPONENT_DIAGNOSIS
        else:
            primary_name = CapabilityName.CROSS_COMPONENT_CHAIN_TRACING

        active_names = [primary_name]
        if request.history_trigger is not HistoryTrigger.NOT_REQUESTED:
            active_names.append(CapabilityName.HISTORY_CASE_MATCHING)
        # 风险与报告是每次诊断都必须执行的横切约束，顺序固定在调查策略之后。
        active_names.extend((CapabilityName.RISK_ASSESSMENT, CapabilityName.STRUCTURED_REPORTING))
        definitions = tuple(self.get(name) for name in active_names)

        tool_priority = _stable_unique(
            tuple(tool for definition in definitions for tool in definition.tool_priority)
        )
        # 无论单组件还是跨组件，都裁剪到路由已经批准的范围，避免优先级诱导 Planner 越界调查。
        allowed_components = set(request.components)
        tool_priority = tuple(
            tool
            for tool in tool_priority
            if any(_tool_belongs_to_component(tool, component) for component in allowed_components)
        )

        required_inputs = _stable_unique(
            tuple(item for definition in definitions for item in definition.required_inputs)
        )
        output_rules = _stable_unique(
            tuple(rule for definition in definitions for rule in definition.output_validation_rules)
        )
        prompt_fragments = tuple(
            f"[{definition.name.value}]\n{definition.prompt_fragment}" for definition in definitions
        )

        return CapabilitySelection(
            contract_id=self.contract_id,
            intent=request.intent,
            components=request.components,
            history_trigger=request.history_trigger,
            active_capabilities=tuple(active_names),
            prompt_fragments=prompt_fragments,
            tool_priority=tool_priority,
            required_inputs=required_inputs,
            output_validation_rules=output_rules,
        )


def _tool_belongs_to_component(tool: ToolName, component: Component) -> bool:
    """判断一个已白名单化工具是否属于目标组件命名空间。

    工具名契约固定为 `<component>.<operation>`，因此前缀比较可复用九个枚举且无需维护第二张
    易漂移映射表。输入均为枚举；若未来命名规则改变，工具契约测试和本函数测试会共同失败。
    """

    return tool.value.startswith(f"{component.value}.")


@lru_cache(maxsize=1)
def get_capability_registry() -> CapabilityRegistry:
    """构造并缓存进程级固定 capability 注册表。

    注册表无可变运行状态，缓存避免 lifespan 与路由重复审计同一组定义，并确保各节点观察一致
    契约。测试若需验证全新构造，可直接实例化 `CapabilityRegistry` 而不修改缓存对象。
    """

    return CapabilityRegistry()
