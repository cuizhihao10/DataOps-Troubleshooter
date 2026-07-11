"""公开运行时领域能力的固定契约与注册表入口。

本包只描述 Planner 可复用的调查策略，不执行模型、MCP、数据库或工作流调用。调用方通过
默认注册表获得版本化选择结果，从而把 capability 与仓库级 Codex Skill、动态插件系统隔离。
"""

from app.capabilities.base import (
    CAPABILITY_CONTRACT_ID,
    CapabilityDefinition,
    CapabilityInputField,
    CapabilityName,
    CapabilitySelection,
    CapabilitySelectionRequest,
    DiagnosisIntent,
    HistoryTrigger,
)
from app.capabilities.registry import CapabilityRegistry, get_capability_registry

__all__ = [
    "CAPABILITY_CONTRACT_ID",
    "CapabilityDefinition",
    "CapabilityInputField",
    "CapabilityName",
    "CapabilityRegistry",
    "CapabilitySelection",
    "CapabilitySelectionRequest",
    "DiagnosisIntent",
    "HistoryTrigger",
    "get_capability_registry",
]
