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
