# 第 8 章 有界 ReAct 循环与并行取证

第 7 章交付了一个"能把一次决策做对"的模型边界。这一章处理**一串决策**：谁来执行模型选的工具、
Observation 怎么回到下一轮、预算怎么扣、模型越界时怎么办。

代码只有一个文件：`app/orchestration/react_loop.py`（810 行），契约 ID 是 `langgraph-react-loop:v3`。

## 8.1 你会验证什么

```bash
.venv/Scripts/python -m pytest -q tests/unit/test_react_loop.py
# 实测：20 passed in 1.02s
```

这 20 个用例的名字几乎就是本章的目录，值得先通读一遍——每一个都是一条边界的自然语言表述：

| 用例名 | 它守住的边界 |
|---|---|
| `langgraph_loop_injects_capabilities_records_observation_and_finishes` | 正常路径：注入能力 → 取证 → 结束 |
| `need_user_input_stops_without_calling_executor` | 非 `call_tool` 决策一次工具都不调 |
| `same_tool_with_different_parameters_executes_as_two_actions` | 去重按参数，不按工具名 |
| `duplicate_action_is_blocked_before_second_executor_call` | 同参重复在进 MCP 之前被拦 |
| `component_scope_blocks_out_of_capability_tool` | 越出已批准组件范围的工具被拦 |
| `react_budget_stops_before_an_extra_planner_or_tool_call` | 预算耗尽时**连模型都不再调** |
| `total_timeout_cancels_blocked_planner_and_preserves_route_event` | 总超时取消挂死节点但保留已有事件 |
| `invalid_evidence_reference_and_trace_are_blocked` | 编造引用、trace 漂移被拦 |
| `restored_tool_event_blocks_same_action_with_new_run_trace` | 恢复 checkpoint 后仍能识别同参调用 |
| `expected_planner_provider_error_becomes_public_loop_stop` | 第 7 章那些域错误变成公开终态 |
| `parallel_batch_runs_concurrently_and_consumes_one_step_per_action` | 并行是真并发，且一批 N 个记 N 步 |
| `single_action_batch_still_reports_its_tool_name` | 单调用路径的可读性没有被并行改造牺牲 |
| `duplicate_action_inside_one_batch_is_blocked_before_any_execution` | 并行不能成为"同一查询发三遍"的绕过路径 |
| `batch_over_configured_parallel_limit_is_blocked` | 批次超并行上限整批拒绝 |
| `batch_exceeding_remaining_step_budget_is_blocked` | 批次超剩余步数整批拒绝 |
| `planner_context_parallel_allowance_shrinks_with_remaining_budget` | 注入 Prompt 的批次上限与门禁同源 |
| `hypothesis_updates_accumulate_into_state_with_deterministic_status` | 假设跨轮累积，置信度由状态确定 |
| `fabricated_hypothesis_reference_is_blocked_like_decision_reference` | 假设里的引用走同一道门禁 |
| `planner_stop_reason_reaches_state_as_evaluable_enum_value` | 停止原因以可评测枚举值落到状态 |
| `knowledge_reference_passes_gate_but_cannot_promote_hypothesis` | 知识引用合法，但不能把假设升为 supported |

最后一行是这一章最需要理解的一条，也是"证据驱动"这四个字的实际定义。

本章涉及的源码：

| 文件 | 行数 | 职责 |
|---|---|---|
| `app/orchestration/react_loop.py` | 810 | 图拓扑、三个节点、八道门禁、指纹、假设投影 |
| `app/orchestration/models.py` | 199 | `ReactLoopConfig`、状态、公开事件、八个停止原因 |
| `tests/unit/test_react_loop.py` | 1046 | 20 个用例 |

## 8.2 ReAct 是什么，以及本项目改了它哪两处

ReAct（Reasoning + Acting）的原始形态是一个文本循环：模型交替输出 `Thought:`（推理）、
`Action:`（要调的工具）、然后由外部把 `Observation:`（工具结果）拼回提示里，再让模型继续。
教科书实现通常是一个 `while` 循环加一堆正则解析。

本项目保留了 Action/Observation 交替这个核心，但改了两处，而且两处都是**为了让它能上生产**：

**第一处：Thought 不出现在任何输出里。** 第 7 章讲过，`PlannerDecision` 根本没有 `thought` 字段。模型
可以在内部推理，但它交给系统的只有结构化决策。这不是洁癖——CLAUDE.md 那条"绝不外泄推理过程"覆盖
API 响应、`run_events`、trace span、SSE 帧、`/metrics` 和 `/demo` 前端，六个出口。少一个字段就少六处
泄漏风险。

**第二处：循环是有界的，界由控制器持有。** 原始 ReAct 靠模型自己说"我完成了"来结束。本项目里模型
**也**可以说结束（`status="finish"`），但控制器有五条独立于模型的终止条件：步数用尽、总墙钟耗尽、
重复调用、越界工具、编造引用。CLAUDE.md 的说法是：

> 返工预算属于工作流（`max_audit_revisions` ≤ 1，模型无法扩大）

同一条原则在这里的形态是：**预算属于 `ReactLoopConfig`，模型看得到但改不了。**

```python
class ReactLoopConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_steps: int = Field(default=6, ge=1, le=20)
    max_parallel_actions: int = Field(default=3, ge=1, le=MAX_PARALLEL_TOOL_ACTIONS)
    total_timeout_seconds: float = Field(default=60, gt=0, le=600)
```

`frozen=True` 的作用在 docstring 里写着："避免调用方在运行中扩大预算"。注意这里防的不是模型——模型
连这个对象都碰不到——防的是**编排代码自己**。一个 run 开始后预算就是常量，这样"这次运行为什么停了"
永远有唯一答案。

## 8.3 LangGraph 的三个概念

只有 Java / Python 语法基础的读者，把 LangGraph 理解成**一个状态机执行器**就够了。它提供三样东西：

**1. 状态 Schema。** 图里流动的是一个对象，本项目用 Pydantic 模型 `ReactGraphState`：

```python
graph = StateGraph(ReactGraphState, context_schema=ReactGraphRuntime)
```

每个节点是 `async def node(state) -> state` 的形状——**接一个状态，返回一个新状态**。用 Pydantic 模型而
不是 `TypedDict`（LangGraph 的默认做法）的好处很直接：每次节点返回都过一遍校验，某个节点写坏了字段会
在那个节点就失败，而不是流到三个节点之后才在别处炸。

**2. 节点与边。** `add_node` 注册函数，`add_edge` 声明固定跳转，`add_conditional_edges` 声明"由一个函数
决定下一跳"。

**3. context（依赖注入）。** 这是本项目用得最关键的一个特性，8.5 节单独讲。

一个常见疑问：既然节点就是 `async def state -> state`，为什么不直接手写一个 `while` 循环？答案在
`_build_react_graph` 的 docstring 里有一半，另一半在第 11 章：**LangGraph 提供了 checkpoint 语义和流式
状态输出**，本项目靠 `astream(stream_mode="values")` 在总超时被触发时拿到"最后一个完整状态"（8.6 节），
靠图的可序列化状态支持 cancel / resume（第 11 章）。手写循环这两件都要自己实现。

## 8.4 拓扑：三个节点、一条条件边

```python
def _build_react_graph():
    graph = StateGraph(ReactGraphState, context_schema=ReactGraphRuntime)
    graph.add_node("select_capabilities", _select_capabilities)
    graph.add_node("planner_react", _planner_react)
    graph.add_node("execute_tools", _execute_tools)
    graph.add_edge(START, "select_capabilities")
    graph.add_edge("select_capabilities", "planner_react")
    graph.add_conditional_edges(
        "planner_react",
        _route_after_planner,
        {"execute_tools": "execute_tools", "end": END},
    )
    graph.add_edge("execute_tools", "planner_react")
    return graph.compile(name="dataops_bounded_react_v3")
```

画出来就是：

```text
START → select_capabilities → planner_react ─┬─(call_tool)→ execute_tools ─┐
                                  ↑          └─(其它)──────→ END           │
                                  └──────────────────────────────────────────┘
```

**唯一的循环边是 `execute_tools → planner_react`**，唯一的分支点是 `planner_react` 之后。整张图只有这
一处不确定性，因此"这次运行为什么走了这条路"永远只需要看一个函数（`_route_after_planner`）。

### 8.4.1 为什么并行不用 LangGraph 的 fan-out

LangGraph 支持从一个节点扇出到多个并行分支再汇聚。本项目**没有用**，`_build_react_graph` 的 docstring
说明了原因：

> 执行节点一次处理整批并行 Action，因此并行度不需要引入 LangGraph 的 fan-out 边——那会把重复检测和
> 预算记账分散到多个分支里。

这句话值得展开。假设用 fan-out：三个 Action 变成三个并行分支，每个分支自己执行、自己回写状态。于是：

- **指纹去重**：三个分支要互相知道对方提交了什么才能查批内重复，而并行分支之间恰好看不到对方的中间
  状态。批内重复（8.12 节那个"同一查询发三遍"）就无法在执行前拦住。
- **预算记账**：`react_step += 1` 在三个分支里各执行一次，需要依赖 LangGraph 的 reducer 做合并；一旦
  合并规则和"整批拒绝"的语义不一致，预算就会算错。
- **原子回写**：8.11 节会看到，本项目刻意先把三个 Observation 全部合并好，再**一次**构造新 `AgentState`。
  fan-out 天然是分批回写。

**并行是执行细节，不是拓扑细节。** 把它放进 `asyncio.gather` 而不是图结构里，图就还是那三个节点，
而所有需要"看到整批"的逻辑都留在一个函数里。

### 8.4.2 `recursion_limit` 为什么是 `max_steps * 2 + 6`

```python
config={"recursion_limit": self._config.max_steps * 2 + 6},
```

LangGraph 用 `recursion_limit` 防止图无限循环——超过就抛异常。这个值必须**大于**任何合法运行的实际
步数，否则合法运行会被误杀。

算一下上界：每轮最多两个节点（`planner_react` + `execute_tools`），最多 `max_steps` 轮（因为每轮至少
消耗一个工具步），所以 `max_steps * 2`；再加上 `select_capabilities`、最后一次 `planner_react`（那次
返回 `finish`，不进执行节点）和一点余量，就是 `+ 6`。

它是**第二道防线**，不是主要防线。主要防线是 8.8 节那个 `react_step >= max_steps` 检查——它在调用模型
**之前**就返回终态。`recursion_limit` 只在图实现出错（比如某天有人加了一条边形成新循环）时兜底，而
兜底的方式是抛异常而不是安全降级，因为那属于实现缺陷，不该伪装成"业务上的安全停止"。

## 8.5 `ReactGraphRuntime`：依赖不能进状态

```python
@dataclass(frozen=True, slots=True)
class ReactGraphRuntime:
    """保存一次图执行共享但不进入 checkpoint 的依赖和绝对截止时间。

    Planner、执行器和注册表是进程内对象，不能序列化进领域状态；LangGraph context 将它们与
    Pydantic 状态分离。每次 run 创建独立 context，因此并发诊断不会共享截止时间或可变状态。
    """

    planner: PlannerAgent
    executor: ToolActionExecutor
    registry: CapabilityRegistry
    config: ReactLoopConfig
    deadline_monotonic: float
```

这是本章最重要的一个结构决策：**图里流动的状态（`ReactGraphState`）和图执行所需的依赖
（`ReactGraphRuntime`）是两个东西。**

为什么必须分开？因为状态要能落库。第 11 章会把 `ReactGraphState` 序列化进 `session_checkpoints` 以支持
cancel / resume。而 `planner` 是一个持有 `httpx` 连接池的对象，`executor` 会启 stdio 子进程——这些东西
**没有任何合理的 JSON 表示**。如果它们混在状态里，序列化会直接失败，或者更糟：被某个宽松的序列化器
悄悄丢掉，恢复之后变成 `None`。

LangGraph 用 `context_schema` 提供了这个分离，节点签名因此长这样：

```python
async def _planner_react(
    graph_state: ReactGraphState,
    runtime: Runtime[ReactGraphRuntime],
) -> ReactGraphState:
```

第一个参数是**可序列化的数据**，第二个参数是**不可序列化的能力**。这条分界线一旦画好，"什么能进
checkpoint"就不再需要靠纪律记住。

### 8.5.1 `deadline_monotonic` 是绝对时刻，不是剩余时长

```python
runtime_context = ReactGraphRuntime(
    ...
    deadline_monotonic=monotonic() + self._config.total_timeout_seconds,
)
```

存的是**截止时刻**（一个绝对值），而不是"还剩多少秒"。区别在于：如果存剩余时长，每个节点都得负责把
它减掉并写回状态，任何一个节点忘了减，预算就漏了。存绝对时刻之后，任何节点在任何时候都能算出剩余：

```python
remaining_time_ms = max(
    0,
    int((runtime.context.deadline_monotonic - monotonic()) * 1000),
)
```

用 `monotonic()` 而不是 `time.time()`：单调时钟不受系统时间调整（NTP 校正、夏令时、手动改表）影响。
用墙钟的话，一次 NTP 回调就可能让 `deadline` 落在过去，整轮运行立刻超时；或者落在很远的未来，超时保护
彻底失效。**任何"经过了多久"的判断都该用单调时钟，任何"什么时候发生的"才用墙钟。**

`max(0, ...)` 保证结果非负——`PlannerTurnContext.remaining_time_ms` 有 `Field(ge=0)` 约束（第 7 章），
截止时刻已过时算出的负数会直接让上下文构造失败，那是把一次超时变成一个 `ValidationError`，方向错了。

### 8.5.2 `frozen=True, slots=True` 与"每次 run 独立 context"

`frozen=True` 防的是节点顺手改预算或截止时刻。`slots=True` 省内存并防拼错属性名。

更重要的是 docstring 最后一句：**"每次 run 创建独立 context，因此并发诊断不会共享截止时间或可变状态。"**
`BoundedReactLoop` 实例本身是可复用的（图在构造时编译一次），但 context 在每次 `run()` 里新建。这就是
为什么一个 `BoundedReactLoop` 可以安全地服务并发的多个 run——**共享的是编译好的图（不可变），隔离的是
context（每次新建）。**

## 8.6 `run()`：`astream` 与总超时的配合

```python
try:
    # 外层墙钟预算覆盖 Planner 和 MCP 的等待时间；astream 让已完成节点状态持续可恢复。
    async with asyncio.timeout(self._config.total_timeout_seconds):
        async for raw_state in self._graph.astream(
            initial_state,
            context=runtime_context,
            stream_mode="values",
            config={"recursion_limit": self._config.max_steps * 2 + 6},
        ):
            latest_state = ReactGraphState.model_validate(raw_state)
except TimeoutError:
    latest_state = _stop_graph_state(
        latest_state,
        reason=ReactStopReason.TOTAL_TIMEOUT,
        summary="ReAct 总墙钟预算已耗尽，正在运行的 Planner 或工具节点已取消。",
        event_type=ReactEventType.LOOP_STOPPED,
    )
```

这十几行是整个控制器里最值得逐字读的部分。

**为什么用 `astream` 而不是 `ainvoke`。** `ainvoke` 只在图跑完时返回最终状态；如果中途超时，`asyncio.timeout`
取消协程，**你什么都拿不到**——已经花掉的三次模型调用和五次 MCP 调用全部丢失，用户只能收到一句"超时"。

`astream(stream_mode="values")` 每完成一个节点就产出一次完整状态，外层用 `latest_state` 持续覆盖。于是
超时发生时，`latest_state` 里是**最后一个成功完成的节点之后的完整状态**，包含到那一刻为止的全部证据、
工具事件和公开事件。`except TimeoutError` 分支只需要给它补一个终态标记。

对照测试用例名：`total_timeout_cancels_blocked_planner_and_preserves_route_event`——"preserves" 那个词
就是这个设计的目的。**超时是一种降级，不是一次回滚。**

**`asyncio.timeout` 而不是 `wait_for`。** 二者都能超时取消，但 `asyncio.timeout` 是上下文管理器，能包住
一整段包含 `async for` 的代码块；`wait_for` 需要一个 awaitable。Python 3.11+ 里 `asyncio.timeout` 是
推荐写法，且超时时抛的是标准 `TimeoutError`（3.11 起 `asyncio.TimeoutError` 就是它的别名）。

**取消是真的取消。** 超时触发时，正在 `await` 的 Planner HTTP 请求或 MCP 子进程调用会收到
`CancelledError` 并中断。这就是为什么总超时必须显著大于单次模型超时。`ReactLoopConfig` 自己的默认值是
60 秒，但生产装配走的是 `app/api/main.py:664`：

```python
config=ReactLoopConfig(
    max_steps=settings.max_react_steps,
    max_parallel_actions=settings.max_parallel_tool_actions,
    total_timeout_seconds=settings.react_total_timeout_seconds,   # 默认 240
),
```

240 不是拍的。第 7 章 §7.8.3 算过那笔账：一次 Planner 调用最坏 61 秒（30 秒超时 + 重试 30 秒 + 1 秒退避），
而一轮运行最多六步。`settings.py` 里有一条校验直接把这个关系钉住：

```python
if self.react_total_timeout_seconds < worst_case:
    raise ValueError(
        "react_total_timeout_seconds must cover one planner call plus its transient retries"
    )
```

docstring 里那句话说得更直白：**"只加重试而不留预算是自欺欺人"**——如果总预算连一次带重试的 Planner
调用都装不下，那个重试策略就永远不会真正生效，只是配置文件里的装饰。

**`model_validate` 那一行的作用。** `astream` 产出的 `raw_state` 在 LangGraph 内部是 dict 形态，这里显式
过一遍 Pydantic 校验再赋给 `latest_state`。代价是每个节点多一次校验，买到的是"进入终态构造的状态一定
合法"——因为超时分支会直接拿 `latest_state` 去构造结果，那时候再发现字段坏了就没有补救余地了。

### 8.6.1 最后那个 `RuntimeError` 守的是什么

```python
if latest_state.capability_selection is None:
    raise RuntimeError("React graph ended before capability selection")
return ReactRunResult(
    contract_id=REACT_LOOP_CONTRACT_ID,
    state=latest_state.agent_state,
    capabilities=latest_state.capability_selection,
    events=latest_state.events,
)
```

`capability_selection` 只可能是 `None` 的情况是：图在第一个节点（`select_capabilities`）完成之前就结束
了——也就是总超时恰好在那 0.1 毫秒内触发，或者图拓扑被改坏。

处理方式是抛 `RuntimeError` 而不是返回一个 `capabilities=None` 的结果。理由和第 7 章反复出现的那条一样：
**这是实现缺陷，不是业务上的安全降级。** 把它变成一个"能力为空的成功结果"会让上层拿到一个没有工具边界
快照的 run，而那份快照是评测重放 Prompt 边界的唯一依据。

`ReactRunResult` 自己还有一层校验，把"图静默结束"这件事彻底堵死：

```python
if not self.state.stop_reason:
    raise ValueError("completed React runs require state.stop_reason")
if self.events[-1].event_type not in {
    ReactEventType.LOOP_STOPPED,
    ReactEventType.POLICY_BLOCKED,
}:
    raise ValueError("completed React runs require a terminal public event")
```

外加 `events: list[ReactPublicEvent] = Field(min_length=2)`——至少两条事件（能力选择 + 某个终态）。
**"运行结束了但没人知道为什么"在这里不是 bug，是构造不出来的对象。**

## 8.7 `select_capabilities`：一次确定性注入

```python
selection = runtime.context.registry.select(graph_state.capability_request)
agent_state = graph_state.agent_state.model_copy(
    update={
        "intent": selection.intent.value,
        "active_capabilities": [name.value for name in selection.active_capabilities],
        "next_action": None,
        "stop_reason": None,
    }
)
```

第 4 章讲过 `CapabilityRegistry`：五项固定能力，只输出 Prompt 片段、工具优先级、输入要求和输出校验规则，
**不调 LLM**。这里是它唯一的调用点。

三个细节：

**`next_action` 和 `stop_reason` 被清空，证据和工具事件不动。** docstring 说明了原因："旧 stop_reason 和
next_action 会清空以开始本轮运行，但已有证据、路径和工具事件保持不变供恢复场景使用。"这是为第 11 章的
resume 准备的——恢复一个被取消的 run 时，输入状态里有上一轮的证据（要保留）和上一轮的终止原因（必须
清掉，否则新一轮从一开始就带着"已停止"的标记）。

**`status` 被显式设为 `RUNNING`。** 同理，恢复场景下输入状态可能是 `STOPPED`。

**这个节点不可能失败到需要降级。** `registry.select` 是纯确定性校验，失败就是 `ValueError` 往上抛。所以
这里没有任何 `_stop_graph_state` 分支——**能力选择要么成功，要么这次 run 根本不该开始。**

事件摘要的措辞也很克制：

```python
summary=(
    f"已按 {selection.intent.value} 选择 {len(selection.active_capabilities)} 项固定能力。"
),
```

只有意图值和能力**数量**，没有能力名列表。能力名在 `ReactRunResult.capabilities` 里有完整快照，事件
时间线不需要重复一遍；而事件 `summary` 有 `max_length=500` 的硬上限，把可枚举的结构化信息塞进自由文本
是浪费预算。

## 8.8 `planner_react`：八道门禁与它们的顺序

这是全章最长的节点，也是"顺序本身就是设计"的最好例子。docstring 先把原则说了：

> 节点先检查工具步数和剩余时间，再调用可替换 Planner。决策仅记录公开摘要；随后依次校验证据引用、
> 并行批次大小、剩余步数、工具组件范围、trace 和同参指纹。**任何一条不通过就整批拒绝而不是悄悄截断，
> 因为"只执行了你要求的一部分"会让 Planner 基于不完整前提继续推理。**

按代码顺序走一遍。

### 8.8.1 模型调用之前：两道免费的门

```python
if graph_state.agent_state.react_step >= runtime.context.config.max_steps:
    return _stop_graph_state(
        graph_state,
        reason=ReactStopReason.REACT_BUDGET_EXHAUSTED,
        summary="已达到 Planner 工具 Action 上限，循环在再次调用模型前停止。",
        event_type=ReactEventType.LOOP_STOPPED,
    )
```

摘要里"**在再次调用模型前停止**"这句是重点。步数用尽的时候，再问模型一次也不能有任何 Action 被执行，
那次调用是纯浪费——按第 7 章的实测，一次 Planner 调用 10–16 秒加一笔 token 费用。

对应的测试用例名把这条断言写得很清楚：`react_budget_stops_before_an_extra_planner_or_tool_call`。

第二道是剩余时间：

```python
remaining_time_ms = max(0, int((runtime.context.deadline_monotonic - monotonic()) * 1000))
if remaining_time_ms == 0:
    return _stop_graph_state(
        graph_state,
        reason=ReactStopReason.TOTAL_TIMEOUT,
        summary="Planner 调用前检测到总墙钟预算已耗尽。",
        event_type=ReactEventType.LOOP_STOPPED,
    )
```

注意这跟 8.6 节那个 `asyncio.timeout` 是**两条独立路径**，产生的是同一个 `TOTAL_TIMEOUT`。区别在于：
外层 `asyncio.timeout` 是**抢占式**的（在任意 `await` 点打断），这里的检查是**主动式**的（在花钱之前
先看一眼）。有了主动检查，"预算只剩 200 毫秒"这种情况就不会再发起一次注定被取消的模型调用。

**两道门都在模型调用之前，且都返回终态而不是抛异常。** 这是"预算属于控制器"最直接的体现。

### 8.8.2 注入 Prompt 的批次上限必须与门禁同源

```python
# 剩余步数同时进入 Prompt 和并行上限：模型看到的可并行数量必须等于控制器真正允许的数量，
# 否则它会反复提交刚好超预算的批次，而每次拒绝都白花一次模型调用。
remaining_tool_calls = runtime.context.config.max_steps - graph_state.agent_state.react_step
context = PlannerTurnContext(
    ...
    max_parallel_actions=min(
        runtime.context.config.max_parallel_actions,
        remaining_tool_calls,
    ),
    remaining_time_ms=remaining_time_ms,
)
```

`min(配置并行度, 剩余步数)`。CLAUDE.md 把这条单独列了出来：

> Prompt 里的批次上限与门禁同源：控制器注入 `min(配置并行度, 剩余步数)`。

场景：`max_steps=6`、`max_parallel_actions=3`，已经用了 5 步。如果注入的还是 3，模型完全合理地提交
3 个 Action，然后被 8.8.6 那道 `PARALLEL_BUDGET_EXCEEDED` 整批拒绝——**一次调用白花，而且模型没做错任何
事，是我们给了它错误的前提。**

测试用例 `planner_context_parallel_allowance_shrinks_with_remaining_budget` 专门守这条。它守的不是安全性
（越界批次照样会被拒），而是**别让门禁去惩罚模型遵守了我们自己给错的规则**。

> 一般规则：任何被门禁检查的约束，都应该以门禁使用的同一个值告诉模型。两处各算一遍必然漂移。

### 8.8.3 span 只包住模型往返

```python
try:
    # span 只包住模型往返：门禁判定属于确定性逻辑，混进来会让 Planner 延迟看起来比实际更高。
    with trace_span(
        TraceSpanKind.REACT_STEP,
        "react.planner_decision",
        react_step=graph_state.agent_state.react_step,
        remaining_time_ms=remaining_time_ms,
    ) as span:
        decision = await runtime.context.planner.decide(context)
        span.annotate(
            decision_status=decision.status.value,
            action_count=len(decision.actions),
            evidence_ref_count=len(decision.evidence_refs),
        )
except PlannerAgentError as exc:
    # 只把适配层已净化的预期失败转换成终态；编程异常继续传播，避免隐藏真实缺陷。
    return _stop_graph_state(
        graph_state,
        reason=exc.stop_reason,
        summary=exc.public_summary,
        event_type=ReactEventType.LOOP_STOPPED,
    )
```

`with` 块里只有一行 `await` 加一次 `annotate`。后面那六道门禁全在 span 之外——它们是本地计算，微秒级，
混进来只会污染"模型有多慢"这个指标。P95 目标是 30 秒，如果 span 边界画错，优化方向也会跟着错。

`except PlannerAgentError` 这一句是第 7 章那套异常分类学的**兑现点**。第 7 章设计了
`PlannerOutputValidationError` / `PlannerRefusalError` / `PlannerProviderError` 三兄弟共享一个基类，
基类只带 `stop_reason` 和 `public_summary` 两个可公开字段——就是为了让这里能写成三行：

```python
reason=exc.stop_reason,
summary=exc.public_summary,
```

控制器**不需要知道**失败是校验错误、拒答还是 429。它只需要一个可公开的分类标签和一句可展示的说明。
这就是"基类只携带两个可公开字段"那个决定买到的东西：**编排层完全不必解析模型失败的细节。**

而 `PlannerAgentError` 之外的异常（`ValueError`、`KeyError`、`AttributeError`……）继续往上抛。注释写得
很直接："避免隐藏真实缺陷"。**能被翻译成 `stop_reason` 的是预期失败，翻译不了的是 bug，两者不能共用
一条出口。**

### 8.8.4 事件先记，门禁后判

```python
agent_state = graph_state.agent_state.model_copy(update={"next_action": decision})
updated = _append_event(
    graph_state.model_copy(update={"agent_state": agent_state}),
    event_type=ReactEventType.PLANNER_DECISION,
    summary=decision.decision_summary,
    # 单 Action 保留具体工具名；批次刻意留空 tool_name，因为只写第一个工具会让时间线读起来
    # 像"只调用了一个工具"，批次规模统一由 parallel_action_count 表达。
    tool_name=(decision.actions[0].tool_name if len(decision.actions) == 1 else None),
    parallel_action_count=len(decision.actions),
    observation_refs=tuple(decision.evidence_refs),
)
```

**决策事件在所有门禁之前就被追加了**，后面每个 `_stop_graph_state(updated, ...)` 传的都是这个已经带上
决策事件的状态。

这个顺序是刻意的。如果先判门禁再记事件，那么一次被拦截的决策在时间线上就只剩一条
`POLICY_BLOCKED`——用户看到"你的调用被拦了"，但看不到**模型到底想调什么**。排查一次
`tool_not_allowed_by_capability` 时，那正是唯一有用的信息。

`tool_name` 那个三目也值得看。注释说明了为什么批次留空：`ReactPublicEvent.tool_name` 是单个值，批次里
填第一个工具会让读时间线的人以为只调了一个。批次规模由独立字段表达，而这个字段在模型层面就被约束了：

```python
parallel_action_count: int = Field(default=0, ge=0, le=MAX_PARALLEL_TOOL_ACTIONS)
```

测试 `single_action_batch_still_reports_its_tool_name` 守的是反面：别为了处理批次把单调用路径的可读性
一起牺牲掉。并行改造最常见的副作用就是让最常见的那条路径变难读。

### 8.8.5 引用门禁：假设里的引用走同一道门

```python
valid_refs = collect_valid_reference_ids(
    agent_state,
    graph_state.evidence_bundle,
    graph_state.confirmed_case_memories,
)
claimed_refs = set(decision.evidence_refs)
for update in decision.hypothesis_updates:
    claimed_refs.update(update.evidence_refs)
invalid_refs = sorted(claimed_refs - valid_refs)
if invalid_refs:
    return _stop_graph_state(
        updated,
        reason=ReactStopReason.INVALID_EVIDENCE_REFERENCE,
        summary=f"Planner 引用了 {len(invalid_refs)} 个当前状态中不存在的证据。",
        event_type=ReactEventType.POLICY_BLOCKED,
    )
```

代码上方那段注释是整个文件里最长的一条，把两件事都交代了：

> Planner 引用必须来自当前状态；模型不能仅凭格式合法就创造不存在的 evidence_id/path_id。
> **假设更新里的引用走同一道门禁**：它们会成为报告根因的 evidence_refs，若放宽校验，模型就能用编造的
> 引用换到一条看起来"有据可依"的结论。可引用宇宙必须与报告层完全同源：草稿、策略校验、修订和 Auditor
> 都接受 Bundle 的 kn_*/path_*/dc_* 与 confirmed 案例 ID，早期版本只认实时 evidence_id 与 checkpoint 旧
> 路径，于是模型引用 Prompt 里明明给出的知识证据反而被整批拒绝——**首次真实模型评测第三个案例的
> `invalid_evidence_reference` 就是这条口径错误。**

三层信息：

1. **`claimed_refs` 把 `decision.evidence_refs` 和每条 `hypothesis_updates[].evidence_refs` 合起来查。**
   漏掉后者的后果很具体：假设的 `evidence_refs` 会被投影成报告根因的引用（第 9 章），那才是最需要真实
   的地方。测试 `fabricated_hypothesis_reference_is_blocked_like_decision_reference` 守这条。
2. **`collect_valid_reference_ids` 与报告层同源。** 第 7 章 §7.9.4 讲过它的孪生函数
   `collect_reference_sources`——渲染层用后者算"白名单告诉模型"，门禁用前者算"白名单验证模型"。**两个
   函数、同一份定义**，这就是 v8 Prompt 修掉的那处口径错误：v7 时代渲染层给了知识证据，门禁却不认。
3. **这道门在假设投影之前。** 顺序很关键：先验证引用真实，再让引用进入假设集合。反过来的话，被拒绝的
   这一轮已经把编造引用写进 `hypotheses` 了，而 `hypotheses` 会活到报告层。

`sorted(claimed_refs - valid_refs)` 排序只为让错误可复现；摘要里只报**数量**不报具体 ID——那些 ID 是模型
编造的字符串，回显给用户没有价值，还多一条把模型输出原样吐给前端的路径。

### 8.8.6 三道批次门禁：为什么全部整批拒绝

先是 `status` 不是 `call_tool` 的正常结束路径（8.9 节讲它前面的假设投影），然后是三道批次检查：

```python
actions = list(decision.actions)
if not actions:
    raise RuntimeError("validated call_tool decision unexpectedly lacks actions")
if len(actions) > runtime.context.config.max_parallel_actions:
    return _stop_graph_state(..., reason=ReactStopReason.PARALLEL_LIMIT_EXCEEDED, ...)
if len(actions) > remaining_tool_calls:
    # 并行只压缩等待时间，不额外发放取证预算；批次超出剩余步数时整批拒绝，避免"执行两个、
    # 丢弃一个"这种让 Planner 无法解释的部分成功。
    return _stop_graph_state(..., reason=ReactStopReason.PARALLEL_BUDGET_EXCEEDED, ...)
```

第一行的 `RuntimeError` 是断言而不是门禁：`PlannerDecision` 的 `model_validator` 已经保证
`status="call_tool"` 时 `actions` 非空（第 1 章 / 第 7 章 §7.5.1）。这里再查一遍是为了让"契约被改坏"
立刻可见，代价一行。

**两个不同的停止原因，而不是一个 `parallel_rejected`。** `PARALLEL_LIMIT_EXCEEDED` 意味着模型违反了我们
告诉它的并行上限；`PARALLEL_BUDGET_EXCEEDED` 意味着模型的批次没超并行度但超了剩余步数。前者是模型不
守规矩，后者往往是 8.8.2 那个 `min()` 出问题。**两种原因指向两个不同的修法，所以必须是两个枚举值。**

至于为什么是"整批拒绝"而不是"截断成允许的长度"——CLAUDE.md 把这条列为关键边界：

> 所有门禁（并行上限 → 步数预算 → capability 范围 / `trace_id` / 指纹去重）都整批拒绝而不截断，否则
> Planner 会基于"其余调用也发生了"的错误前提继续推理。

具体想象一下截断的后果：模型提交"查状态 + 查日志 + 查拓扑"，控制器只执行前两个。下一轮模型看到两条
Observation，但它的内部推理是基于三个都执行了的——它会得出"拓扑没问题（因为我查了）"这种结论，而拓扑
根本没查。**部分成功比整体失败更危险，因为它产生的是错误的确信而不是明确的缺口。**

### 8.8.7 逐 Action 三道门：范围、trace、指纹

```python
batch_fingerprints: list[str] = []
for action in actions:
    if action.tool_name not in selection.tool_priority:
        return _stop_graph_state(..., reason=ReactStopReason.TOOL_NOT_ALLOWED_BY_CAPABILITY, ...)
    if action.arguments.trace_id != agent_state.run_id:
        return _stop_graph_state(..., reason=ReactStopReason.TRACE_ID_MISMATCH, ...)
    fingerprint = _action_fingerprint(action)
    # 批内重复与历史重复用同一套指纹判定：并行不应成为"同一次查询同时发三遍"的绕过路径。
    if fingerprint in graph_state.executed_action_fingerprints or (
        fingerprint in batch_fingerprints
    ):
        return _stop_graph_state(..., reason=ReactStopReason.DUPLICATE_ACTION_BLOCKED, ...)
    batch_fingerprints.append(fingerprint)
return updated
```

**门禁一：组件范围。** `selection.tool_priority` 来自第 4 章的能力配置，它是"本次运行批准了哪些组件"
的投影。九个工具全局存在，但一次 LTS 调度故障的调查不该去查 BDS 的表结构。Prompt 里也给了
`allowed_tool_names`（第 7 章 §7.9.5），这里是它的强制版本。

**门禁二：`trace_id` 必须逐字等于 `run_id`。** v8 Prompt 把这条写进了字段标题：
`【本次运行的 trace_id（每个 action 的 arguments.trace_id 必须逐字等于该值）】`。它保证 MCP 侧的每次
调用都能被归属到确切的 run——这是审计要求，也是 8.12 节指纹计算要排除它的原因。

**门禁三：指纹去重，批内与历史用同一套判定。** `batch_fingerprints` 是本轮的临时列表，
`graph_state.executed_action_fingerprints` 是历史累积。两个集合都查，任一命中就整批拒绝。

注释点出了并行引入的新绕过路径：串行时代，"同一查询发三遍"必然跨三轮，每轮都会撞上历史指纹。并行之后，
一批里塞三个相同 Action 就绕过了历史检查——如果只查历史的话。测试
`duplicate_action_inside_one_batch_is_blocked_before_any_execution` 就是为这条新增的。

**注意这个循环是"发现即整批拒绝"，而不是"收集所有问题再报告"。** 三个 Action 里第二个越界，第一个也
不执行。这与 8.8.6 是同一条原则，只是粒度更细。

### 8.8.8 把八道门禁的顺序列出来

| 序 | 检查 | 停止原因 | 在模型调用之前？ |
|---|---|---|---|
| 1 | 步数预算 | `react_budget_exhausted` | ✅ |
| 2 | 剩余墙钟 | `total_timeout` | ✅ |
| — | *（调用模型；域错误 → 第 7 章的三类 `stop_reason`）* | | |
| 3 | 引用真实性（决策 + 假设） | `invalid_evidence_reference` | ❌ |
| 4 | 批次长度 vs 并行上限 | `parallel_limit_exceeded` | ❌ |
| 5 | 批次长度 vs 剩余步数 | `parallel_budget_exceeded` | ❌ |
| 6 | 工具属于已批准组件 | `tool_not_allowed_by_capability` | ❌ |
| 7 | `trace_id` == `run_id` | `trace_id_mismatch` | ❌ |
| 8 | 指纹未重复（批内 + 历史） | `duplicate_action_blocked` | ❌ |

顺序的三条规律：

1. **不花钱的检查排在花钱之前**（1、2 在模型调用前）。
2. **影响状态的检查排在写状态之前**（3 在假设投影前）。
3. **整批性质的检查排在逐条检查之前**（4、5 在 6、7、8 前）——因为一个超长批次根本不需要逐条验证。

`ReactStopReason` 的 docstring 把这八个值的共同性质讲清楚了：

> Planner 的 finish/need_user_input 原因仍来自其结构化输出；本枚举只覆盖预算、总超时、重复 Action、
> 组件越界、trace 漂移、无效引用和并行批次越界等**控制器可以客观判定的失败路径**。

也就是说系统里有**两套** `stop_reason`：模型自报的七个（第 7 章 §7.10.5）和控制器判定的八个。它们最终
都以字符串落到 `AgentState.stop_reason`，但来源截然不同——一个是模型的自我评估，一个是客观事实。评测时
这个区分很要紧：模型自报 `evidence_sufficient` 是它的判断，控制器判定 `react_budget_exhausted` 是发生
过的事。

## 8.9 假设投影：结论怎么从模型输出变成领域对象

引用门禁通过之后、`status` 判断之前，插着一段容易被忽略但非常关键的代码：

```python
# 假设投影必须发生在这里而不是报告层：确定性草稿只认 AgentState.hypotheses，早期版本把
# hypothesis_updates 连同决策一起丢掉，于是模型在 decision_summary 里说出了正确根因，报告的
# root_causes 却恒为空，Auditor 随后以 report_incomplete 否决——首次真实模型评测里
# root_cause_top1_hit_rate 实测为 0 就是这条链路造成的，与模型能力无关。
agent_state = agent_state.model_copy(
    update={
        "hypotheses": _project_hypothesis_updates(
            agent_state.hypotheses,
            decision.hypothesis_updates,
            components=list(selection.components),
            # 升为 supported 只认实时 Observation：知识节点与历史案例可以被引用，但"知识库里
            # 有这种故障模式"不等于"本次运行观察到了它"，否则模型能凭检索结果直接换到根因。
            observation_refs={item.evidence_id for item in agent_state.evidence},
        )
    }
)
```

### 8.9.1 那次 `root_cause_top1_hit_rate = 0`

先讲注释里那个事故，因为它解释了这段代码为什么存在。

首次真实模型评测（`docs/live-golden-eval-results.md` 的 Run A）里，根因命中率**实测为 0**。当时很自然的
解读是"模型能力不够，找不到根因"。翻 run 记录才发现：模型在 `decision_summary` 里已经把根因说对了，
但报告的 `root_causes` 是空数组，Auditor 于是以 `report_incomplete` 否决。

原因是当时的循环只把 `decision` 存进 `next_action`，`hypothesis_updates` 读完就丢。而第 9 章的确定性
草稿生成器**只认 `AgentState.hypotheses`**——它不会去读 `decision_summary` 的自由文本（那是刻意的，
见第 9 章）。链路断在中间，两端都没错。

这就是 CLAUDE.md 那条评测诚实性要求的具体来源：

> 不能把 `root_cause_top1_hit_rate=0` 说成"模型找不到根因"，也不能把它悄悄改口径后当成提升。

**指标为 0 首先要问的是"这条链路通吗"，而不是"模型行吗"。** 一个断掉的投影和一个无能的模型会产生完全
相同的数字。

### 8.9.2 为什么投影在循环层而不是报告层

技术上，报告层完全可以自己遍历一遍历史决策把 `hypothesis_updates` 收集起来。不这么做有两个理由：

1. **`hypotheses` 要参与后续轮次的推理。** v8 Prompt 的 user 模板里有
   `【当前假设集合】`（第 7 章 §7.10.6）——下一轮 Planner 看到的是**已投影、已受约束**的假设，而不是
   自己上一轮的原话。这形成一个闭环：模型的自述经过确定性规则打折之后再回到模型眼前。
2. **checkpoint 里存的是 `AgentState`。** 如果假设只以"历史决策"的形式存在，恢复时就得重放全部决策才能
   重建它们（第 11 章）。投影成状态之后，恢复只需要读一个字段。

### 8.9.3 投影在 `status` 判断之前：`finish` 那一轮的假设也要收

注意 8.8.6 的 `if decision.status is not PlannerStatus.CALL_TOOL` 在这段投影**之后**。也就是说模型说
"我查完了，可以结束"的那一轮，它同时提交的 `hypothesis_updates` 照样被收进状态。

这跟 v8 Prompt 的硬约束是配套的（第 7 章 §7.10.3）：Prompt 要求模型在 `finish` 的那一轮也必须提交
`hypothesis_updates`，因为那通常正是根因最终确定的一轮。如果投影写在 `status` 判断之后，**最有价值的那
一轮更新恰好会被丢掉**——这正是 8.9.1 那个事故最讽刺的部分。

### 8.9.4 置信度不让模型自报

```python
# 置信度由状态确定性映射，而不是让模型自报一个数字：报告层会把它渲染成 RootCauseConclusion 的
# confidence，一旦交给模型，读者看到的"0.92"既无法复算也无法反驳。CONFIRMED 不在表内，它只能
# 由用户确认案例记忆时产生，Planner 无权自我确认。
_HYPOTHESIS_CONFIDENCE: dict[HypothesisStatus, float] = {
    HypothesisStatus.CANDIDATE: 0.4,
    HypothesisStatus.SUPPORTED: 0.7,
    HypothesisStatus.REJECTED: 0.0,
}
```

三个值，一张查表。为什么不让模型输出一个 0–1 的浮点数？因为 LLM 报出来的置信度既不是校准过的概率，
也不可复算——注释说得直白：读者看到的"0.92"**既无法复算也无法反驳**。而 0.7 是可以解释的：
"状态是 supported，而 supported 在本系统里恒等于 0.7"。

**`CONFIRMED` 故意不在表里。** 查表用的是 `_HYPOTHESIS_CONFIDENCE[status]`（直接下标，不是 `.get()`），
所以哪天有人让 Planner 能投影出 `CONFIRMED`，这里立刻 `KeyError`，而不是悄悄给一个默认值。
`HypothesisStatus.CONFIRMED` 只在用户确认案例记忆时产生（第 10 章）——**模型无权自我确认，这是缺一个
字典项换来的结构性保证。**

### 8.9.5 只有实时 Observation 能把假设升到 supported

```python
def _projected_hypothesis_status(
    update_status: HypothesisUpdateStatus,
    *,
    supporting_count: int,
) -> HypothesisStatus:
    if update_status is HypothesisUpdateStatus.REJECTED:
        return HypothesisStatus.REJECTED
    if update_status is HypothesisUpdateStatus.WEAKENED:
        return HypothesisStatus.CANDIDATE
    if supporting_count > 0:
        return HypothesisStatus.SUPPORTED
    return HypothesisStatus.CANDIDATE
```

四行，但 `supporting_count` 是怎么算的才是重点：

```python
status = _projected_hypothesis_status(
    update.status,
    supporting_count=len([ref for ref in supporting if ref in observation_refs]),
)
```

`observation_refs` 是 `{item.evidence_id for item in agent_state.evidence}`——**只有本次运行通过 MCP 真正
取回来的 Observation**。知识节点（`kn_*`）、图路径（`path_*`）、文档切片（`dc_*`）、confirmed 案例 ID 全都
**不计数**。

这条规则是 0.4 节那句"实时 Observation 永远优先于历史案例"在代码里最硬的落点。想清楚不加会怎样：模型
检索到一条知识节点"LTS 队列积压通常由上游 checkpoint 膨胀导致"，把它作为 `evidence_refs` 提交一条
`strengthened`，假设立刻升到 supported、置信度 0.7、进报告成为根因。**整条推理链里没有任何一个字节来自
本次故障。** 这就是 RAG 系统最典型的失败模式：把"检索到了相似的东西"当成"证明了这件事"。

注意知识引用**不会被丢弃**——docstring 说明了："知识与历史引用仍会保留在 supporting_evidence 里供报告
溯源"。它们进得了引用列表，只是不参与状态升级。**可引用 ≠ 可采信为本次事实。**

测试 `knowledge_reference_passes_gate_but_cannot_promote_hypothesis` 把这两半合起来断言：同一个
`kn_*` 引用既通过了 8.8.5 的引用门禁（说明它合法），又没能把假设升到 supported（说明它不算实时证据）。
**一个用例守住两条容易被合并处理的边界。**

`WEAKENED` 回落 `CANDIDATE` 而不是 `REJECTED` 也有交代："保留后续轮次重新加强的可能"。证据变弱不等于
假设被推翻，第三轮可能又出现支持它的观测。

### 8.9.6 引用按状态分流，且累积去重

```python
supporting = list(current.supporting_evidence)
contradicting = list(current.contradicting_evidence)
target = (
    contradicting
    if update.status in {HypothesisUpdateStatus.WEAKENED, HypothesisUpdateStatus.REJECTED}
    else supporting
)
for ref in update.evidence_refs:
    if ref not in target:
        target.append(ref)
```

同一条 `evidence_refs`，进支持集还是反对集**由本轮 `update.status` 决定**。模型不需要（也没有办法）
分别提交两个列表——它只说"这条更新是加强还是削弱"，分流由代码做。

`if ref not in target` 而不是用 `set`：**顺序要保留**，因为报告里的引用列表按首次出现排序，而 `set` 的
迭代顺序在跨进程时不稳定（第 5 章讲过同一个问题）。列表长度是个位数，O(n²) 无所谓。

累积而非覆盖，所以多轮取证会持续增强同一条假设。docstring："因此多轮取证会持续增强同一假设而不是互相
覆盖"。

测试 `hypothesis_updates_accumulate_into_state_with_deterministic_status` 守累积 + 确定性状态两条。

### 8.9.7 组件不让模型自述

```python
current = FaultHypothesis(
    hypothesis_id=update.hypothesis_id,
    symptom=symptom,
    candidate_root_cause=root_cause,
    components=list(components),   # ← 来自 selection.components，不是模型输出
)
```

新建假设的 `components` 一律取本次运行已批准的 capability 组件。docstring："模型不能在假设里自述组件，
避免报告把未获批组件写进结论。"

这是 8.8.7 那道组件范围门禁的**镜像**：工具调用不能越界，结论里的组件也不能越界。否则会出现一个尴尬的
报告——所有证据都来自 LTS，结论却写着"BDS 资源不足"。

### 8.9.8 未知 `hypothesis_id` + 非 `new` 状态：静默忽略

```python
if current is None:
    if update.status is not HypothesisUpdateStatus.NEW:
        continue
```

模型说"加强假设 h3"，但状态里没有 h3。三种可选处理：

| 做法 | 后果 |
|---|---|
| 静默创建一条新假设 | "加强"凭空变成一条**没有症状描述**的结论，可能进报告 |
| 抛异常终止本轮 | 一次拼错 ID 毁掉整轮真实取证（那一轮的 Observation 全部作废） |
| `continue` 忽略这条更新 | 丢一条更新，其余更新与本轮取证照常 |

docstring 把选择和两个被否决的方案都写下来了：

> 更新引用了不存在的 hypothesis_id 且状态不是 `new` 时直接忽略：静默创建会让"增强"凭空变成一条没有
> 症状描述的新结论，而抛错会让一次拼错 ID 毁掉整轮真实取证。

这条是本章为止**唯一一处"静默忽略"**，而且注释解释了为什么这里可以：被忽略的是一条**增量**更新，不是
一次工具执行。8.8.6 拒绝截断批次是因为"部分执行"会污染模型的前提；这里丢一条针对不存在对象的更新，
不会让任何人相信一件没发生的事。**"整批拒绝"与"静默忽略"的分界是：会不会让下游基于错误前提继续推理。**

## 8.10 `_route_after_planner`：路由只读结构化字段

```python
def _route_after_planner(graph_state: ReactGraphState) -> str:
    """根据结构化循环状态选择执行工具批次或结束图，不读取自然语言摘要。

    只有 running 且 next_action 为 call_tool 的状态可以进入执行节点；所有停止路径统一返回 end。
    缺少 Action 的 running 状态代表图实现错误，显式抛出 RuntimeError 防止静默结束。
    """

    if graph_state.status is ReactLoopStatus.STOPPED:
        return "end"
    decision = graph_state.agent_state.next_action
    if decision is None or decision.status is not PlannerStatus.CALL_TOOL:
        raise RuntimeError("running React graph requires a call_tool decision")
    return "execute_tools"
```

四行逻辑，两点值得说。

**第一，它只读 `status` 和 `decision.status` 两个枚举。** docstring 第一行就把这条写成了标题——
"不读取自然语言摘要"。不解析 `decision_summary`，不看 `stop_reason` 的字符串内容，更不看任何模型自由
文本。这跟第 9 章审计路由那条"路由只读 `AuditStatus` 与 `retry_count`，永不解析 `revision_instructions`"
是同一条原则的两次应用：**控制流只依赖枚举，因为枚举的取值集合是封闭的，自由文本不是。**

另外注意所有停止路径**统一返回 `"end"`**，而不是按停止原因分出多条边。八种 `ReactStopReason` 加模型自报
的七种，共十五种停法，走的是同一条出边——**因为它们的后续处理完全相同**：把状态交给 8.6 节的 `run()`
去校验终态。为每种原因画一条边只会让图变复杂而没有任何行为差异。

**第二，`running` 却没有合法决策时抛 `RuntimeError` 而不是转向 `end`。** docstring 说明了动机："缺少
Action 的 running 状态代表图实现错误，显式抛出 RuntimeError 防止静默结束。"能走到这条边说明
`_planner_react` 既没有停止循环也没有留下 `call_tool` 决策，那是控制器自身的 bug。悄悄转向 `end` 会让这
个 bug 表现为"诊断莫名结束"，在生产里几乎不可能定位。

对比 8.9.8 的静默忽略：那里是**模型的输入有瑕疵**，这里是**控制器自身状态不一致**。前者要容错，后者要
炸得越响越好。`_execute_tools` 开头还有一道同样性质的重复断言：

```python
if decision is None or not decision.actions:
    raise RuntimeError("execute_tools requires a validated pending action batch")
```

路由已经查过一遍了，节点入口再查一遍。**因为节点是可以被单测直接调用的**——它不能假设自己一定是从那条
条件边进来的。

## 8.11 `_execute_tools`：真并行、原子回写、逐 Action 记账

这是第 8 章标题里"并行取证"那半句的实现。docstring 把四件事一次说完：

> 批次用 `asyncio.gather` 同时发起：九个工具都是只读的，且 `StdioMcpClient` 每次调用都启动独立子进程
> 会话，因此并发调用之间没有共享连接或游标可被破坏。`return_exceptions=True` 让编程异常在所有兄弟协程
> 收尾后再原样重抛，避免第一个失败留下仍在写 span 的孤儿任务。回写按 Action 顺序进行，`react_step`
> 增加批次长度，因此**并行只买到更低延迟而不是更多取证预算**。

### 8.11.1 并发安全不是靠加锁，是靠 stdio 会话本来就独立

```python
results = await asyncio.gather(
    *(
        _execute_single_action(
            runtime.context.executor,
            action,
            react_step=graph_state.agent_state.react_step,
        )
        for action in actions
    ),
    return_exceptions=True,
)
```

一行 `gather`，没有锁、没有信号量、没有连接池。能这么写是因为第 3 章那个设计决定：`StdioMcpClient`
**每次 `call_tool` 都新起一个 `sys.executable -m mcp_server.server` 子进程会话**。

当时那个决定看起来是浪费——每次工具调用都付一次 Python 解释器启动开销。现在收到了回报：三个并发 Action
跑在三个互不相干的操作系统进程里，**没有任何共享可变状态**，所以并发安全是结构性的，不是靠纪律维持的。

如果当初选的是"复用一个长连接会话"，这里就必须处理请求 ID 复用、响应乱序分派、会话崩溃时三个协程一起
失败——那是一整类 bug。CLAUDE.md 把这个因果关系明确记下来了：

> `execute_tools` 用 `asyncio.gather` 并发跨独立 stdio 子进程执行（`StdioMcpClient` 每次 `call_tool`
> 都开新会话，所以并发安全）。

**一个早期为了简单做的决定，在两章之后变成了并行改造的前提条件。** 这类回报是"边界画对"的典型信号。

注意 `react_step=graph_state.agent_state.react_step`：批内每个 Action 记的都是**同一个**步号。它们属于
同一轮，步号是轮次标识而不是执行序号。

### 8.11.2 `return_exceptions=True` 之后立刻重抛

```python
for result in results:
    if isinstance(result, BaseException):
        raise result
observations = [result for result in results if isinstance(result, ToolObservation)]
```

看起来自相矛盾：既然要重抛，为什么不直接用默认的 `return_exceptions=False`？

区别在**兄弟协程的命运**。`gather` 默认行为是第一个异常立刻向上传播，而此时其余协程**仍在运行**——它们
没有被取消，只是没人再等它们。于是会出现：

- 子 span 在父 span 已经退出 `with` 之后才写 `annotate`，父子时间戳颠倒；
- stdio 子进程没有走完 `aclose()`，留下僵尸进程；
- 报错的堆栈里看不到另外两个工具究竟成功了还是失败了。

`return_exceptions=True` 让 `gather` **等所有协程收尾**，把异常当返回值收集起来。然后我们自己遍历、
`raise result` 原样重抛——`raise` 一个异常实例会保留它原本的 `__traceback__`，所以调试信息不丢。

**这就是"编程异常继续传播"和"不留孤儿任务"两个要求的交集写法。** 注意能走到这里的异常一定是编程错误：
MCP 侧的预期失败（超时、`EMPTY_RESULT`、越权）在第 3 章的 executor 里已经被转成
`ToolObservation(response.ok=False)` 了，是**返回值而不是异常**。

`isinstance(result, ToolObservation)` 这一行在语义上是多余的（异常都被重抛了），但它让 mypy 知道
`observations` 的元素类型，同时对"executor 返回了别的东西"这种契约破坏保持防御。

### 8.11.3 span 的父子层次：平铺会让并行看起来像串行

```python
# 批 span 是父节点，每个 Action 的 react.tool_call 是子节点：只有这样火焰图才能同时显示
# "整批等了多久"和"哪个工具是长尾"，而把三个 span 平铺会让并行看起来像串行。
with trace_span(
    TraceSpanKind.TOOL_CALL,
    "react.tool_batch",
    action_count=len(actions),
    tool_names="+".join(action.tool_name.value for action in actions),
    react_step=graph_state.agent_state.react_step,
) as batch_span:
```

三层结构：

```text
react.planner_decision   （模型往返，8.8.3）
react.tool_batch         （整批墙钟）
  ├─ react.tool_call     tool_name=lts_job_status
  ├─ react.tool_call     tool_name=lts_task_logs
  └─ react.tool_call     tool_name=dependency_topology
```

父子关系是怎么建立的？`_execute_single_action` 的 docstring 讲了机制：

> 父指针取自 ContextVar，而 `asyncio.gather` 为每个协程复制当前上下文，因此批内子 span 会稳定挂在批
> span 之下，互相之间不会因为完成顺序不同而错挂。

`asyncio` 为每个 Task **复制**（而非共享）`contextvars.Context`。三个协程各自拿到一份指向 `tool_batch`
的父指针快照，各自设置自己的 span 为"当前"，**互不干扰**。如果父指针存在一个模块级全局变量里，第二个
协程一开始就会把第一个的父指针覆盖掉——这是异步代码里最难查的一类串扰。

`tool_names="+".join(...)` 用 `+` 而不是空格或逗号连接。第 12 章会讲原因：span 属性值被正则限制为 ASCII
标识符，**空格与 CJK 直接拒绝**。这不是风格选择，是那条 ASCII 白名单强加的。

批 span 的收尾也有内容：

```python
failed_count = sum(1 for item in observations if not item.response.ok)
batch_span.annotate(ok_count=len(observations) - failed_count, failed_count=failed_count)
if failed_count:
    batch_span.mark(TraceSpanStatus.ERROR)
```

**一个工具失败就把整批 span 标红。** 因为读火焰图的人需要立刻看到"这轮取证不完整"，而不是展开三个子
span 逐个检查。

### 8.11.4 先算完再一次回写

```python
# 先合并全部 Observation 数据，再一次构造新 AgentState，避免其他节点看到半回写状态。
evidence = list(graph_state.agent_state.evidence)
tool_events = list(graph_state.agent_state.tool_events)
observation_refs = list(graph_state.agent_state.observation_refs)
fingerprints = list(graph_state.executed_action_fingerprints)
for action, observation in zip(actions, observations, strict=True):
    evidence = _merge_evidence(evidence, observation.evidence)
    tool_events = _merge_tool_events(tool_events, observation.tool_events)
    observation_refs = _stable_unique([*observation_refs, *observation.observation_refs])
    fingerprints = _stable_unique([*fingerprints, _action_fingerprint(action)])
agent_state = graph_state.agent_state.model_copy(update={...})
```

循环体操作的全是**局部列表**，循环结束后才有唯一一次 `model_copy`。这是 8.4.1 节"原子回写"那条不用
LangGraph 扇出的理由在代码里的样子。

`zip(actions, observations, strict=True)` 的 `strict=True`（Python 3.10+）值得单独说：它要求两个序列
**长度严格相等**，否则抛 `ValueError`。如果 `gather` 因为某种原因少返回一个结果，默认 `zip` 会**静默截断**，
于是第三个 Action 的证据凭空消失、指纹没记上、下一轮它还能再被调用一次。**`strict=True` 是一个字符换来
的静默数据丢失防护。**

同一个 `zip` 在下面记事件时又出现一次，同样带 `strict=True`。

### 8.11.5 `react_step += len(actions)`：并行不买预算

```python
"react_step": graph_state.agent_state.react_step + len(actions),
```

一行，但它是整个并行设计的核心约束。CLAUDE.md 单列一条：

> **并行只买延迟，不买预算。** 一批 N 个 Action 记 N 步，因此调大并行度不会让模型多看证据。

为什么这条这么要紧？设想改成 `+= 1`（"一轮算一步"）：`max_steps=6` 配 `max_parallel_actions=3`，模型就能
执行 18 次工具调用。于是**并行度这个纯性能参数变成了成本参数**——运维为了降延迟把它从 3 调到 5，实际
上把取证预算悄悄放大到 30 次，token 消耗和 MCP 负载一起翻倍。

`+= len(actions)` 之后，`max_steps=6` 恒等于"最多 6 次工具调用"，无论它们怎么分批。并行度只影响这 6 次
被压缩成几轮墙钟。

测试 `parallel_batch_runs_concurrently_and_consumes_one_step_per_action` 的名字就是这条断言，它同时验证：

```python
assert final.agent_state.react_step == 3        # 三个 Action 记三步
assert executor.max_in_flight == 3              # 且它们真的同时在飞
```

### 8.11.6 `max_in_flight`：怎么证明"真的并发"

并行最容易造假的地方是：代码写了 `asyncio.gather`，但下游其实是串行的（比如 executor 内部有锁，或者
`await` 链上某处是同步阻塞调用）。这时候总耗时不会下降，而**断言总耗时又会让测试变成 flaky 的性能测试**。

测试用的 `ConcurrencyProbeExecutor` 换了个思路——测**峰值并发数**：

```python
self._in_flight += 1
self.max_in_flight = max(self.max_in_flight, self._in_flight)
await asyncio.sleep(0.05)
...
finally:
    self._in_flight -= 1
```

`asyncio.sleep(0.05)` 制造一个必然让出控制权的窗口。如果三个 Action 真并发，三次 `+= 1` 会在任何一次
`-= 1` 之前发生，`max_in_flight` 到 3。如果串行，每个 Action 走完整个"进入→睡→退出"，峰值恒为 1。

**这是个确定性断言，不是时间断言。** 无论机器多慢，串行都不可能让峰值大于 1；无论机器多快，并发的三个
协程都会在同一个 `sleep` 窗口里重叠。`finally` 里减一保证异常路径也不会污染计数。

> 一般化：要验证并发，不要断言"耗时变短"（受机器负载影响，必然 flaky），要断言"某个时刻有 N 个任务
> 同时在场"。前者测的是性能，后者测的是结构。

### 8.11.7 每个 Action 一条 Observation 事件

```python
# 每个 Action 单独产生一条 Observation 事件：批次内某个工具失败必须能被单独读出来，
# 否则"三个里有一个 EMPTY_RESULT"会被压缩成一句无法追责的批次摘要。
for action, observation in zip(actions, observations, strict=True):
    updated = _append_event(
        updated,
        event_type=ReactEventType.OBSERVATION_RECORDED,
        summary=_observation_summary(action, observation),
        tool_name=action.tool_name,
        parallel_action_count=len(actions),
        observation_refs=tuple(observation.observation_refs),
    )
```

**决策端一条（8.8.4），观测端 N 条。** 这个不对称是刻意的：决策是模型的一个动作，观测是 N 个独立事实。
每条事件带 `tool_name`（这里不像决策事件那样留空，因为每条事件确实只对应一个工具），同时带
`parallel_action_count=len(actions)` 让读者知道它属于一个 N 并行批次。

所以一次 3 并行的轮次在时间线上是 4 条事件：1 条 `PLANNER_DECISION`（`tool_name=None`,
`parallel_action_count=3`）+ 3 条 `OBSERVATION_RECORDED`（各带 `tool_name`）。测试里断言的正是那个
**精确 7 条事件序列**（含首尾的 `CAPABILITY_SELECTED` 与终止事件）。

摘要由 `_observation_summary` 生成，它的 docstring 值得原样引用：

> 摘要刻意只包含工具名、证据条数、尝试次数和错误码：这些都是可以核对的事实，而把响应内容摘进事件会让
> 时间线变成第二份未经校验的证据来源。失败路径明确声明未伪造证据。

```python
f"{action.tool_name.value} 失败（{error_code}），记录 {len(observation.tool_events)} 次尝试且未伪造证据。"
```

"**未伪造证据**"这四个字直接写进用户可见的摘要。第 3 章那条"失败工具不产出 evidence"的边界，在这里变成
了一句可读的声明——**用户不需要去翻代码才能知道失败时系统没有编造数据。**

注意事件摘要里没有任何响应内容。如果这里摘一句日志原文，时间线就成了"第二份证据来源"：它没经过
第 8.8.5 节的引用门禁，也不在报告的可引用宇宙里，但看起来同样权威。**能被引用的证据只有一个入口。**

## 8.12 指纹去重：为什么要先 pop 掉 `trace_id`

```python
def _action_fingerprint(action: ToolAction) -> str:
    """把工具名和除 trace 外的规范化参数转换为跨 checkpoint 稳定指纹。

    ``trace_id`` 是每个新 run 必须变化的审计身份，不属于查询语义；先移除它，才能在恢复上一轮
    ToolEvent 后识别相同工具、资源、时间窗和场景。JSON 规范化避免键序/空格漏检，SHA-256 只做
    本地等价性，不承载凭据或安全签名。
    """

    payload_data = action.model_dump(mode="json")
    # trace 仍由前置门禁严格绑定当前 run_id；这里只排除它，防止新 run ID 成为重复调用绕过路径。
    payload_data["arguments"].pop("trace_id")
    payload = json.dumps(
        payload_data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(payload.encode("utf-8")).hexdigest()
```

五个细节，每个都不能改。

**`pop("trace_id")`：因为 `trace_id` 每个 run 必然不同。** 8.8.7 那道门禁强制它逐字等于 `run_id`。如果
指纹把它算进去，同一次运行内它是常量（不影响去重），但**跨 run 恢复时它变了**——第 11 章的 checkpoint
恢复会重建 `executed_action_fingerprints`，而恢复后的新 run 有新的 `run_id`，于是所有历史指纹都对不上，
模型可以把恢复前查过的工具**原样再查一遍**，白花预算。docstring 把这条概括成一句话："`trace_id` 是每个
新 run 必须变化的审计身份，**不属于查询语义**"。

测试 `restored_tool_event_blocks_same_action_with_new_run_trace` 就是这条：从历史 `tool_events` 重建的
指纹，必须能拦住一个**带新 `trace_id`** 的相同 Action。

**`pop("trace_id")` 没给默认值。** 写的是 `pop("trace_id")` 而不是 `pop("trace_id", None)`——键不存在
就 `KeyError`。这是刻意的：`trace_id` 是所有工具参数模型的必填字段（第 1 章），哪天有人加了一个不带它的
参数模型，指纹会**立刻炸**而不是悄悄开始计算一份包含了不同字段集的哈希。用 `None` 兜底反而会让"去重口径
静默变了"这件事无声无息。

**`sort_keys=True`：字典顺序不能影响指纹。** Python 3.7+ 的 dict 保序，而 `model_dump` 的键顺序取决于
字段声明顺序——重排一次 Pydantic 模型的字段就会让所有历史指纹失效。排序后指纹只依赖**内容**。

**`separators=(",", ":")`：去掉 `json.dumps` 默认的空格。** 纯粹是让编码规范唯一化，同一个对象不能有
两种字节表示。docstring 把这两条合起来说成"JSON 规范化避免键序/空格漏检"——注意用词是**漏检**：编码不
唯一的后果不是误报，是**同一个 Action 算出两个指纹于是被放行**。

**`ensure_ascii=False`：中文参数按 UTF-8 原样编码。** 工具参数里会出现中文（比如作业名），
`ensure_ascii=True` 会把它转成 `\uXXXX`。两种都能唯一化，但既然最后都 `encode("utf-8")`，不转义更短，
且与项目其它序列化点（第 7 章 `_json_text`）保持一致。

**为什么是 SHA-256 而不是 `hash()`？** Python 内置 `hash()` 对 `str` 默认加了**每进程随机盐**
（`PYTHONHASHSEED`），跨进程不稳定——而指纹要写进 checkpoint 再被另一个 Worker 进程读出来。这是"必须用
密码学哈希"的一个非安全理由：**它是唯一跨进程稳定的选择。** docstring 也主动澄清了它不是安全签名：
"SHA-256 只做本地等价性，不承载凭据或安全签名。"

至于"同工具不同参数算两次"，测试 `same_tool_with_different_parameters_executes_as_two_actions` 守它。
去重的粒度是"工具 + 参数"，不是"工具"——查 `lts_task_logs` 的两个不同任务是两次合法取证。

### 8.12.1 从历史事件重建指纹

```python
def _fingerprints_from_tool_events(tool_events: list[ToolEvent]) -> list[str]:
    """从已有 ToolEvent 重建去重集合，使 checkpoint 恢复后仍拦截同参 Action。

    MCP 重试会产生多个具有相同工具和请求的事件，最终通过稳定去重只保留一个指纹；该过程不
    依赖 event_id，因此兼容旧事件 ID 生成规则，并保留首次出现顺序便于调试。
    """

    return _stable_unique(
        [
            _action_fingerprint(ToolAction(tool_name=event.tool_name, arguments=event.request))
            for event in tool_events
        ]
    )
```

它把每条 `ToolEvent` 的 `tool_name` + `request` **重新组装成一个 `ToolAction`**，再走同一个
`_action_fingerprint`。恢复路径和执行路径用的是**同一个函数**，所以两者不可能漂移——如果恢复侧自己写一遍
规范化逻辑，哪天改了 `sort_keys` 就只改了一半。

docstring 提到两个不显眼的性质：

- **MCP 一次重试会产生两条 `ToolEvent`（同工具同请求）**（第 3 章：瞬时错误最多重试一次）。它们算出同一
  个指纹，`_stable_unique` 把它折成一条。所以"重试"不会让去重集合里出现重复项。
- **不依赖 `event_id`。** 只用 `tool_name` 和 `request` 两个语义字段，因此**旧快照里按旧规则生成的
  `event_id` 照样能被解析**。这是一条实际的向后兼容保证：事件 ID 生成规则调整过一次，如果指纹依赖它，
  所有历史 run 恢复后都会失去去重能力。

这是个一般性的好性质：**派生数据尽量保持可重算。** 如果指纹只存快照而不可重算，那么快照损坏时无从校验，
任何指纹算法调整也会让旧快照永久不兼容。

## 8.13 `_append_event` 与 `ReactPublicEvent`：时间线的三条不变量

### 8.13.1 事件 ID 由控制流决定，因此可复现

```python
sequence = len(graph_state.events) + 1
event_id = _stable_id("react_evt", graph_state.agent_state.run_id, str(sequence), event_type.value)
```

docstring：

> 事件 ID 由 run_id、序号和类型计算，**重放相同控制流可得到相同引用**。函数不修改原列表，避免 LangGraph
> 并发或调试快照之间共享可变对象；事件字段最终由 ReactPublicEvent 再校验。

没有 UUID4，没有时间戳参与 ID。三个输入（run_id、单调序号、事件类型）都由控制流唯一决定，所以同一次
`run_id` 重放同一串决策会得到**逐字相同**的事件 ID。这让评测可以对整条时间线做等值断言，而不是逐字段
比对后忽略 ID。

`sequence = len(events) + 1` 依赖"事件只追加不删除"。这是纯函数式追加保证的：

```python
return graph_state.model_copy(update={"events": [*graph_state.events, event]})
```

`[*old, new]` 建新列表而不是 `old.append(new)`。docstring 讲了动机——避免"LangGraph 并发或调试快照之间
共享可变对象"。如果原地 append，一份被别处持有的旧状态引用会**跟着变**，而 LangGraph 的 checkpoint 和
`astream` 的每一帧都持有旧状态引用（8.6 节的 `latest_state` 就是其中一个）。

### 8.13.2 模型层强制"停止类型必须带停止原因"

`ReactPublicEvent` 的 validator：

| 不变量 | 实现方式 |
|---|---|
| `event_id` 形如 `react_evt_` + 16 位十六进制 | `pattern=r"^react_evt_[a-f0-9]{16}$"` |
| `summary` 不超过 500 字符 | `Field(max_length=500)` |
| `parallel_action_count` ≤ 全局并行上限 | `Field(ge=0, le=MAX_PARALLEL_TOOL_ACTIONS)` |
| 停止类事件**必须**带 `stop_reason`，非停止类**必须不**带 | `model_validator` 双向断言 |

最后一条是双向的，这点容易做一半。单向（"停止必须带原因"）只防漏；双向还防**一条
`OBSERVATION_RECORDED` 事件带上 `stop_reason`**——那会让前端把一条正常观测渲染成终止卡片。

`_stop_graph_state` 的 docstring 明确把这个校验当成第二道防线："调用方必须传终止类事件，**事件模型会再次
校验该不变量**。" 控制器自己保证一遍，模型层再验一遍。

### 8.13.3 事件里不许出现的东西

五种事件类型（`CAPABILITY_SELECTED`、`PLANNER_DECISION`、`OBSERVATION_RECORDED`、`POLICY_BLOCKED`、
`LOOP_STOPPED`）共用**同一个** 8 字段模型，没有一个字段能装自由文本以外的东西：`summary` 有 500 字上限，
`tool_name` 是枚举，`observation_refs` 是 ID 元组，`stop_reason` 是枚举值字符串（也限 500 字）。而且：

```python
model_config = ConfigDict(extra="forbid", frozen=True)
```

`extra="forbid"` 是这里真正的结构性保证：**多传一个字段会 `ValidationError`，而不是被悄悄接受**。所以
"Thought 无处可放"不是靠"记得别写"，是**模型定义里没有那个字段，而且加不进去**。这是 8.2 节"六个输出面
都没有 Thought"里的一个面。

`frozen=True` 则保证事件写进时间线之后不可再改——配合 8.13.1 的纯函数式追加，整条时间线是**只增不改**的。

同一条原则在本章出现了三次：`_observation_summary` 只报规模不报内容（8.11.7），`invalid_refs` 只报数量不
报 ID（8.8.5），事件模型 `extra="forbid"`（这里）。三处互不相干的地方，同一句话：**公开面只承载可核对的
事实。**

顺带看一眼 ID 生成的一致性：

```python
def _stable_id(prefix: str, *parts: str) -> str:
    digest = sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]
    return f"{prefix}_{digest}"
```

`"|".join(parts)` 用分隔符而不是直接拼接——docstring 说明了原因："分隔符避免部件简单拼接歧义"。
`("ab", "c")` 和 `("a", "bc")` 直接拼接会得到同一个字符串，加分隔符之后不会。截断到 16 位与
`ReactPublicEvent` 的 `pattern=r"^react_evt_[a-f0-9]{16}$"` **逐字对应**，改一处另一处立刻红。
docstring 同样主动澄清用途边界："截断只用于作品规模的可读审计引用，不用于认证、加密或全局安全唯一性。"

## 8.14 `_merge_evidence` / `_merge_tool_events`：冲突即 `ValueError`

两个函数结构相同：

```python
def _merge_evidence(existing: list[Evidence], incoming: list[Evidence]) -> list[Evidence]:
    by_id = {item.evidence_id: item for item in existing}
    for item in incoming:
        current = by_id.get(item.evidence_id)
        if current is not None and current != item:
            raise ValueError(f"conflicting Evidence payload for {item.evidence_id}")
        by_id.setdefault(item.evidence_id, item)
    return list(by_id.values())
```

**同 ID 且完全相等 → 保留首项**（合法重放，比如 MCP 重试或 checkpoint 恢复后重跑）。
**同 ID 但内容不同 → `ValueError`。**

第二条是本节的全部内容。三种可选做法：

| 做法 | 后果 |
|---|---|
| 新值覆盖旧值 | 一条已经被引用过的证据**内容变了**，报告的引用指向了另一份事实 |
| 保留旧值忽略新值 | 新观测被静默丢弃，模型下一轮看到的是过期事实 |
| 抛 `ValueError` | 整个 run 失败，但没有任何人基于错误事实下结论 |

docstring 解释了为什么选第三个：

> 若 ID 相同但结构不同，说明稳定 ID 或上游来源契约冲突，函数抛出 ValueError 而不是覆盖旧事实。
> **该异常属于实现/协议缺陷，不应伪装成安全降级结论。**

最后半句是关键。系统里有一套完整的降级机制（第 9 章的 `degraded` 报告、`uncertainties` 字段），但那套
机制是为**证据不足**设计的——"我查到的东西不够下结论"。而"同一个 ID 对应两份不同事实"不是证据不足，是
**证据系统本身坏了**。这时候产出一份带 `uncertainties` 的降级报告，等于用"结论不确定"掩盖"数据不可信"。

**降级要诚实到不掩盖 bug。** 一个只在证据冲突时才触发的 `ValueError` 会让这类缺陷在评测里立刻暴露；
一个"覆盖旧值"的实现会让它永远藏在正确的百分比后面。

`_merge_tool_events` 是同一个模式换成 `event_id`，但它的返回顺序有一条额外要求：

> 返回顺序保持**既有事件在前、新事件在后**，使 API 时间线与真实执行顺序一致。

`dict` 保序 + `setdefault` 恰好给出这个顺序，不需要额外排序。这两个函数是 8.11.4 那个"局部列表逐条合并"
循环的被调用方，所以整批 Observation 的合并顺序等于 `actions` 的顺序，也就是模型提交批次的顺序。

## 8.15 三个 span：本章对可观测性做的三个决定

第 12 章会完整讲 `run-trace:v1` 的实现（采集器、落库、`GET /api/v1/runs/{run_id}/trace` 回放）。本节只讲
ReAct 层用它时做的三个决定，因为它们是"怎么用 tracing"的通用问题。

`trace_span` 的签名只有三样东西：

```python
def trace_span(kind: TraceSpanKind, name: str, **attributes: AttributeValue) -> Iterator[SpanHandle]:
```

**决定一：`kind` 是七值枚举，不是自由字符串。** 本章用到两个：`REACT_STEP`（Planner 决策）和
`TOOL_CALL`（批次与单次调用）。`TraceSpanKind` 的 docstring 讲了为什么不给自由字符串：

> 枚举与三层嵌套 LangGraph、MCP 协议边界和两条检索通道一一对应，因此聚合视图天然按真实架构分组；
> **自由字符串会让同一段耗时在不同 run 里落到不同类别，事后无法比较。**

也就是说 span 的分类维度**必须是封闭集合**，否则"P95 里工具调用占多少"这个问题就没有稳定答案——有人写
`"tool"`，有人写 `"tool_call"`，聚合就散了。这和 8.10 节"路由只读枚举"是同一个理由的另一次应用。

**决定二：业务代码完全不传 span 参数。** docstring：

> 这是业务代码唯一需要用到的入口：**父子关系由 ContextVar 自动推导**，因此检索、MCP 与 Agent 模块不必
> 互相传递 span 参数，也不需要判断遥测是否开启。

对比另一种常见写法——把 `parent_span` 当参数一路往下传。那会污染每一层的函数签名：
`_execute_single_action(executor, action, *, react_step, parent_span)`，而且第 3 章的 MCP executor 也得跟着
加参数。ContextVar 方案让 tracing **完全不出现在业务接口里**。代价是要处理好上下文恢复：

```python
finally:
    # 必须在 span 结束前恢复父指针，否则同级后继 span 会错误地挂到刚结束的兄弟节点下。
    _CURRENT_PARENT.reset(token)
```

`reset(token)` 而不是 `set(旧值)`——这是 `ContextVar` 的正确用法（第 7 章 §7.12.2 讲过同一件事）。

**决定三：没绑定采集器时是零成本 no-op。**

```python
collector = _CURRENT_COLLECTOR.get()
if collector is None:
    yield _INERT_SPAN
    return
```

`_INERT_SPAN` 是一个共享的惰性对象，`annotate` / `mark` 都是空实现。所以单元测试和离线评测里那些
`with trace_span(...)` 一行开销都不多花，也不需要在测试里装配任何遥测。**这是"可观测性代码不能改变业务
代码结构"的必要条件**——如果不加遥测就跑不起来，测试就得开始 mock 采集器。

本章产生的 span 树：

```text
react.planner_decision   kind=react_step   入口: react_step, remaining_time_ms
                                           出口: decision_status, action_count, evidence_ref_count
react.tool_batch         kind=tool_call    入口: action_count, tool_names, react_step
  │                                        出口: ok_count, failed_count  [失败则 mark(ERROR)]
  ├─ react.tool_call     kind=tool_call    入口: tool_name, react_step
  │                                        出口: ok, error_code, attempt_count, evidence_count
  ├─ react.tool_call     ...
  └─ react.tool_call     ...
```

三个 span 名都是 `模块.动作` 形式的纯 ASCII 标识符，属性值也一样（这就是 8.11.3 那个 `"+".join(...)` 的
来源）。第 12 章会讲这条 ASCII 白名单是正则强制的，不是命名约定。

注意"入口属性"和"出口属性"的分工：开 span 时写**已知的输入**（第几步、剩余多少时间、要调几个工具），
`annotate` 写**结果**（成功几个、错误码、重试了几次）。这个分工让一条未闭合的 span（进程被杀）仍然带着
足够的输入信息说明它当时在干什么。

## 8.16 本章的设计取舍清单

| # | 决定 | 被否决的写法 | 否决理由 |
|---|---|---|---|
| 1 | 循环用真实 `StateGraph` | `while` 循环手写 | 节点/边可单测、可 checkpoint、可被外层图复用 |
| 2 | 状态是 Pydantic 模型 | `TypedDict` | 字段校验与不变量随状态走，而不是靠调用方自觉 |
| 3 | 依赖放 `context_schema` | 塞进 state | Planner/executor 不可序列化，进状态就无法 checkpoint |
| 4 | 绝对 deadline | 递减的 `remaining_ms` 字段 | 每个节点都要更新它，漏一次预算就永远花不完 |
| 5 | `monotonic()` 判耗时 | `time.time()` | 系统时钟回拨会让超时判断变成负数 |
| 6 | `astream` + `latest_state` | `ainvoke` | 超时是**降级**（保留已取证据）而不是回滚 |
| 7 | `asyncio.timeout` | `wait_for` 包整个 `async for` | `wait_for` 对异步生成器语义不清 |
| 8 | 单节点内 `gather` 扇出 | LangGraph 原生 fan-out | 指纹去重、预算记账、原子回写都需要一个统一决策点 |
| 9 | `recursion_limit = max_steps*2+6` | 依赖 LangGraph 默认值 | 显式第二道防线，且与业务预算同源 |
| 10 | 步数/时间门禁在模型调用**前** | 调用后再判 | 一次注定无效的调用是 10–16 秒加真金白银 |
| 11 | Prompt 注入 `min(并行度, 剩余步数)` | 恒定注入配置值 | 否则模型合规提交的批次也会被拒，白花一次调用 |
| 12 | 门禁失败**整批拒绝** | 截断成允许长度 | 部分执行让 Planner 基于"其余也执行了"的错误前提推理 |
| 13 | `parallel_limit` 与 `parallel_budget` 两个原因 | 合成一个 | 两者指向两种不同的修法 |
| 14 | 决策事件在门禁**之前**追加 | 之后 | 被拦截时唯一有用的信息就是"模型想调什么" |
| 15 | 批次决策事件 `tool_name=None` | 填第一个工具名 | 会让时间线读起来像只调了一个工具 |
| 16 | 假设引用与决策引用同一道门 | 只查决策引用 | 假设引用会成为报告根因的引用，那里最需要真实 |
| 17 | 假设投影在循环层 | 报告层自己收集 | 下一轮 Prompt 要看到已打折的假设；checkpoint 只存状态 |
| 18 | 投影在 `status` 判断**之前** | 之后 | `finish` 那一轮往往正是根因定稿的一轮 |
| 19 | 置信度确定性查表 | 让模型输出数字 | 模型报的"0.92"无法复算也无法反驳 |
| 20 | `CONFIRMED` 不在查表内 | 补一个默认值 | 让"Planner 自我确认"变成 `KeyError` 而不是静默通过 |
| 21 | 只有实时 Observation 能升 supported | 任何引用都算 | 否则"检索到相似案例"直接换到根因 |
| 22 | 未知 id + 非 `new` 静默忽略 | 抛错 / 静默创建 | 抛错让一次拼错毁掉整轮取证；创建会产出无症状结论 |
| 23 | `react_step += len(actions)` | `+= 1` | 并行度是性能参数，不能变成预算参数 |
| 24 | `return_exceptions=True` 后重抛 | 默认让首个异常传播 | 避免孤儿协程写乱 span、留下僵尸子进程 |
| 25 | `zip(..., strict=True)` | 默认 `zip` | 长度不等时静默截断 = 证据消失且指纹漏记 |
| 26 | 逐 Action 一条 Observation 事件 | 批次一条汇总 | "三个里一个失败"不能被压成一句无法追责的摘要 |
| 27 | 指纹先 pop `trace_id` | 全参数入哈希 | 否则 checkpoint 恢复后所有历史指纹失效 |
| 28 | `pop("trace_id")` 不给默认值 | `pop(..., None)` | 让"去重口径变了"炸出来而不是静默 |
| 29 | SHA-256 而非内置 `hash()` | `hash()` | `PYTHONHASHSEED` 让内置哈希跨进程不稳定 |
| 30 | 指纹可从 `ToolEvent` 重算 | 只存快照 | 派生数据可重算 → 快照可校验、算法可演进 |
| 31 | 事件 ID 由控制流决定 | UUID4 / 带时间戳 | 相同控制流重放得到相同 ID，评测可整条断言 |
| 32 | 事件追加用 `[*old, new]` | `old.append(...)` | 旧状态引用（`latest_state`、checkpoint）不能被就地改 |
| 33 | 停止原因校验**双向** | 只查"停止必须带原因" | 防止一条正常观测带上 `stop_reason` 被渲染成终止 |
| 34 | 事件模型 `extra="forbid"` | 允许额外字段 | Thought 不是"记得别写"，是**写不进去** |
| 35 | 证据 ID 冲突抛 `ValueError` | 覆盖 / 忽略 | 数据系统坏了不能伪装成"结论不确定"的降级 |
| 36 | 所有停止路径共用一条 `end` 边 | 按原因分边 | 十五种停法的后续处理完全相同 |
| 37 | `running` 无 Action 抛 `RuntimeError` | 转向 `end` | 控制器 bug 会伪装成"诊断莫名结束" |
| 38 | span `kind` 是枚举 | 自由字符串 | 分类维度不封闭 → 跨 run 无法聚合比较 |
| 39 | span 父子靠 ContextVar | 参数一路传递 | 遥测不该出现在业务函数签名里 |
| 40 | 无采集器时 no-op | 要求测试装配遥测 | 可观测性不能改变业务代码结构 |
| 41 | 峰值并发探针验证并行 | 断言总耗时变短 | 时间断言必然 flaky；峰值断言是确定性的 |

## 8.17 本章遗留的缺口

诚实起来，本章有三处**没有被测试守住**的地方：

**1. `_stop_graph_state` 的 `reason` 参数类型是 `ReactStopReason | str`。** 枚举分支安全，但 `str` 分支
（用于承载 Planner 自报原因）没有任何校验——传一个拼错的 `"evidence_sufficent"` 会一路写到 `AgentState`
和事件里。上游确实只传 `decision.stop_reason.value`，所以当前是安全的，但这是**调用方纪律而不是结构保证**。
把签名收紧成两个枚举的联合类型就能补掉它。

**2. `recursion_limit = self._config.max_steps * 2 + 6` 里的常数 6 没有对应测试。** 8.4.2 讲了它是第二道
防线，但如果哪天往图里加了两个节点，这个公式会静默变得偏紧——表现是长 run 在没有任何门禁触发的情况下抛
LangGraph 的 `GraphRecursionError`，而那个异常**不会**被翻译成 `stop_reason`（8.6 节的 `except` 只接
`TimeoutError`）。一个"节点数变化时公式必须重算"的断言能让它显式失败。

**3. `_observation_summary` 的失败分支没有断言"不含响应内容"。** 8.11.7 说摘要刻意只报规模，但守这条的
只有代码审查。第 7 章 §7.12 那个 8 键 `model_dump` 隐私断言是个好模板：对事件摘要做一次"不得出现响应体
关键字"的断言，就能把这条从纪律变成门禁。

三条都不是 bug，是**保证的强度不够**：现在靠调用方守规矩，将来靠不住。按 0.6 节的说法，这类地方才是面试
里真正会被追问的部分——"你怎么知道它不会退化"。

## 8.18 小结与下一章

第 8 章讲的其实只有一件事：**把一个开放式的"让模型自己决定调什么工具"的循环，变成一个每一步都有边界、
每一次停止都有原因、每一条结论都可追溯的确定性控制器。**

模型在这个循环里的角色被压缩得很小——它只做一件事：在给定的证据、假设和允许工具集下输出一个结构化决策。
其余全部由代码决定：

- **能不能调**：八道门禁，两道在花钱之前（8.8）
- **调几个**：`min(并行度, 剩余步数)`，且并行不买预算（8.8.2、8.11.5）
- **结论怎么算**：确定性投影 + 查表置信度 + 只认实时 Observation（8.9）
- **什么时候停**：控制器判定八种 + 模型自报七种，两套分开记（8.8.8）
- **用户看到什么**：8 字段、`extra="forbid"`、`frozen=True` 的事件模型（8.13）

第 7 章把"一次模型调用"做可靠，第 8 章把"一串模型调用"做有界。下一章处理最后一段：**模型说的话怎么变成
一份能对外交付的报告，以及谁有权否决它。**

第 9 章会讲 `app/reporting/` 与 `report_workflow.py`：确定性草稿生成器（为什么它只认
`AgentState.hypotheses` 而不读 `decision_summary`——8.9.1 那个事故的另一半）、`app/reporting/policy.py` 的
确定性规则、独立 Auditor 的四级阶梯，以及本章反复提到的那条非对称否决权：**规则说有问题就必须返工，模型的
"通过"覆盖不了它。**
