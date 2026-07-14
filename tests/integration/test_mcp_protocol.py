"""通过真实 stdio MCP 协议验证九工具、失败分类和重试 trace。

测试不调用服务端 Python 函数，而是完成 initialize、list_tools 和 call_tool。这样能够
发现伪协议实现、错误工具注解、结构化输出缺失和失败重试次数错误。
"""

import pytest

from app.domain.planner import ToolAction
from app.domain.tooling import ToolErrorCode, ToolName
from app.mcp.client import StdioMcpClient
from app.mcp.executor import McpToolExecutor


def _action(
    scenario_id: str,
    resource_id: str,
    trace_id: str,
    *,
    tool_name: str = "lts.get_task_status",
) -> ToolAction:
    """构造统一、已校验的 ToolAction，供真实 MCP 协议参数化测试复用。

    固定带时区时间窗并允许覆盖工具名、场景、资源和 trace，使每个测试只突出自己的协议行为；
    通过 `model_validate` 而非松散字典保证测试输入与生产 Planner Action 使用同一 Schema。
    """

    return ToolAction.model_validate(
        {
            "tool_name": tool_name,
            "arguments": {
                "resource_id": resource_id,
                "time_range": {
                    "start": "2026-07-10T00:00:00+08:00",
                    "end": "2026-07-10T03:00:00+08:00",
                },
                "scenario_id": scenario_id,
                "trace_id": trace_id,
            },
        }
    )


@pytest.mark.asyncio
async def test_real_mcp_protocol_lists_read_only_lts_tool() -> None:
    """验证独立 FastMCP 进程通过 list_tools 暴露完整九工具及统一安全注解。

    断言排序名称、数量、只读、非破坏、幂等和输出 Schema，能同时发现服务未启动、工具遗漏、
    静默改名或注解漂移；直接读取本地枚举无法证明真实协议注册，因此本测试必须跨 stdio 握手。
    """

    client = StdioMcpClient()

    assert await client.list_tools() == tuple(sorted(tool.value for tool in ToolName))
    descriptors = await client.list_tool_descriptors()
    assert len(descriptors) == 9
    assert all(descriptor.read_only for descriptor in descriptors)
    assert all(not descriptor.destructive for descriptor in descriptors)
    assert all(descriptor.idempotent for descriptor in descriptors)
    assert all(descriptor.has_output_schema for descriptor in descriptors)


@pytest.mark.asyncio
async def test_action_crosses_mcp_protocol_and_becomes_observation() -> None:
    """验证成功的 LTS Action 穿过真实 MCP 后生成响应、证据引用和单次 ToolEvent。

    测试从执行器入口出发，覆盖子进程启动、initialize、call_tool、Pydantic 响应校验和 Observation
    标准化；状态值、trace 与事件数共同证明结果来自指定 Fixture 且没有发生不必要重试。
    """

    executor = McpToolExecutor(StdioMcpClient(), retry_count=1)
    observation = await executor.execute(
        _action(
            "cross_chain_pk_conflict",
            "dws_order_report_daily",
            "trace_protocol_success_001",
        )
    )

    assert observation.response.ok is True
    assert observation.response.data["status"] == "failed"
    assert observation.tool_event.tool_name.value == "lts.get_task_status"
    assert observation.tool_event.trace_id == "trace_protocol_success_001"
    assert len(observation.tool_events) == 1
    assert observation.observation_refs


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "expected_data_key"),
    [
        ("lts.get_task_log", "component_error_code"),
        ("lts.get_dependency_topology", "upstream_task"),
    ],
)
async def test_remaining_lts_tools_cross_real_mcp_protocol(
    tool_name: str,
    expected_data_key: str,
) -> None:
    """参数化验证其余两个 LTS 工具也使用真实协议并返回各自结构化字段。

    每个工具复用同一场景和资源，但检查不同业务键，从而防止注册表把多个名称错误绑定到同一
    处理器；单事件和非空引用同时验证成功路径没有重试且证据已标准化。
    """

    executor = McpToolExecutor(StdioMcpClient(), retry_count=1)
    observation = await executor.execute(
        _action(
            "cross_chain_pk_conflict",
            "dws_order_report_daily",
            f"trace_{tool_name.replace('.', '_')}_001",
            tool_name=tool_name,
        )
    )

    assert observation.response.ok is True
    assert expected_data_key in observation.response.data
    assert len(observation.tool_events) == 1
    assert observation.observation_refs


@pytest.mark.asyncio
async def test_mcp_failure_response_is_preserved_without_fake_evidence() -> None:
    """验证 EMPTY_RESULT 作为非瞬时失败原样保留，且不会重试或生成伪 Evidence。

    场景通过真实协议返回结构化失败；执行器应产生一个不可重试 ToolEvent，但 evidence 与引用
    必须为空。该测试防止为了让报告“有依据”而把错误消息错误转换成已观察事实。
    """

    executor = McpToolExecutor(StdioMcpClient(), retry_count=1)
    observation = await executor.execute(
        _action(
            "lts_empty_result",
            "lts_inventory_snapshot_daily",
            "trace_protocol_empty_001",
        )
    )

    assert observation.response.ok is False
    assert observation.response.error_code is ToolErrorCode.EMPTY_RESULT
    assert observation.evidence == []
    assert observation.observation_refs == []
    assert observation.tool_event.retryable is False
    assert len(observation.tool_events) == 1


@pytest.mark.asyncio
async def test_transient_mcp_failure_retries_once_and_preserves_both_events() -> None:
    """验证 TIMEOUT 恰好重试一次，并保留两个具有不同稳定 ID 的失败事件。

    Fixture 对两次调用都返回瞬时错误，最终响应仍失败且无证据；attempt 序列 `[1, 2]` 证明预算
    没有少执行或无限循环，两个 event_id 证明成功/失败合并逻辑没有覆盖首次尝试。
    """

    executor = McpToolExecutor(StdioMcpClient(), retry_count=1)
    observation = await executor.execute(
        _action(
            "lts_empty_result",
            "lts_inventory_snapshot_daily",
            "trace_protocol_timeout_001",
            tool_name="lts.get_dependency_topology",
        )
    )

    assert observation.response.ok is False
    assert observation.response.error_code is ToolErrorCode.TIMEOUT
    assert observation.evidence == []
    assert [event.attempt for event in observation.tool_events] == [1, 2]
    assert all(event.retryable for event in observation.tool_events)
    assert observation.tool_events[0].event_id != observation.tool_events[1].event_id


@pytest.mark.asyncio
async def test_lts_all_observation_sources_fail_without_creating_evidence() -> None:
    """验证状态、日志和拓扑均不可用时，真实 MCP 仍保留各自精确失败语义。

    两个 EMPTY_RESULT 是稳定缺数，只生成一次不可重试事件；拓扑 TIMEOUT 是瞬时错误，按统一预算
    重试一次并保留两个事件。三项 Observation 都必须没有 Evidence，证明上层看到的是“调查已执行但
    没有事实”，而不是把错误消息包装成根因或把所有失败压缩成不可审计的单一异常。
    """

    executor = McpToolExecutor(StdioMcpClient(), retry_count=1)
    observations = {}
    # 三项调用复用相同执行器配置，却使用独立 trace，既验证统一重试策略，也避免事件 ID 互相覆盖。
    for tool_name in (
        "lts.get_task_status",
        "lts.get_task_log",
        "lts.get_dependency_topology",
    ):
        observations[tool_name] = await executor.execute(
            _action(
                "lts_empty_result",
                "lts_inventory_snapshot_daily",
                f"trace_lts_unavailable_{tool_name.replace('.', '_')}",
                tool_name=tool_name,
            )
        )

    # 先区分稳定缺数与瞬时超时，再检查各自事件数量；否则只断言 ok=false 会掩盖错误分类回归。
    status = observations["lts.get_task_status"]
    log = observations["lts.get_task_log"]
    topology = observations["lts.get_dependency_topology"]
    assert status.response.error_code is ToolErrorCode.EMPTY_RESULT
    assert log.response.error_code is ToolErrorCode.EMPTY_RESULT
    assert topology.response.error_code is ToolErrorCode.TIMEOUT
    assert len(status.tool_events) == 1
    assert len(log.tool_events) == 1
    assert [event.attempt for event in topology.tool_events] == [1, 2]
    assert status.tool_event.retryable is False
    assert log.tool_event.retryable is False
    assert all(event.retryable for event in topology.tool_events)
    # 失败事件可用于审计，但不得成为业务 Evidence；该断言守住“工具错误不等于根因事实”的边界。
    assert all(not observation.evidence for observation in observations.values())
    assert all(not observation.observation_refs for observation in observations.values())


@pytest.mark.asyncio
async def test_lts_parameter_failure_uses_dependency_readiness_as_counterevidence() -> None:
    """验证 LTS 参数错误的三项成功 Observation 能区分根因证据与依赖反证。

    状态只证明配置阶段重试耗尽，日志才给出 INVALID_PARTITION_DATE，拓扑则证明上游已就绪；三者
    必须通过真实 MCP 各产生稳定 Evidence。测试不在协议层直接输出根因，而是验证上层诊断所需的
    支持证据和反证同时存在，避免把任意 LTS 失败都归因于上游依赖。
    """

    executor = McpToolExecutor(StdioMcpClient(), retry_count=1)
    observations = {}
    # 独立 trace 保持每项工具事件可寻址；相同 scenario/resource 则证明三项事实来自同一诊断窗口。
    for tool_name in (
        "lts.get_task_status",
        "lts.get_task_log",
        "lts.get_dependency_topology",
    ):
        observations[tool_name] = await executor.execute(
            _action(
                "lts_parameter_validation_failure",
                "lts_finance_reconciliation_daily",
                f"trace_lts_parameter_{tool_name.replace('.', '_')}",
                tool_name=tool_name,
            )
        )

    status = observations["lts.get_task_status"]
    log = observations["lts.get_task_log"]
    topology = observations["lts.get_dependency_topology"]
    assert all(observation.response.ok for observation in observations.values())
    assert status.response.data["state"] == "FAILED"
    assert status.response.data["attempt"] == status.response.data["max_attempts"] == 3
    assert log.response.data["component_error_code"] == "INVALID_PARTITION_DATE"
    assert log.response.data["parameter_name"] == "partition_date"
    assert topology.response.data["upstream_ready"] is True
    assert topology.response.data["blocked_dependencies"] == []
    # source_id 集合锁定公开引用面，同时确认成功反证不会因“不支持候选根因”而被过滤掉。
    assert {
        observation.response.evidence[0].source_id for observation in observations.values()
    } == {
        "lts_status_finance_reconciliation_parameter",
        "lts_log_finance_reconciliation_parameter",
        "lts_topology_finance_reconciliation_ready",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "resource_id", "expected_data_key"),
    [
        ("bds.get_task_status", "bds_customer_profile_hourly", "cpu_percent"),
        ("bds.get_task_log", "bds_customer_profile_hourly", "spill_count"),
        ("bds.get_table_info", "dwd_customer_event", "latest_partition"),
    ],
)
async def test_bds_tools_cross_real_mcp_protocol(
    tool_name: str,
    resource_id: str,
    expected_data_key: str,
) -> None:
    """参数化验证 BDS 状态、日志和表信息三工具均跨真实 MCP 返回专属字段。

    不同资源 ID 与 expected_data_key 证明服务注册、参数传递和 Fixture 精确匹配正确；每个成功
    调用只保留一个事件并产生证据引用，工具层不需要 Planner 或数据库参与。
    """

    executor = McpToolExecutor(StdioMcpClient(), retry_count=1)
    observation = await executor.execute(
        _action(
            "bds_resource_pressure",
            resource_id,
            f"trace_{tool_name.replace('.', '_')}_001",
            tool_name=tool_name,
        )
    )

    assert observation.response.ok is True
    assert expected_data_key in observation.response.data
    assert len(observation.tool_events) == 1
    assert observation.observation_refs


@pytest.mark.asyncio
async def test_bds_permission_denied_is_not_retried_or_turned_into_evidence() -> None:
    """验证权限拒绝不会因重试预算而重复调用，也不会被包装成可信证据。

    PERMISSION_DENIED 属于稳定边界错误，重复请求无信息增益；断言单事件、retryable=False 和空证据
    防止执行器把所有失败一概重试，或让 Planner 将“无法读取”误解为表状态事实。
    """

    executor = McpToolExecutor(StdioMcpClient(), retry_count=1)
    observation = await executor.execute(
        _action(
            "bds_permission_denied",
            "dwd_sensitive_segment_mock",
            "trace_bds_permission_001",
            tool_name="bds.get_table_info",
        )
    )

    assert observation.response.ok is False
    assert observation.response.error_code is ToolErrorCode.PERMISSION_DENIED
    assert observation.evidence == []
    assert len(observation.tool_events) == 1
    assert observation.tool_event.retryable is False


@pytest.mark.asyncio
async def test_bds_data_skew_preserves_normal_volume_counterevidence() -> None:
    """验证 BDS 长尾场景同时返回热点分布证据和正常总量反证。

    状态确认尾部停滞但执行器在线，日志提供 9.6 倍热点分桶，表信息证明分区已就绪且行数落在
    基线区间。三项均通过真实 MCP 成功返回并保留 source_id，使上层能够区分数据倾斜、输入暴增、
    缺分区和资源丢失，而不是只凭“运行很慢”选择根因。
    """

    executor = McpToolExecutor(StdioMcpClient(), retry_count=1)
    resources = {
        "bds.get_task_status": "bds_customer_segment_daily",
        "bds.get_task_log": "bds_customer_segment_daily",
        "bds.get_table_info": "dwd_customer_segment_input",
    }
    observations = {}
    # 表工具使用数据集 ID、状态/日志使用任务 ID，保留真实工具边界而不是强行统一 resource_id。
    for tool_name, resource_id in resources.items():
        observations[tool_name] = await executor.execute(
            _action(
                "bds_data_skew",
                resource_id,
                f"trace_bds_skew_{tool_name.replace('.', '_')}",
                tool_name=tool_name,
            )
        )

    status = observations["bds.get_task_status"]
    log = observations["bds.get_task_log"]
    table = observations["bds.get_table_info"]
    assert all(observation.response.ok for observation in observations.values())
    assert status.response.data["progress_percent"] == 83
    assert status.response.data["active_executors"] == 16
    assert log.response.data["component_error_code"] == "DATA_SKEW_DETECTED"
    assert log.response.data["skew_ratio"] == pytest.approx(9.6)
    assert log.response.data["executor_lost"] == 0
    assert table.response.data["partition_ready"] is True
    assert (
        table.response.data["baseline_row_count_min"]
        <= table.response.data["row_count"]
        <= table.response.data["baseline_row_count_max"]
    )
    # 反证 source 与直接倾斜 source 同等可寻址，确保报告审计能检查排除过程。
    assert {
        observation.response.evidence[0].source_id
        for observation in observations.values()
    } == {
        "bds_status_customer_segment_long_tail",
        "bds_log_customer_segment_skew",
        "bds_table_customer_segment_normal_volume",
    }


@pytest.mark.asyncio
async def test_successful_bds_responses_preserve_conflicting_partition_facts_over_mcp() -> None:
    """验证三个成功 BDS 响应经真实 MCP 后仍保留互相矛盾的业务事实。

    测试逐一执行状态、日志和表信息工具，要求三者均 ``ok=true`` 且保留稳定 source ID；随后用结构化
    字段证明状态声称源未就绪，而日志和表元数据声称同一分区已读取且可查询。该用例刻意不让协议
    层“调和”冲突，因为事实裁决属于 Planner/Auditor，MCP 边界只负责忠实传输 Observation。
    """

    executor = McpToolExecutor(StdioMcpClient(), retry_count=1)
    calls = (
        ("bds.get_task_status", "bds_inventory_snapshot_hourly"),
        ("bds.get_task_log", "bds_inventory_snapshot_hourly"),
        ("bds.get_table_info", "dwd_inventory_snapshot"),
    )
    observations = {}
    for tool_name, resource_id in calls:
        # 每个调用使用独立 trace，便于协议失败时精确定位，同时不改变三者共享的场景事实窗口。
        observations[tool_name] = await executor.execute(
            _action(
                "bds_conflicting_partition_evidence",
                resource_id,
                f"trace_conflict_{tool_name.replace('.', '_')}",
                tool_name=tool_name,
            )
        )

    assert all(observation.response.ok for observation in observations.values())
    assert {
        observation.response.evidence[0].source_id
        for observation in observations.values()
    } == {
        "bds_conflict_status_inventory",
        "bds_conflict_log_inventory",
        "bds_conflict_table_inventory",
    }
    assert observations["bds.get_task_status"].response.data["source_ready"] is False
    assert observations["bds.get_task_log"].response.data["event"] == "SOURCE_READ_COMPLETED"
    assert observations["bds.get_table_info"].response.data["partition_queryable"] is True


@pytest.mark.asyncio
async def test_lts_to_bds_dependency_chain_crosses_real_mcp_protocol() -> None:
    """验证 LTS 失败现象可沿真实 MCP 工具结果追到 BDS 分区读取阻塞。

    五个调用覆盖 LTS 状态/拓扑和 BDS 状态/日志/表信息；断言使用结构化业务字段与稳定 source ID
    拼接“LTS 上游未就绪 → 依赖 BDS → BDS 等待缺失分区”。测试不直接读取 Fixture，也不让模型
    猜测组件关系，因此能发现协议注册、资源匹配或 Observation 标准化破坏跨组件证据链的问题。
    """

    executor = McpToolExecutor(StdioMcpClient(), retry_count=1)
    calls = (
        ("lts.get_task_status", "dws_order_report_daily"),
        ("lts.get_dependency_topology", "dws_order_report_daily"),
        ("bds.get_task_status", "bds_order_aggregate_daily"),
        ("bds.get_task_log", "bds_order_aggregate_daily"),
        ("bds.get_table_info", "ods_order_delta"),
    )
    observations = {}
    for tool_name, resource_id in calls:
        # 独立 trace 保留每项只读 Action 的审计身份，场景 ID 则让五次调用读取同一合成事实快照。
        observations[tool_name] = await executor.execute(
            _action(
                "cross_chain_pk_conflict",
                resource_id,
                f"trace_lts_bds_{tool_name.replace('.', '_')}",
                tool_name=tool_name,
            )
        )

    assert all(observation.response.ok for observation in observations.values())
    assert observations["lts.get_task_status"].response.data["upstream_ready"] is False
    assert (
        observations["lts.get_dependency_topology"].response.data["upstream_task"]
        == "bds_order_aggregate_daily"
    )
    assert observations["bds.get_task_status"].response.data["stage"] == "source_read"
    assert observations["bds.get_task_log"].response.data["warning"] == "SOURCE_PARTITION_LAG"
    table_data = observations["bds.get_table_info"].response.data
    assert table_data["latest_partition"] != table_data["expected_partition"]
    assert {
        observation.response.evidence[0].source_id
        for observation in observations.values()
    } == {
        "lts_status_dws_order_report_daily",
        "lts_topology_dws_order_report_daily",
        "bds_status_order_aggregate",
        "bds_log_order_aggregate",
        "bds_table_ods_order_delta",
    }


@pytest.mark.asyncio
async def test_bds_to_flashsync_root_cause_chain_crosses_real_mcp_protocol() -> None:
    """验证 BDS 分区等待可沿真实 MCP Observation 追到 FlashSync 主键冲突。

    测试调用 BDS 三工具与 FlashSync 三工具，先证明 BDS 停在 source_read 且目标分区落后，再证明同步
    吞吐为零、日志存在脱敏主键冲突且一致性差异与积压相等。所有判断来自结构化响应和稳定 source
    ID，既不直接读取 Fixture，也不把 GraphRAG 相似度单独当成实时根因事实。
    """

    executor = McpToolExecutor(StdioMcpClient(), retry_count=1)
    calls = (
        ("bds.get_task_status", "bds_order_aggregate_daily"),
        ("bds.get_task_log", "bds_order_aggregate_daily"),
        ("bds.get_table_info", "ods_order_delta"),
        ("flashsync.get_sync_delay", "ods_order_delta"),
        ("flashsync.get_sync_log", "ods_order_delta"),
        ("flashsync.check_consistency", "ods_order_delta"),
    )
    observations = {}
    for tool_name, resource_id in calls:
        # 同一场景保证事实窗口一致，独立 trace 则让六项跨组件 Action 均可单独审计和定位失败。
        observations[tool_name] = await executor.execute(
            _action(
                "cross_chain_pk_conflict",
                resource_id,
                f"trace_bds_flashsync_{tool_name.replace('.', '_')}",
                tool_name=tool_name,
            )
        )

    assert all(observation.response.ok for observation in observations.values())
    assert observations["bds.get_task_status"].response.data["stage"] == "source_read"
    table_data = observations["bds.get_table_info"].response.data
    assert table_data["latest_partition"] != table_data["expected_partition"]
    delay_data = observations["flashsync.get_sync_delay"].response.data
    assert delay_data["throughput_per_second"] == 0
    assert (
        observations["flashsync.get_sync_log"].response.data["component_error_code"]
        == "FS_PRIMARY_KEY_CONFLICT"
    )
    consistency_data = observations["flashsync.check_consistency"].response.data
    assert consistency_data["consistent"] is False
    assert consistency_data["difference_count"] == delay_data["backlog_records"]
    assert {
        observation.response.evidence[0].source_id
        for observation in observations.values()
    } == {
        "bds_status_order_aggregate",
        "bds_log_order_aggregate",
        "bds_table_ods_order_delta",
        "flashsync_delay_ods_order_delta",
        "flashsync_log_ods_order_delta",
        "flashsync_consistency_ods_order_delta",
    }


@pytest.mark.asyncio
async def test_lts_to_bds_resource_exhaustion_chain_crosses_real_mcp_protocol() -> None:
    """验证独立事实环境能从 LTS 超时追到 BDS 资源耗尽并排除输入异常。

    六个真实 MCP 调用必须共同证明：LTS 只因上游超时而失败，拓扑指向 BDS；BDS CPU/内存饱和且
    频繁 spill/丢执行器，但输入分区和数据量正常、倾斜不显著。该组合防止将所有 LTS→BDS 案例都
    套用缺分区根因，也验证新 Fixture 确实经过协议边界而非被 Golden runner 直接读取。
    """

    executor = McpToolExecutor(StdioMcpClient(), retry_count=1)
    calls = (
        ("lts.get_task_status", "dws_customer_profile_daily"),
        ("lts.get_task_log", "dws_customer_profile_daily"),
        ("lts.get_dependency_topology", "dws_customer_profile_daily"),
        ("bds.get_task_status", "bds_customer_profile_hourly"),
        ("bds.get_task_log", "bds_customer_profile_hourly"),
        ("bds.get_table_info", "dwd_customer_event"),
    )
    observations = {}
    for tool_name, resource_id in calls:
        # 六项 Action 共享合成场景时间窗，但每个 trace 独立，保留可观察协议调用身份。
        observations[tool_name] = await executor.execute(
            _action(
                "cross_lts_bds_resource_exhaustion",
                resource_id,
                f"trace_resource_chain_{tool_name.replace('.', '_')}",
                tool_name=tool_name,
            )
        )

    assert all(observation.response.ok for observation in observations.values())
    assert observations["lts.get_task_status"].response.data["upstream_ready"] is False
    assert (
        observations["lts.get_task_log"].response.data["component_error_code"]
        == "LTS_UPSTREAM_TIMEOUT"
    )
    assert (
        observations["lts.get_dependency_topology"].response.data["upstream_task"]
        == "bds_customer_profile_hourly"
    )
    status_data = observations["bds.get_task_status"].response.data
    assert status_data["cpu_percent"] == 99
    assert status_data["memory_percent"] == 97
    log_data = observations["bds.get_task_log"].response.data
    assert log_data["spill_count"] == 63
    assert log_data["executor_lost"] == 5
    assert log_data["skew_ratio"] < 1.2
    table_data = observations["bds.get_table_info"].response.data
    assert table_data["latest_partition"] == table_data["expected_partition"]
    assert abs(table_data["row_count"] - table_data["baseline_row_count"]) < 20_000
    assert {
        observation.response.evidence[0].source_id
        for observation in observations.values()
    } == {
        "lts_status_customer_profile_daily",
        "lts_log_customer_profile_daily",
        "lts_topology_customer_profile_daily",
        "bds_status_customer_profile_cross",
        "bds_log_customer_profile_cross",
        "bds_table_customer_event_cross",
    }


@pytest.mark.asyncio
async def test_flashsync_partial_evidence_preserves_symptoms_without_fake_cause() -> None:
    """验证症状工具成功而根因日志为空时，真实 MCP 保留部分证据和失败边界。

    延迟与一致性响应应各产生一条 Evidence，日志 EMPTY_RESULT 则只产生不可重试 ToolEvent、没有伪
    Evidence。差异数量与积压相等只能确认症状一致，不能让协议层编造 component_error_code；该边界
    为上层安全降级提供可审计输入。
    """

    executor = McpToolExecutor(StdioMcpClient(), retry_count=1)
    observations = {}
    for tool_name in (
        "flashsync.get_sync_delay",
        "flashsync.get_sync_log",
        "flashsync.check_consistency",
    ):
        observations[tool_name] = await executor.execute(
            _action(
                "flashsync_incomplete_root_cause_evidence",
                "ods_inventory_delta",
                f"trace_partial_{tool_name.replace('.', '_')}",
                tool_name=tool_name,
            )
        )

    delay = observations["flashsync.get_sync_delay"]
    log = observations["flashsync.get_sync_log"]
    consistency = observations["flashsync.check_consistency"]
    assert delay.response.ok is True
    assert consistency.response.ok is True
    assert delay.response.data["backlog_records"] == 74
    assert consistency.response.data["difference_count"] == 74
    assert log.response.ok is False
    assert log.response.error_code is ToolErrorCode.EMPTY_RESULT
    assert log.evidence == []
    assert len(log.tool_events) == 1
    assert log.tool_event.retryable is False
    assert {
        delay.response.evidence[0].source_id,
        consistency.response.evidence[0].source_id,
    } == {
        "flashsync_delay_inventory_incomplete",
        "flashsync_consistency_inventory_incomplete",
    }


@pytest.mark.asyncio
async def test_flashsync_checkpoint_regression_aligns_offset_and_consistency_gaps() -> None:
    """验证检查点回退的位点差、积压和目标缺失记录在真实 MCP 中一致。

    延迟工具给出当前/已提交 offset，日志明确旧快照恢复并阻止自动重放，一致性工具确认同量目标
    缺失且无重复。三项独立 Observation 必须共享 1200 的差值和稳定 source_id，防止上层把普通延迟、
    主键冲突或目标重复误报成检查点回退。
    """

    executor = McpToolExecutor(StdioMcpClient(), retry_count=1)
    observations = {}
    # 三项工具共享同一资源和事实窗口，但使用独立 trace 保证每个 ToolEvent 可单独审计。
    for tool_name in (
        "flashsync.get_sync_delay",
        "flashsync.get_sync_log",
        "flashsync.check_consistency",
    ):
        observations[tool_name] = await executor.execute(
            _action(
                "flashsync_checkpoint_regression",
                "ods_customer_status_delta",
                f"trace_checkpoint_{tool_name.replace('.', '_')}",
                tool_name=tool_name,
            )
        )

    delay = observations["flashsync.get_sync_delay"]
    log = observations["flashsync.get_sync_log"]
    consistency = observations["flashsync.check_consistency"]
    assert all(observation.response.ok for observation in observations.values())
    assert delay.response.data["offset_gap"] == 1200
    assert delay.response.data["backlog_records"] == 1200
    assert log.response.data["component_error_code"] == "CHECKPOINT_REGRESSION"
    assert log.response.data["automatic_replay_blocked"] is True
    assert consistency.response.data["missing_target_records"] == 1200
    assert consistency.response.data["duplicate_target_records"] == 0
    assert (
        delay.response.data["last_committed_offset"]
        - delay.response.data["current_offset"]
        == consistency.response.data["source_offset"]
        - consistency.response.data["target_offset"]
        == 1200
    )
    # 全部 source_id 都进入公开引用面，后续高风险建议才能同时引用症状、根因和影响范围。
    assert {
        observation.response.evidence[0].source_id
        for observation in observations.values()
    } == {
        "flashsync_delay_customer_status_checkpoint",
        "flashsync_log_customer_status_checkpoint",
        "flashsync_consistency_customer_status_checkpoint",
    }


@pytest.mark.asyncio
async def test_flashsync_schema_mapping_aligns_rejections_and_missing_records() -> None:
    """验证 Schema 映射滞后在真实 MCP 中形成版本差、拒绝数和缺失数闭环。

    延迟工具公开源 v12/映射 v11，日志给出 customer_tier 未映射和 600 条拒绝，一致性工具给出
    同量解析失败与目标缺失且无重复。三项均成功并保留 Evidence，防止把 Schema 漂移误判为主键
    冲突、检查点回退或普通吞吐波动。
    """

    executor = McpToolExecutor(StdioMcpClient(), retry_count=1)
    observations = {}
    # 相同资源/窗口保证数值可比较，独立 trace 保证三个 Observation 可分别审计。
    for tool_name in (
        "flashsync.get_sync_delay",
        "flashsync.get_sync_log",
        "flashsync.check_consistency",
    ):
        observations[tool_name] = await executor.execute(
            _action(
                "flashsync_schema_mapping_outdated",
                "ods_customer_profile_delta",
                f"trace_schema_{tool_name.replace('.', '_')}",
                tool_name=tool_name,
            )
        )

    delay = observations["flashsync.get_sync_delay"]
    log = observations["flashsync.get_sync_log"]
    consistency = observations["flashsync.check_consistency"]
    assert all(observation.response.ok for observation in observations.values())
    assert delay.response.data["source_schema_version"] == 12
    assert delay.response.data["mapping_schema_version"] == 11
    assert delay.response.data["backlog_records"] == 600
    assert log.response.data["component_error_code"] == "SCHEMA_MAPPING_OUTDATED"
    assert log.response.data["unmapped_fields"] == ["customer_tier"]
    assert log.response.data["rejected_records"] == 600
    assert consistency.response.data["schema_parse_failures"] == 600
    assert consistency.response.data["missing_target_records"] == 600
    assert consistency.response.data["duplicate_target_records"] == 0
    # 三个来源分别支持症状、直接根因和影响范围，报告不能只引用错误日志省略一致性验证。
    assert {
        observation.response.evidence[0].source_id
        for observation in observations.values()
    } == {
        "flashsync_delay_customer_profile_schema",
        "flashsync_log_customer_profile_schema",
        "flashsync_consistency_customer_profile_schema",
    }


@pytest.mark.asyncio
async def test_customer_profile_schema_failure_propagates_across_real_mcp_protocol() -> None:
    """验证同一 600 条 Schema 缺口经真实 MCP 从 FlashSync 传播到 BDS 和 LTS。

    六项只读调用必须共同证明：LTS 依赖拓扑指向 BDS/FlashSync；BDS 分区存在且资源正常，但
    5000 条预期输入只到达 4400 条；FlashSync v11 映射拒绝源 v12 的 600 条记录且无重复。
    数量、版本和稳定 source_id 同时闭合，防止 Golden runner 绕过协议或把 BDS 资源压力误报为根因。
    """

    executor = McpToolExecutor(StdioMcpClient(), retry_count=1)
    calls = (
        ("lts.get_task_status", "dws_customer_profile_daily"),
        ("lts.get_dependency_topology", "dws_customer_profile_daily"),
        ("bds.get_task_status", "bds_customer_profile_hourly"),
        ("bds.get_table_info", "ods_customer_profile_delta"),
        ("flashsync.get_sync_log", "ods_customer_profile_delta"),
        ("flashsync.check_consistency", "ods_customer_profile_delta"),
    )
    observations = {}
    for tool_name, resource_id in calls:
        # 每次调用使用独立 trace，既共享同一合成事实窗口，又让六个 ToolEvent 可分别审计。
        observations[tool_name] = await executor.execute(
            _action(
                "cross_customer_profile_schema_propagation",
                resource_id,
                f"trace_schema_cross_{tool_name.replace('.', '_')}",
                tool_name=tool_name,
            )
        )

    assert all(observation.response.ok for observation in observations.values())
    lts_status = observations["lts.get_task_status"].response.data
    topology = observations["lts.get_dependency_topology"].response.data
    bds_status = observations["bds.get_task_status"].response.data
    table = observations["bds.get_table_info"].response.data
    sync_log = observations["flashsync.get_sync_log"].response.data
    consistency = observations["flashsync.check_consistency"].response.data
    assert lts_status["upstream_ready"] is False
    assert topology["upstream_task"] == "bds_customer_profile_hourly"
    assert topology["source_sync_task"] == "flashsync_customer_profile_delta"
    assert bds_status["cpu_percent"] < 50
    assert bds_status["memory_percent"] < 50
    assert table["latest_partition"] == table["expected_partition"]
    assert sync_log["source_schema_version"] == 12
    assert sync_log["mapping_schema_version"] == 11
    assert sync_log["unmapped_fields"] == ["customer_tier"]
    assert consistency["duplicate_target_records"] == 0
    assert (
        lts_status["missing_upstream_records"]
        == bds_status["missing_records"]
        == table["missing_records"]
        == sync_log["rejected_records"]
        == consistency["missing_target_records"]
        == consistency["schema_parse_failures"]
        == 600
    )
    assert {
        observation.response.evidence[0].source_id
        for observation in observations.values()
    } == {
        "lts_status_customer_profile_schema_cross",
        "lts_topology_customer_profile_schema_cross",
        "bds_status_customer_profile_schema_cross",
        "bds_table_customer_profile_schema_cross",
        "flashsync_log_customer_profile_schema_cross",
        "flashsync_consistency_customer_profile_schema_cross",
    }


@pytest.mark.asyncio
async def test_checkpoint_regression_propagates_to_bds_across_real_mcp_protocol() -> None:
    """验证 1200 位点回退经真实 MCP 映射为 BDS 同量输入缺失和高风险同步根因。

    六项调用必须证明 BDS 分区存在、资源正常且倾斜不显著，但输入和物化位点各缺 1200；FlashSync
    的当前/提交位点差、积压、旧检查点日志和目标缺失也必须同为 1200 且零重复。这个多源闭环防止
    仅凭 BDS 缺数猜测缺分区，或仅凭同步延迟就宣称检查点回退。
    """

    executor = McpToolExecutor(StdioMcpClient(), retry_count=1)
    calls = (
        ("bds.get_task_status", "bds_customer_status_snapshot_hourly"),
        ("bds.get_task_log", "bds_customer_status_snapshot_hourly"),
        ("bds.get_table_info", "ods_customer_status_delta"),
        ("flashsync.get_sync_delay", "ods_customer_status_delta"),
        ("flashsync.get_sync_log", "ods_customer_status_delta"),
        ("flashsync.check_consistency", "ods_customer_status_delta"),
    )
    observations = {}
    for tool_name, resource_id in calls:
        # 同一合成窗口使用独立 trace，使每个跨组件 Observation 可单独审计且不会被视为重复 Action。
        observations[tool_name] = await executor.execute(
            _action(
                "cross_bds_flashsync_checkpoint_regression",
                resource_id,
                f"trace_checkpoint_cross_{tool_name.replace('.', '_')}",
                tool_name=tool_name,
            )
        )

    assert all(observation.response.ok for observation in observations.values())
    bds_status = observations["bds.get_task_status"].response.data
    bds_log = observations["bds.get_task_log"].response.data
    table = observations["bds.get_table_info"].response.data
    delay = observations["flashsync.get_sync_delay"].response.data
    sync_log = observations["flashsync.get_sync_log"].response.data
    consistency = observations["flashsync.check_consistency"].response.data
    assert bds_status["cpu_percent"] < 50
    assert bds_status["memory_percent"] < 50
    assert bds_log["skew_ratio"] < 1.2
    assert table["latest_partition"] == table["expected_partition"]
    assert sync_log["component_error_code"] == "CHECKPOINT_REGRESSION"
    assert sync_log["automatic_replay_blocked"] is True
    assert consistency["duplicate_target_records"] == 0
    assert (
        bds_status["missing_records"]
        == bds_log["offset_gap"]
        == table["missing_records"]
        == table["expected_offset"] - table["materialized_offset"]
        == delay["offset_gap"]
        == delay["last_committed_offset"] - delay["current_offset"]
        == delay["backlog_records"]
        == sync_log["previous_committed_offset"] - sync_log["restored_snapshot_offset"]
        == consistency["missing_target_records"]
        == consistency["source_offset"] - consistency["target_offset"]
        == 1200
    )
    assert {
        observation.response.evidence[0].source_id
        for observation in observations.values()
    } == {
        "bds_status_customer_status_checkpoint_cross",
        "bds_log_customer_status_checkpoint_cross",
        "bds_table_customer_status_checkpoint_cross",
        "flashsync_delay_customer_status_checkpoint_cross",
        "flashsync_log_customer_status_checkpoint_cross",
        "flashsync_consistency_customer_status_checkpoint_cross",
    }


@pytest.mark.asyncio
async def test_bds_data_skew_propagates_to_lts_across_real_mcp_protocol() -> None:
    """验证 BDS 热点长尾经真实 MCP 传播为 LTS 上游超时，并保留三项排除证据。

    LTS 状态/日志/拓扑必须证明本地执行未开始且依赖 BDS；BDS 状态/日志/表信息必须同时给出 9.6 倍
    热点、27 次 spill、在线执行器、正常资源、已就绪分区和基线内总量。这个组合区分数据倾斜与
    资源耗尽、输入暴增或缺分区，并证明 Golden runner 没有绕过 stdio MCP 读取 Fixture 答案。
    """

    executor = McpToolExecutor(StdioMcpClient(), retry_count=1)
    calls = (
        ("lts.get_task_status", "dws_customer_segment_daily"),
        ("lts.get_task_log", "dws_customer_segment_daily"),
        ("lts.get_dependency_topology", "dws_customer_segment_daily"),
        ("bds.get_task_status", "bds_customer_segment_daily"),
        ("bds.get_task_log", "bds_customer_segment_daily"),
        ("bds.get_table_info", "dwd_customer_event"),
    )
    observations = {}
    for tool_name, resource_id in calls:
        # 每项 Action 使用独立 trace；共享 scenario 只固定事实，不合并 ToolEvent 的审计身份。
        observations[tool_name] = await executor.execute(
            _action(
                "cross_lts_bds_data_skew",
                resource_id,
                f"trace_skew_cross_{tool_name.replace('.', '_')}",
                tool_name=tool_name,
            )
        )

    assert all(observation.response.ok for observation in observations.values())
    lts_status = observations["lts.get_task_status"].response.data
    lts_log = observations["lts.get_task_log"].response.data
    topology = observations["lts.get_dependency_topology"].response.data
    bds_status = observations["bds.get_task_status"].response.data
    bds_log = observations["bds.get_task_log"].response.data
    table = observations["bds.get_table_info"].response.data
    assert lts_status["upstream_ready"] is False
    assert lts_log["local_execution_started"] is False
    assert topology["upstream_task"] == "bds_customer_segment_daily"
    assert bds_status["executors_online"] == 16
    assert bds_status["cpu_percent"] < 80
    assert bds_status["memory_percent"] < 80
    assert bds_log["warning"] == "DATA_SKEW_DETECTED"
    assert bds_log["skew_ratio"] == 9.6
    assert bds_log["spill_count"] == 27
    assert bds_log["executor_lost"] == 0
    assert table["latest_partition"] == table["expected_partition"]
    assert table["baseline_row_count_min"] <= table["row_count"] <= table["baseline_row_count_max"]
    assert {
        observation.response.evidence[0].source_id
        for observation in observations.values()
    } == {
        "lts_status_customer_segment_skew_cross",
        "lts_log_customer_segment_skew_cross",
        "lts_topology_customer_segment_skew_cross",
        "bds_status_customer_segment_skew_cross",
        "bds_log_customer_segment_skew_cross",
        "bds_table_customer_segment_skew_cross",
    }


@pytest.mark.asyncio
async def test_target_write_throttle_propagates_across_three_components_via_real_mcp() -> None:
    """验证目标端限流经真实 MCP 形成 FlashSync→BDS→LTS 的三组件数量闭环。

    六项调用恰好使用默认 ReAct Action 预算：LTS 状态/拓扑负责定位传播链，BDS 状态/表信息证明
    分区存在且资源正常，FlashSync 延迟/日志提供低吞吐、2600 条积压和 TARGET_WRITE_THROTTLED
    直接根因。所有响应必须 ``ok=true``，避免把业务限流误当作 MCP 协议失败或重试语义。
    """

    executor = McpToolExecutor(StdioMcpClient(), retry_count=1)
    calls = (
        ("lts.get_task_status", "dws_revenue_dashboard_daily"),
        ("lts.get_dependency_topology", "dws_revenue_dashboard_daily"),
        ("bds.get_task_status", "bds_revenue_aggregate_hourly"),
        ("bds.get_table_info", "ods_payment_delta"),
        ("flashsync.get_sync_delay", "ods_payment_delta"),
        ("flashsync.get_sync_log", "ods_payment_delta"),
    )
    observations = {}
    for tool_name, resource_id in calls:
        # 独立 trace 保证每项协议调用均可审计；scenario 只共享确定性事实，不合并 ToolEvent。
        observations[tool_name] = await executor.execute(
            _action(
                "cross_lts_bds_flashsync_target_throttle",
                resource_id,
                f"trace_target_throttle_{tool_name.replace('.', '_')}",
                tool_name=tool_name,
            )
        )

    assert all(observation.response.ok for observation in observations.values())
    lts_status = observations["lts.get_task_status"].response.data
    topology = observations["lts.get_dependency_topology"].response.data
    bds_status = observations["bds.get_task_status"].response.data
    table = observations["bds.get_table_info"].response.data
    delay = observations["flashsync.get_sync_delay"].response.data
    sync_log = observations["flashsync.get_sync_log"].response.data
    assert lts_status["local_execution_started"] is False
    assert topology["upstream_task"] == "bds_revenue_aggregate_hourly"
    assert topology["source_sync_task"] == "flashsync_payment_delta"
    assert bds_status["cpu_percent"] < 80
    assert bds_status["memory_percent"] < 80
    assert table["latest_partition"] == table["expected_partition"]
    assert table["schema_compatible"] is True
    assert delay["source_read_healthy"] is True
    assert delay["throughput_rows_per_second"] < delay["baseline_throughput_rows_per_second"]
    assert sync_log["component_error_code"] == "TARGET_WRITE_THROTTLED"
    assert sync_log["target_quota_utilization_percent"] == 100
    assert sync_log["automatic_quota_change_blocked"] is True
    assert (
        lts_status["missing_upstream_records"]
        == bds_status["missing_records"]
        == bds_status["expected_records"] - bds_status["available_records"]
        == table["missing_records"]
        == table["expected_row_count"] - table["row_count"]
        == delay["backlog_records"]
        == 2600
    )
    assert {
        observation.response.evidence[0].source_id
        for observation in observations.values()
    } == {
        "lts_status_revenue_target_throttle_cross",
        "lts_topology_revenue_target_throttle_cross",
        "bds_status_revenue_target_throttle_cross",
        "bds_table_revenue_target_throttle_cross",
        "flashsync_delay_revenue_target_throttle_cross",
        "flashsync_log_revenue_target_throttle_cross",
    }


@pytest.mark.asyncio
async def test_source_authorization_expiry_propagates_via_real_mcp_without_secret() -> None:
    """验证业务授权过期经真实 MCP 传播，同时不把授权材料带入 Observation。

    六个协议响应都必须 ``ok=true``，表示 MCP 传输成功；FlashSync data 内的
    ``SOURCE_AUTHORIZATION_EXPIRED`` 才是业务失败。LTS/BDS/FlashSync 的 1800 条缺口必须闭合，
    目标写入健康用于排除上一案例的目标限流；日志只能公开合成租约 ID，并明确授权值未暴露。
    """

    executor = McpToolExecutor(StdioMcpClient(), retry_count=1)
    calls = (
        ("lts.get_task_status", "dws_settlement_summary_daily"),
        ("lts.get_dependency_topology", "dws_settlement_summary_daily"),
        ("bds.get_task_status", "bds_settlement_aggregate_hourly"),
        ("bds.get_table_info", "ods_settlement_delta"),
        ("flashsync.get_sync_delay", "ods_settlement_delta"),
        ("flashsync.get_sync_log", "ods_settlement_delta"),
    )
    observations = {}
    for tool_name, resource_id in calls:
        # 独立 trace 证明六次协议调用均可审计，且不会把一个授权异常复制成多次工具失败。
        observations[tool_name] = await executor.execute(
            _action(
                "cross_lts_bds_flashsync_source_auth_expired",
                resource_id,
                f"trace_source_auth_{tool_name.replace('.', '_')}",
                tool_name=tool_name,
            )
        )

    assert all(observation.response.ok for observation in observations.values())
    lts_status = observations["lts.get_task_status"].response.data
    topology = observations["lts.get_dependency_topology"].response.data
    bds_status = observations["bds.get_task_status"].response.data
    table = observations["bds.get_table_info"].response.data
    delay = observations["flashsync.get_sync_delay"].response.data
    sync_log = observations["flashsync.get_sync_log"].response.data
    assert lts_status["local_execution_started"] is False
    assert topology["upstream_task"] == "bds_settlement_aggregate_hourly"
    assert topology["source_sync_task"] == "flashsync_settlement_delta"
    assert bds_status["cpu_percent"] < 80
    assert bds_status["memory_percent"] < 80
    assert table["latest_partition"] == table["expected_partition"]
    assert table["schema_compatible"] is True
    assert delay["source_read_healthy"] is False
    assert delay["target_write_healthy"] is True
    assert delay["throughput_rows_per_second"] == 0
    assert sync_log["component_error_code"] == "SOURCE_AUTHORIZATION_EXPIRED"
    assert sync_log["authorization_expired"] is True
    assert sync_log["automatic_authorization_rotation_blocked"] is True
    assert sync_log["authorization_value_exposed"] is False
    assert sync_log["authorization_lease_id"].startswith("synthetic_lease_")
    assert (
        lts_status["missing_upstream_records"]
        == bds_status["missing_records"]
        == bds_status["expected_records"] - bds_status["available_records"]
        == table["missing_records"]
        == table["expected_row_count"] - table["row_count"]
        == delay["backlog_records"]
        == sync_log["read_rejected_records"]
        == 1800
    )
    assert {
        observation.response.evidence[0].source_id
        for observation in observations.values()
    } == {
        "lts_status_settlement_source_auth_cross",
        "lts_topology_settlement_source_auth_cross",
        "bds_status_settlement_source_auth_cross",
        "bds_table_settlement_source_auth_cross",
        "flashsync_delay_settlement_source_auth_cross",
        "flashsync_log_settlement_source_auth_cross",
    }


@pytest.mark.asyncio
async def test_watermark_timezone_mismatch_exposes_silent_loss_via_real_mcp() -> None:
    """验证水位线时区错配经真实 MCP 形成三组件 900 条静默漏数闭环。

    六个响应都为 ``ok=true``，因为协议成功和同步进程结束都不能证明数据完整；LTS 质量门禁、BDS
    记录校验、FlashSync 错误码与一致性抽检必须独立给出同一 900 条差异。分区、Schema、资源和零
    重复作为反证，避免把漏数误归因于缺分区、字段映射、资源压力或重复写入。
    """

    executor = McpToolExecutor(StdioMcpClient(), retry_count=1)
    calls = (
        ("lts.get_task_status", "dws_order_fulfillment_daily"),
        ("lts.get_dependency_topology", "dws_order_fulfillment_daily"),
        ("bds.get_task_status", "bds_order_fulfillment_aggregate_hourly"),
        ("bds.get_table_info", "ods_order_event_delta"),
        ("flashsync.get_sync_log", "ods_order_event_delta"),
        ("flashsync.check_consistency", "ods_order_event_delta"),
    )
    observations = {}
    for tool_name, resource_id in calls:
        # 独立 trace 让每项跨组件事实都能回溯到真实协议调用，而不是由测试在内存中拼接响应。
        observations[tool_name] = await executor.execute(
            _action(
                "cross_lts_bds_flashsync_watermark_timezone_mismatch",
                resource_id,
                f"trace_watermark_timezone_{tool_name.replace('.', '_')}",
                tool_name=tool_name,
            )
        )

    assert all(observation.response.ok for observation in observations.values())
    lts_status = observations["lts.get_task_status"].response.data
    topology = observations["lts.get_dependency_topology"].response.data
    bds_status = observations["bds.get_task_status"].response.data
    table = observations["bds.get_table_info"].response.data
    sync_log = observations["flashsync.get_sync_log"].response.data
    consistency = observations["flashsync.check_consistency"].response.data
    assert lts_status["local_execution_started"] is False
    assert lts_status["data_quality_gate_passed"] is False
    assert topology["upstream_task"] == "bds_order_fulfillment_aggregate_hourly"
    assert topology["source_sync_task"] == "flashsync_order_event_delta"
    assert bds_status["cpu_percent"] < 80
    assert bds_status["memory_percent"] < 80
    assert table["latest_partition"] == table["expected_partition"]
    assert table["schema_compatible"] is True
    assert sync_log["component_error_code"] == "WATERMARK_TIMEZONE_MISMATCH"
    assert sync_log["source_event_timezone"] == "UTC"
    assert sync_log["configured_watermark_timezone"] == "Asia/Shanghai"
    assert sync_log["timezone_offset_minutes"] == 480
    assert sync_log["automatic_watermark_change_blocked"] is True
    assert consistency["consistent"] is False
    assert consistency["duplicate_on_target"] == 0
    assert (
        lts_status["missing_upstream_records"]
        == bds_status["missing_records"]
        == bds_status["expected_records"] - bds_status["available_records"]
        == table["missing_records"]
        == table["expected_row_count"] - table["row_count"]
        == sync_log["skipped_records"]
        == consistency["missing_on_target"]
        == consistency["source_count"] - consistency["target_count"]
        == consistency["timezone_boundary_mismatch_records"]
        == 900
    )
    assert {
        observation.response.evidence[0].source_id
        for observation in observations.values()
    } == {
        "lts_status_order_watermark_timezone_cross",
        "lts_topology_order_watermark_timezone_cross",
        "bds_status_order_watermark_timezone_cross",
        "bds_table_order_watermark_timezone_cross",
        "flashsync_log_order_watermark_timezone_cross",
        "flashsync_consistency_order_watermark_timezone_cross",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "expected_data_key"),
    [
        ("flashsync.get_sync_delay", "delay_seconds"),
        ("flashsync.get_sync_log", "component_error_code"),
        ("flashsync.check_consistency", "consistent"),
    ],
)
async def test_flashsync_tools_cross_real_mcp_protocol(
    tool_name: str,
    expected_data_key: str,
) -> None:
    """参数化验证 FlashSync 延迟、日志和一致性工具的真实协议映射。

    三个名称分别检查不同结构化数据键，能发现处理器绑定错位；成功结果必须只有一次事件并带
    Observation 引用，证明只读同步观察完整通过客户端和标准化边界。
    """

    executor = McpToolExecutor(StdioMcpClient(), retry_count=1)
    observation = await executor.execute(
        _action(
            "cross_chain_pk_conflict",
            "ods_order_delta",
            f"trace_{tool_name.replace('.', '_')}_001",
            tool_name=tool_name,
        )
    )

    assert observation.response.ok is True
    assert expected_data_key in observation.response.data
    assert len(observation.tool_events) == 1
    assert observation.observation_refs


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "error_code"),
    [
        ("flashsync.get_sync_delay", ToolErrorCode.TIMEOUT),
        ("flashsync.get_sync_log", ToolErrorCode.SERVICE_UNAVAILABLE),
    ],
)
async def test_flashsync_transient_failures_retry_once_without_fake_evidence(
    tool_name: str,
    error_code: ToolErrorCode,
) -> None:
    """验证 FlashSync 的 TIMEOUT 与 SERVICE_UNAVAILABLE 都只重试一次且不制造证据。

    参数化覆盖两个批准瞬时错误，最终 error_code 必须与 Fixture 一致；两个 retryable 事件证明
    重试审计完整，空 evidence 则保证知识降级或 Planner 后续不能声称实时同步工具已确认根因。
    """

    executor = McpToolExecutor(StdioMcpClient(), retry_count=1)
    observation = await executor.execute(
        _action(
            "flashsync_timeout",
            "ods_payment_delta",
            f"trace_{tool_name.replace('.', '_')}_failure_001",
            tool_name=tool_name,
        )
    )

    assert observation.response.ok is False
    assert observation.response.error_code is error_code
    assert observation.evidence == []
    assert [event.attempt for event in observation.tool_events] == [1, 2]
    assert all(event.retryable for event in observation.tool_events)
