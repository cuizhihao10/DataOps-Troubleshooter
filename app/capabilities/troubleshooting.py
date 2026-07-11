"""声明单组件诊断与跨组件链路溯源的固定调查策略。

两个定义只提供 Planner 约束和九个只读 MCP 工具的建议顺序。实际 Action 仍由 Planner
逐次生成并经执行器校验，定义本身不会调用工具、读取 Fixture 或伪造 Observation。
"""

from app.capabilities.base import (
    CapabilityDefinition,
    CapabilityInputField,
    CapabilityName,
)
from app.domain.tooling import ToolName

# 单组件定义列出三个组件的完整工具组；注册表会按已校验的唯一组件过滤对应前缀。
SINGLE_COMPONENT_DIAGNOSIS = CapabilityDefinition(
    name=CapabilityName.SINGLE_COMPONENT_DIAGNOSIS,
    summary="围绕一个明确组件收集当前状态、日志与组件特有元数据，以最小只读调查面验证候选根因。",
    prompt_fragment="""仅调查 components 中唯一的组件。先选择最能确认当前状态的只读工具，
再按证据缺口读取日志或组件特有元数据；每轮只能提出一个结构化 Action。不得把相似文本、
工具失败或其他组件的历史现象当成本次实时事实。若白名单工具无法取得关键证据，
应降低置信度并明确缺失项，而不是扩大组件范围或编造 Observation。""",
    tool_priority=(
        ToolName.LTS_GET_TASK_STATUS,
        ToolName.LTS_GET_TASK_LOG,
        ToolName.LTS_GET_DEPENDENCY_TOPOLOGY,
        ToolName.BDS_GET_TASK_STATUS,
        ToolName.BDS_GET_TASK_LOG,
        ToolName.BDS_GET_TABLE_INFO,
        ToolName.FLASHSYNC_GET_SYNC_DELAY,
        ToolName.FLASHSYNC_GET_SYNC_LOG,
        ToolName.FLASHSYNC_CHECK_CONSISTENCY,
    ),
    required_inputs=(
        CapabilityInputField.USER_QUERY,
        CapabilityInputField.COMPONENTS,
        CapabilityInputField.RESOURCE_ID,
        CapabilityInputField.TIME_RANGE,
        CapabilityInputField.SCENARIO_ID,
        CapabilityInputField.TRACE_ID,
        CapabilityInputField.REALTIME_OBSERVATIONS,
    ),
    output_validation_rules=(
        "活动调查范围必须只包含请求中的唯一组件，除非新实时证据明确支持升级为跨组件意图。",
        "每项根因结论必须引用本次工具 Observation 或可追溯知识证据。",
        "工具失败或空结果只能形成证据缺口、降级置信度或补参请求，不能形成已确认根因。",
    ),
)

# 跨组件顺序先建立 LTS 拓扑，再沿 BDS 和 FlashSync 观察验证传播链，而非按文本相似度拼链。
CROSS_COMPONENT_CHAIN_TRACING = CapabilityDefinition(
    name=CapabilityName.CROSS_COMPONENT_CHAIN_TRACING,
    summary="结合实时组件观察与 GraphRAG 显式路径，验证 LTS、BDS、FlashSync 之间的故障传播链。",
    prompt_fragment="""围绕 components 中的多组件范围调查传播关系。优先确认上游状态和依赖拓扑，
再沿 LTS、BDS、FlashSync 的候选链逐段取得实时 Observation，并用 retrieved_paths 补充
可引用关系。故障链中的每个节点和边都必须有 evidence_id 或 path_id；图路径只能提出候选连接，
不能覆盖冲突的实时工具结果。缺少某一段证据时应明确标记链路不完整。""",
    tool_priority=(
        ToolName.LTS_GET_TASK_STATUS,
        ToolName.LTS_GET_DEPENDENCY_TOPOLOGY,
        ToolName.LTS_GET_TASK_LOG,
        ToolName.BDS_GET_TASK_STATUS,
        ToolName.BDS_GET_TASK_LOG,
        ToolName.BDS_GET_TABLE_INFO,
        ToolName.FLASHSYNC_GET_SYNC_DELAY,
        ToolName.FLASHSYNC_GET_SYNC_LOG,
        ToolName.FLASHSYNC_CHECK_CONSISTENCY,
    ),
    required_inputs=(
        CapabilityInputField.USER_QUERY,
        CapabilityInputField.COMPONENTS,
        CapabilityInputField.RESOURCE_ID,
        CapabilityInputField.TIME_RANGE,
        CapabilityInputField.SCENARIO_ID,
        CapabilityInputField.TRACE_ID,
        CapabilityInputField.CURRENT_HYPOTHESES,
        CapabilityInputField.REALTIME_OBSERVATIONS,
        CapabilityInputField.RETRIEVED_PATHS,
    ),
    output_validation_rules=(
        "故障传播链的每个节点和关系边必须关联有效 evidence_id 或 path_id。",
        "GraphRAG 路径与实时 Observation 冲突时必须暴露差异并服从实时 Observation。",
        "任一必要链路段缺失证据时必须降低链路完整性和根因置信度。",
    ),
)
