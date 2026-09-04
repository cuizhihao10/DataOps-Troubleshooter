"""通过真实 Streamable HTTP 网关验证生产 MCP 传输的契约、鉴权与失败分类。

生产形态下 client↔server 那一跳要过网络，因此本文件在临时端口上启动一个真实 uvicorn 网关，而不是
用 `ASGITransport` 在内存里直连应用：鉴权中间件的真实位置、TCP 连接被拒、跨进程 JSON-RPC 往返恰好
就是这条传输新增的失败面，内存替身会把它们全部抹掉。九工具与注解断言与 `test_mcp_protocol.py` 逐字
相同，两者因此构成"契约与传输无关"的对照组；stdio 不再新增用例，新能力只覆盖 HTTP。
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import httpx
import pytest
import uvicorn
from pydantic import SecretStr

from app.agents.planner import PlannerTurnContext
from app.api.security import ApiSecurityGuard
from app.capabilities import CapabilitySelectionRequest, DiagnosisIntent
from app.domain.models import AgentState, Component
from app.domain.planner import PlannerDecision, ToolAction
from app.domain.tooling import ToolErrorCode, ToolName
from app.mcp.executor import McpToolExecutor
from app.mcp.protocol import McpClientError
from app.mcp.streamable_http import StreamableHttpMcpClient
from app.orchestration import (
    BoundedReactLoop,
    ReactEventType,
    ReactLoopConfig,
    ReactRunRequest,
)
from mcp_server import healthcheck
from mcp_server.security import McpGatewaySecurityMiddleware
from mcp_server.server import mcp

# 令牌取 35 字符以越过 `ApiSecurityGuard` 的 32 字符下限：鉴权用例必须走生产同一套强度校验，
# 用 "secret" 这类短口令会在守卫构造期就被拒绝，从而把"错令牌被拒"退化成"测试写错了"。
GATEWAY_TOKEN = "test-gateway-token-0123456789abcdef"
WRONG_TOKEN = "test-gateway-token-fedcba9876543210"

TIME_RANGE = {
    "start": "2026-07-10T00:00:00+08:00",
    "end": "2026-07-10T03:00:00+08:00",
}


def _action(
    trace_id: str,
    *,
    tool_name: str = "lts.get_task_status",
    scenario_id: str = "cross_chain_pk_conflict",
    resource_id: str = "dws_order_report_daily",
) -> ToolAction:
    """构造一个已通过 Schema 校验的 ToolAction，供 HTTP 传输用例复用。

    走 `ToolAction.model_validate` 而不是松散字典：测试输入必须与生产 Planner 输出同一 Schema，
    否则"HTTP 能调通"可能只是因为测试绕过了参数校验。默认指向跨链场景里必然返回 failed 的 LTS
    任务，让断言可以检查具体业务字段而不是仅仅检查 ok 标志。
    """

    return ToolAction.model_validate(
        {
            "tool_name": tool_name,
            "arguments": {
                "resource_id": resource_id,
                "time_range": TIME_RANGE,
                "scenario_id": scenario_id,
                "trace_id": trace_id,
            },
        }
    )


@pytest.fixture(scope="session")
def gateway_url() -> Iterator[str]:
    """在临时端口上启动一个真实 uvicorn MCP 网关（含鉴权中间件），会话结束时优雅关停。

    装配方式与 `mcp_server.server._run_streamable_http` 逐字一致，因此测试验证的是生产同一个中间件
    位置，而不是一个"测试专用"的包装。fixture 必须是 session 级：
    `StreamableHTTPSessionManager.run()` 每个实例只能跑一次，而 FastMCP 会缓存它，所以一个进程里
    只能创建并服务一次这个应用。端口用预绑定 socket 交给 uvicorn，避免"先随机选端口再祈祷没被占用"
    的竞态。服务器跑在后台线程里：uvicorn 的 `capture_signals()` 在非主线程是 no-op，且 0.51 用
    loop factory 而不修改全局事件循环策略，因此不会破坏同一进程内 Windows Proactor 下的 stdio 用例。
    """

    guard = ApiSecurityGuard(
        mode="bearer",
        token=SecretStr(GATEWAY_TOKEN),
        # 配额刻意开得极大：本文件要断言的是鉴权与传输失败分类，如果限流在并发用例里意外命中，
        # 429 会被分类成 SERVICE_UNAVAILABLE，让一个鉴权用例以看似正确的错误码失败。
        max_requests=10_000,
        window_seconds=60,
    )
    application = McpGatewaySecurityMiddleware(mcp.streamable_http_app(), guard)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(application, log_level="warning"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started:
        # 线程死掉也要退出等待：否则装配错误会表现成 20 秒挂起，而不是一条明确的启动失败。
        if time.monotonic() > deadline or not thread.is_alive():
            server.should_exit = True
            raise RuntimeError("streamable http mcp gateway failed to start")
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


@asynccontextmanager
async def _client(
    url: str,
    *,
    token: str | None = GATEWAY_TOKEN,
    timeout_seconds: float = 10,
) -> AsyncIterator[StreamableHttpMcpClient]:
    """构造一个指向网关的生产客户端，退出时关闭共享连接池。

    默认带正确令牌，鉴权用例只需覆盖 `token`；超时放宽到 10 秒是因为首个请求要付出服务端会话管理器
    的冷启动开销，而 5 秒的生产默认值在慢机器上会把"能连通"退化成偶发 TIMEOUT。finally 里关池是
    必需的：httpx 客户端持有真实 socket，泄漏会让后续用例看到 ResourceWarning 而不是干净的失败。
    """

    client = StreamableHttpMcpClient(
        url=url,
        auth_token=SecretStr(token) if token is not None else None,
        timeout_seconds=timeout_seconds,
    )
    try:
        yield client
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_streamable_http_lists_nine_read_only_tools(gateway_url: str) -> None:
    """验证跨真实 HTTP 网关的 list_tools 暴露完整九工具及统一安全注解。

    这里的六条断言与 `test_mcp_protocol.py` 里 stdio 版本逐字相同，两个文件因此构成对照组：契约由
    `mcp-tools:v1` 定义，与传输无关，换传输不得改动工具名、数量或只读/非破坏/幂等/输出 Schema 注解。
    断言跨真实 TCP 完成，所以它同时证明网关放行了带令牌的握手。
    """

    async with _client(gateway_url) as client:
        assert await client.list_tools() == tuple(sorted(tool.value for tool in ToolName))
        descriptors = await client.list_tool_descriptors()

    assert len(descriptors) == 9
    assert all(descriptor.read_only for descriptor in descriptors)
    assert all(not descriptor.destructive for descriptor in descriptors)
    assert all(descriptor.idempotent for descriptor in descriptors)
    assert all(descriptor.has_output_schema for descriptor in descriptors)


@pytest.mark.asyncio
async def test_action_crosses_streamable_http_and_produces_evidence(gateway_url: str) -> None:
    """验证一次成功 Action 跨真实 HTTP 往返后带回结构化数据、Evidence 与单个 ToolEvent。

    入口是 `McpToolExecutor` 而不是客户端本身，因此断言覆盖的是生产完整链路：JSON 序列化、网关鉴权、
    工具执行、`McpToolResponse` 边界校验、Observation 标准化。单事件说明成功路径没有触发重试，非空
    Evidence 说明结果来自 Fixture 而不是被压平成一个"成功但没有事实"的空响应。
    """

    async with _client(gateway_url) as client:
        executor = McpToolExecutor(client, retry_count=1)
        observation = await executor.execute(_action("trace_http_success_001"))

    assert observation.response.ok is True
    assert observation.response.data["status"] == "failed"
    assert observation.response.evidence
    assert observation.tool_event.tool_name.value == "lts.get_task_status"
    assert observation.tool_event.trace_id == "trace_http_success_001"
    assert len(observation.tool_events) == 1
    assert observation.observation_refs


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("token", "case_id"),
    [(None, "missing"), (WRONG_TOKEN, "wrong")],
)
async def test_rejected_token_is_permission_denied_without_retry(
    gateway_url: str,
    token: str | None,
    case_id: str,
) -> None:
    """验证缺令牌与错令牌都得到 PERMISSION_DENIED，且执行器只尝试一次。

    401 必须落在 PERMISSION_DENIED 而不是 SERVICE_UNAVAILABLE：后者在 `RETRYABLE_TOOL_ERRORS` 内，
    会让一个配错的令牌把每次调用变成两次网关请求，既放大失败又污染限流配额。单个 ToolEvent 就是这条
    分类正确性的可观察证据。Evidence 为空说明传输层失败没有被包装成伪造事实。
    """

    async with _client(gateway_url, token=token) as client:
        executor = McpToolExecutor(client, retry_count=1)
        observation = await executor.execute(_action(f"trace_http_auth_{case_id}_001"))

    assert observation.response.ok is False
    assert observation.response.error_code is ToolErrorCode.PERMISSION_DENIED
    assert observation.evidence == []
    assert observation.observation_refs == []
    assert observation.tool_event.retryable is False
    assert len(observation.tool_events) == 1


@pytest.mark.asyncio
async def test_wrong_authorization_scheme_is_rejected_before_the_mcp_app(gateway_url: str) -> None:
    """验证错误的鉴权方案在 MCP 应用之前就被拒绝，且 401 响应体与资源 API 逐字一致。

    这一条刻意用裸 httpx 而不是生产客户端：`StreamableHttpMcpClient` 永远只会发出 `Bearer …`，用白盒
    手法改写它的请求头等于断言一个生产不可能产生的输入。裸请求同时证明拒绝发生在握手之前——响应是
    普通 JSON 而不是 JSON-RPC 错误对象，因此客户端不会误以为协议层已经建立。
    """

    async with httpx.AsyncClient(trust_env=False, timeout=10) as raw:
        response = await raw.post(
            gateway_url,
            headers={
                "Authorization": f"Basic {GATEWAY_TOKEN}",
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == 'Bearer realm="dataops-api"'
    assert response.json() == {
        "error": "unauthorized",
        "message": "a valid bearer token is required for this endpoint",
    }


@pytest.mark.asyncio
async def test_service_name_host_header_reaches_the_mcp_app(gateway_url: str) -> None:
    """验证按 service 名访问（`Host: mcp-gateway:8900`）不会被 DNS rebinding 防护挡成 421。

    这条用例复现的是一个只在 compose 里出现过的真实故障：FastMCP 构造函数在 host 属于回环地址时会
    自动开启 DNS rebinding 防护，并把 allowed_hosts 限死为三个回环形式，于是 api 容器按 service 名
    打网关时收到 421，启动期工具发现失败、进程直接退出。本文件其余用例全部通过 127.0.0.1 连接，
    结构上看不到这条失败面。它出现时之所以能一路跑到 api 容器崩溃，是因为当时网关的 healthcheck 只
    探"匿名请求被挡成 401"——401 在鉴权中间件就短路了，根本走不到 MCP 应用。现在容器探针补上了带
    令牌的第二段（见 `test_gateway_healthcheck_probe_covers_both_legs`），这条断言则是同一件事在单元
    化环境里的快速反馈。
    """

    async with httpx.AsyncClient(trust_env=False, timeout=10) as raw:
        response = await raw.post(
            gateway_url,
            headers={
                # 显式 Host 覆盖 httpx 按 URL 生成的默认值，连接仍然打向 127.0.0.1 的临时端口：
                # 这正是容器网络里的形态——传输层连的是 service 的 IP，应用层看到的是 service 名。
                "Host": "mcp-gateway:8900",
                "Authorization": f"Bearer {GATEWAY_TOKEN}",
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "host-header-regression", "version": "1.0"},
                },
            },
        )

    # 单独点名 421 而不是只断言 200：将来若有人"顺手删掉" transport_security 参数，失败信息里应当
    # 直接读到病因，而不是一个需要重新查一遍 SDK 才能解释的状态码。
    assert response.status_code != 421
    assert response.status_code == 200
    assert "protocolVersion" in response.text


def test_gateway_healthcheck_probe_covers_both_legs(gateway_url: str) -> None:
    """验证容器探针的两段判定：匿名 GET 必须 401，带令牌的 initialize 必须打到 MCP 应用。

    这条用例针对的是 healthcheck 自身的强度，而不是网关的功能。`api` 用 `service_healthy` 当启动
    闸门，所以探针放过的故障就是 compose 会放过的故障——421 那次正是这样跑到容器退出码 3 的。用真实
    uvicorn 而不是替身，才能让"探针只用标准库、走真实回环 TCP"这条设计前提也一起被验证。
    """

    location = urlsplit(gateway_url)
    assert location.hostname is not None and location.port is not None
    probe_arguments = {
        # Host 头刻意写部署 service 名而不是 127.0.0.1：这一段要覆盖的就是"按部署名访问是否被
        # 主机名校验挡下"，用回环地址探等于把唯一想验的东西替换掉。
        "host_header": "mcp-gateway:8900",
        "host": location.hostname,
        "port": location.port,
        "path": location.path,
        "timeout_seconds": 10,
    }

    healthy, healthy_reason = healthcheck.probe(token=GATEWAY_TOKEN, **probe_arguments)
    wrong_token, wrong_token_reason = healthcheck.probe(token=WRONG_TOKEN, **probe_arguments)
    unset_token, unset_token_reason = healthcheck.probe(token=None, **probe_arguments)

    assert healthy is True, healthy_reason
    assert "mcp-gateway:8900" in healthy_reason
    # 错令牌与缺令牌都是 unhealthy，但原因必须可区分：一个要去对齐两个 service 的令牌，另一个要去
    # 补 compose 的环境变量映射，把它们合并成一句 "unhealthy" 会让排查从读日志退化成猜。
    assert wrong_token is False
    assert "tokens disagree" in wrong_token_reason
    assert unset_token is False
    assert "misconfigured" in unset_token_reason
    # 令牌绝不能出现在任何一条原因里：Health.Log 会被 `docker inspect` 持久化。
    assert all(
        GATEWAY_TOKEN not in reason and WRONG_TOKEN not in reason
        for reason in (healthy_reason, wrong_token_reason, unset_token_reason)
    )


def test_gateway_healthcheck_probe_reports_a_dead_port_instead_of_raising() -> None:
    """验证端口无人监听时探针返回 unhealthy 与可读原因，而不是抛出 traceback。

    容器 healthcheck 只看退出码，异常逃出去的唯一效果是把"连接被拒"这条一眼可判的信息埋进十行栈里，
    并且那十行会被写进 `docker inspect` 的 Health.Log。端口先绑定再释放，保证此刻确实没人监听。
    """

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    dead_port = listener.getsockname()[1]
    listener.close()

    ok, reason = healthcheck.probe(
        token=GATEWAY_TOKEN,
        host_header="mcp-gateway:8900",
        port=dead_port,
        timeout_seconds=3,
    )

    assert ok is False
    assert "not serving" in reason


@pytest.mark.asyncio
async def test_unreachable_gateway_is_service_unavailable_and_retries_once() -> None:
    """验证网关端口无人监听时映射为 SERVICE_UNAVAILABLE，并按预算恰好重试一次。

    端口先绑定再释放，因此它一定属于本机临时端口范围且此刻确实没人监听——比硬编码一个"应该空着"的
    端口可靠。这是 stdio 完全没有的失败面：连接被拒必须落在可重试集合里（网关重启属于瞬时故障），
    `[1, 2]` 与两个不同 event_id 证明预算既没少执行也没变成无限重试。
    """

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    dead_port = probe.getsockname()[1]
    probe.close()

    async with _client(f"http://127.0.0.1:{dead_port}/mcp", timeout_seconds=3) as client:
        executor = McpToolExecutor(client, retry_count=1)
        observation = await executor.execute(_action("trace_http_unreachable_001"))

    assert observation.response.ok is False
    assert observation.response.error_code is ToolErrorCode.SERVICE_UNAVAILABLE
    assert observation.evidence == []
    assert [event.attempt for event in observation.tool_events] == [1, 2]
    assert all(event.retryable for event in observation.tool_events)
    assert observation.tool_events[0].event_id != observation.tool_events[1].event_id


@pytest.mark.asyncio
async def test_concurrent_calls_share_one_pool_with_independent_sessions(
    gateway_url: str,
) -> None:
    """验证同一客户端上三个并发调用全部成功，各自持有独立 MCP 会话。

    这是并行批次在 HTTP 下的最小复现：`execute_tools` 用 `asyncio.gather` 提交 1–3 个互不依赖的只读
    Action，它们共享一个 httpx 连接池但各建一个会话。若池被某次调用提前关闭（SDK 曾经的 `async with`
    陷阱），或若会话状态被共享，这里会以连接错误或响应错配的形式暴露，而不是静默退化成串行。
    """

    async with _client(gateway_url) as client:
        executor = McpToolExecutor(client, retry_count=1)
        observations = await asyncio.gather(
            executor.execute(_action("trace_http_parallel_001")),
            executor.execute(_action("trace_http_parallel_002", tool_name="lts.get_task_log")),
            executor.execute(
                _action("trace_http_parallel_003", tool_name="lts.get_dependency_topology")
            ),
        )

    assert all(observation.response.ok for observation in observations)
    assert observations[0].response.data["status"] == "failed"
    assert "component_error_code" in observations[1].response.data
    assert "upstream_task" in observations[2].response.data
    assert len({observation.tool_event.event_id for observation in observations}) == 3
    assert [observation.tool_event.trace_id for observation in observations] == [
        "trace_http_parallel_001",
        "trace_http_parallel_002",
        "trace_http_parallel_003",
    ]


@pytest.mark.asyncio
async def test_aclose_is_idempotent_and_post_close_calls_are_classified(gateway_url: str) -> None:
    """验证 aclose 可重复调用，且关闭后的调用返回分类错误而不是裸传输异常。

    lifespan 的 finally 会无条件关池，因此幂等是部署要求而不是风格问题。关闭后仍要给出
    SERVICE_UNAVAILABLE：这样"进程正在关停"与"网关暂时不可达"在审计里读起来一致，上层无需为关停
    时序单独写一条异常处理分支，也不会把 httpx 的内部消息透给 ToolEvent。
    """

    client = StreamableHttpMcpClient(
        url=gateway_url,
        auth_token=SecretStr(GATEWAY_TOKEN),
        timeout_seconds=10,
    )
    assert await client.list_tools() == tuple(sorted(tool.value for tool in ToolName))
    await client.aclose()
    # 第二次关闭不得抛错：lifespan 异常路径可能在同一个 finally 里再次触发它。
    await client.aclose()

    with pytest.raises(McpClientError) as discovery_error:
        await client.list_tools()
    assert discovery_error.value.error_code is ToolErrorCode.SERVICE_UNAVAILABLE

    observation = await McpToolExecutor(client, retry_count=0).execute(
        _action("trace_http_closed_001")
    )
    assert observation.response.ok is False
    assert observation.response.error_code is ToolErrorCode.SERVICE_UNAVAILABLE


class ParallelBatchProtocolPlanner:
    """首轮提交三个互不依赖的 LTS 只读 Action，收到真实 Observation 后第二轮结束调查。

    替身不解释工具内容也不生成根因，只按 react_step 选择预先批准的分支。之所以用并行批次而不是照抄
    stdio 版本的单 Action，是因为并行批次才会同时压到"共享连接池"和"每调用独立会话"这两个 HTTP 特有
    性质；保存 contexts 可证明第二轮 Planner 看到的是 MCP 写回的 Evidence，而不是测试注入的结果。
    """

    def __init__(self) -> None:
        """初始化空上下文记录，供测试检查两轮之间的状态传播。

        构造不启动 MCP、模型或后台任务；所有网络 I/O 只发生在 LangGraph 的 execute_tools 节点里，
        因此 Planner 替身在结构上无法绕过协议边界直接读取 Fixture。
        """

        self.contexts: list[PlannerTurnContext] = []

    async def decide(self, context: PlannerTurnContext) -> PlannerDecision:
        """首轮返回三个 LTS Action 的批次，随后引用 Observation 并 finish。

        三个 Action 共用 run_id 作为 trace，满足控制器的链路绑定门禁；批次消耗三个步数，所以第二轮的
        react_step 是 3 而不是 1。若图错误地调用第三轮，显式断言失败而不是返回一个默认 finish——后者
        会把"循环没有按预算停止"伪装成测试通过。
        """

        self.contexts.append(context)
        if context.state.react_step == 0:
            return PlannerDecision.model_validate(
                {
                    "status": "call_tool",
                    "decision_summary": "一次性提交三个互不依赖的 LTS 只读取证。",
                    "hypothesis_updates": [],
                    "actions": [
                        _action(context.state.run_id, tool_name=tool_name)
                        for tool_name in (
                            "lts.get_task_status",
                            "lts.get_task_log",
                            "lts.get_dependency_topology",
                        )
                    ],
                    "evidence_refs": [],
                    "stop_reason": None,
                }
            )
        if context.state.react_step == 3:
            return PlannerDecision.model_validate(
                {
                    "status": "finish",
                    "decision_summary": "三项 LTS 证据均已通过网关取回。",
                    "hypothesis_updates": [],
                    "actions": [],
                    "evidence_refs": context.state.observation_refs,
                    "stop_reason": "evidence_sufficient",
                }
            )
        raise AssertionError("parallel batch Planner should be called exactly twice")


@pytest.mark.asyncio
async def test_langgraph_react_loop_runs_a_parallel_batch_over_streamable_http(
    gateway_url: str,
) -> None:
    """验证 LangGraph 在 HTTP 传输下完成 Planner→并发 MCP 批次→Observation→Planner 的一整轮。

    这是本切片的端到端证明：控制器、并行门禁、执行器与重试逻辑全部不变，只把传输换成网关。react_step
    等于 3 说明并行买到的是延迟而不是额外取证预算；决策事件不带 tool_name 却带
    parallel_action_count=3，说明时间线没有把一个三工具批次读成"只查了一个工具"；第二轮引用等于 MCP
    写回的 observation_refs，说明 Planner 没有自行编造 Observation。
    """

    planner = ParallelBatchProtocolPlanner()
    async with _client(gateway_url) as client:
        loop = BoundedReactLoop(
            planner=planner,
            executor=McpToolExecutor(client, retry_count=1),
            config=ReactLoopConfig(
                max_steps=6,
                max_parallel_actions=3,
                total_timeout_seconds=30,
            ),
        )
        request = ReactRunRequest(
            state=AgentState(
                run_id="run_react_http_001",
                session_id="session_react_http_001",
                user_query="检查 LTS 合成任务失败原因",
            ),
            capability_request=CapabilitySelectionRequest(
                intent=DiagnosisIntent.SINGLE_COMPONENT_DIAGNOSIS,
                components=(Component.LTS,),
            ),
        )
        result = await loop.run(request)

    assert len(planner.contexts) == 2
    assert planner.contexts[0].state.evidence == []
    assert len(planner.contexts[1].state.tool_events) == 3
    assert result.state.react_step == 3
    assert result.state.stop_reason == "evidence_sufficient"
    assert {event.tool_name.value for event in result.state.tool_events} == {
        "lts.get_task_status",
        "lts.get_task_log",
        "lts.get_dependency_topology",
    }
    assert all(event.response.ok for event in result.state.tool_events)
    assert result.state.observation_refs == [item.evidence_id for item in result.state.evidence]
    assert [event.event_type for event in result.events] == [
        ReactEventType.CAPABILITIES_SELECTED,
        ReactEventType.PLANNER_DECISION,
        ReactEventType.OBSERVATION_RECORDED,
        ReactEventType.OBSERVATION_RECORDED,
        ReactEventType.OBSERVATION_RECORDED,
        ReactEventType.PLANNER_DECISION,
        ReactEventType.LOOP_STOPPED,
    ]
    assert result.events[1].parallel_action_count == 3
    assert result.events[1].tool_name is None



