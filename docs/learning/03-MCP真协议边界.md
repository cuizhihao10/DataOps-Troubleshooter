# 第 3 章 MCP 真协议边界：九个只读工具怎么跨进程被调用

## 3.1 你会验证什么

```bash
.venv/Scripts/python -m pytest -q tests/unit/test_tooling_contracts.py tests/unit/test_observation.py
.venv/Scripts/python -m pytest -q tests/integration/test_mcp_protocol.py
.venv/Scripts/python -m pytest -q tests/integration/test_mcp_streamable_http.py
```

第二条命令走 stdio：它真的启动了一个 Python 子进程、真的做了 MCP 握手、真的跨管道传了 JSON-RPC。
第三条命令走生产传输：它在临时端口上拉起一个**真实 uvicorn 网关**（含鉴权中间件），再用生产客户端
打过去。如果你把 `app/mcp/client.py` 换成"直接 import Fixture 然后返回"，第一条命令仍然全绿，后两条
会立刻失败。

本章解决的问题是：**怎么让"Agent 调用了工具"这件事有独立证据，而不是一句自我声明。**

## 3.2 MCP 是什么，以及为什么不直接调函数

MCP（Model Context Protocol）是 Anthropic 提出的一套标准协议，用来让"模型宿主"和"工具提供方"解耦。
它规定了握手（`initialize`）、能力发现（`tools/list`）、调用（`tools/call`）等消息，传输层可以是
stdio 子进程，也可以是 Streamable HTTP。本项目两条都实现了，由 `mcp-transport:v1` 选定：**生产路径
是 Streamable HTTP**（网关是独立部署单元），stdio 保留为可选配置与"协议到底做了什么"的最短可读实现。
本章先按 stdio 讲清协议语义，再在 §3.4.7 起讲 HTTP 那一跳多出来的东西——这个顺序不是历史顺序，而是
因为**契约与传输无关**：九个工具名、注解和 `McpToolResponse` 在两条传输上逐字相同。

Java 读者可以类比 JDBC：`tools/list` 相当于读元数据，`tools/call` 相当于执行语句，而 stdio
子进程相当于驱动实现。你写代码时面对的是协议，不是某个具体实现。

那为什么不直接在 Agent 节点里 `read_fixture(scenario_id, tool_name)`？三个理由，按重要性排序：

1. **可信度。** 项目的卖点是"证据驱动"。如果"调用工具"只是同进程内的一次函数调用，那么"工具调用
   失败""工具超时""工具返回空"这些形态都是自己造的，没有任何东西能证明系统真的具备跨服务取证能力。
   跨进程协议让这些形态变成**真实发生的事**：子进程崩了就是真崩了，超时就是真的读不到数据。
2. **可替换性。** 九个工具今天读的是合成 Fixture，明天换成真实的 LTS/BDS/FlashSync API，客户端一行
   不用改——因为客户端只认协议。反过来，如果 Agent 直接读文件，"接真实系统"就是一次重写。
3. **边界纪律。** 工具进程只能做只读查询，它没有数据库连接、没有模型密钥、没有写权限。把它放进
   另一个进程，这条边界由操作系统保证，而不是由"我们约定不这么写"保证。

CLAUDE.md 里那句"禁止在 Agent 节点里直接读 Fixture 冒充工具调用"就是这一条的硬化表述。

## 3.3 服务端：`mcp_server/server.py` 只有 161 行

```python
mcp = FastMCP(
    name="dataops-troubleshooter-mock",
    instructions=...,
    stateless_http=True,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)

READ_ONLY_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)
```

`FastMCP` 是官方 Python SDK 提供的高层封装，负责协议细节（消息循环、Schema 生成、错误包装），
你只需要注册处理函数。

后两个构造参数只在 HTTP 传输下起作用，但必须写在这里（构造期），所以先说清楚：

- **`stateless_http=True`**：每个 HTTP 请求新建一次性 transport，服务端不累积会话表，横向扩容也不
  需要粘性路由。这与客户端"每次调用新建 MCP 会话"是同一个语义选择的两半。不这么配的后果不是报错
  而是**慢性泄漏**：SDK 的会话表在显式 DELETE 后并不删除条目（`streamable_http_manager.py:314-326`
  的清理分支要求 `not is_terminated`），于是每次工具调用都会留下一条永不回收的记录。
- **`transport_security=...(enable_dns_rebinding_protection=False)`**：这个必须**显式**关闭，"不传参"
  不等于"关闭"。FastMCP 在 `host` 属于 `127.0.0.1` / `localhost` / `::1`（默认值恰好就是
  `127.0.0.1`）时会自动开启防护，并把 allowed_hosts 限死为三个回环形式；容器里按 service 名访问的
  请求（`Host: mcp-gateway:8900`）因此会拿到 HTTP 421，而 api 的 lifespan 在启动期工具发现处失败、
  进程以退出码 3 结束。关闭它的理由是这层防护在本部署里保护不到任何东西：网关不发布宿主端口、不对
  浏览器开放，真正的门禁是 Bearer 令牌，而被 rebinding 诱骗的浏览器拿不到令牌，请求会先被挡成 401。

`ToolAnnotations` 这四个标记是**给宿主看的元数据**，不影响执行：`readOnlyHint=True` 告诉宿主"这个
工具不改变世界"，`idempotentHint=True` 表示"重复调用结果一致"，`openWorldHint=False` 表示"它不访问
开放外部世界"。宿主（包括未来可能接入的 Claude Desktop 之类的通用客户端）可以据此决定是否需要用户
逐次确认。

关键在于这个常量是**一份、共享给九个工具**的。如果每个工具各写一遍注解，早晚会有一个漏写
`readOnlyHint`，而漏写不会导致任何测试失败——它只是让"只读"这个承诺在协议层缺一块。

### 3.3.1 用一个循环注册九个工具

```python
def _register_tools() -> None:
    for tool_name, title, description, handler in (
        (ToolName.LTS_GET_TASK_STATUS, "LTS 任务状态", "...", _lts_get_task_status),
        ...
    ):
        mcp.add_tool(
            handler,
            name=tool_name.value,
            title=title,
            description=description,
            annotations=READ_ONLY_ANNOTATIONS,
            structured_output=True,
        )
```

这里的 `name=tool_name.value` 是第 1 章那个"唯一定义"手法的兑现：服务端注册名直接取自
`ToolName` 枚举，不是手写字符串。于是"客户端要调的名字"和"服务端注册的名字"从同一个符号来，
不可能拼错。

对比常见写法 `@mcp.tool()` 装饰器逐个声明九次：那样每个函数上方都有一段重复的注解和描述，加第十个
工具要复制粘贴。用元组表 + 循环之后，"九个工具形状完全一致"这件事在代码结构上就是显然的。

`structured_output=True` 要求处理函数的返回值按其类型注解生成 JSON Schema 并作为
`structuredContent` 返回，而不是仅仅塞进 `TextContent`。客户端因此可以直接 `model_validate`，
不需要解析自然语言文本。

### 3.3.2 `main()` 是一个传输开关，不是一个代码分支

```python
def main() -> None:
    settings = get_settings()
    if settings.mcp_transport == "stdio":
        # stdio 不开放网络端口，客户端以子进程方式通信，因此没有可鉴权的网络面。
        mcp.run(transport="stdio")
        return
    _run_streamable_http(settings)
```

同一份工具注册表在两条传输上暴露完全相同的九个工具与注解，因此切换传输不需要改动任何工具实现——
"传输是部署形态"这句话在代码里的形态就是这个函数只有五行。

`transport="stdio"` 意味着协议消息走标准输入输出。这带来一条**必须记住的纪律**：服务端进程绝对
不能往 stdout 打印任何非协议内容。一个 `print("debug")` 就会破坏 JSON-RPC 帧，表现为客户端解析
失败或握手挂死。调试信息只能走 stderr。HTTP 那一侧没有这条约束（协议走 socket，stdout 是纯日志），
但反过来多了一条 stdio 完全没有的东西：**一个可被扫描到的网络端点**。

```python
def _run_streamable_http(settings: Settings) -> None:
    import uvicorn

    guard = build_gateway_guard(settings)
    application = McpGatewaySecurityMiddleware(mcp.streamable_http_app(), guard)
    uvicorn.run(application, host=settings.mcp_http_host, port=settings.mcp_http_port, ...)
```

三个决定：

1. **不用 `mcp.run(transport="streamable-http")`。** 那条路径会直接把**未受保护的**应用交给 uvicorn，
   中间件根本没有插入点。要在应用前面强制令牌，就必须自己拿到 ASGI 应用再包一层。
2. **守卫在监听端口之前构造。** `build_gateway_guard` 在缺令牌时抛错，所以"忘配令牌"的后果是进程
   起不来，而不是先开始服务再指望没人扫到。这就是 fail-closed：九个工具虽然全部只读，暴露出去的
   却是整条链路的排障证据（任务状态、日志、依赖拓扑、一致性抽样）。
3. **`import uvicorn` 写在函数里。** stdio 与测试的导入路径因此不必拉起整套 HTTP 服务栈。

鉴权本身**一行都没有新写**：`mcp_server/security.py` 复用资源 API 的 `ApiSecurityGuard`（第 12 章讲
它的 SHA-256 摘要与定长比较），同一套先限流后鉴权的顺序、逐字相同的 401 响应体。两处各写一份的后果
是可预期的——其中一份会先长出"调试用后门"或"顺手放宽的 scheme 解析"。中间件写成纯 ASGI 而不是
Starlette 的 `BaseHTTPMiddleware`，因为后者会缓冲流式响应，而 Streamable HTTP 正是靠 SSE 长流返回
结果；`lifespan` 类型的 scope 必须原样透传，否则 `StreamableHTTPSessionManager.run()` 永不启动，
网关会对每个请求回 500。

## 3.4 客户端：两个实现、一个 Protocol，每个细节都在防一种挂死

上层（执行器、ReAct 循环、评测脚本）只依赖 `app/mcp/protocol.py` 里的 `McpToolClient` Protocol：

```python
class McpToolClient(Protocol):
    async def list_tools(self) -> tuple[str, ...]: ...
    async def list_tool_descriptors(self) -> tuple[McpToolDescriptor, ...]: ...
    async def call_tool(self, tool_name: ToolName, request: McpToolRequest) -> McpToolResponse: ...
```

`McpClientError`、`McpToolDescriptor` 和 `extract_payload` 也住在 `protocol.py`——它们与传输无关，
搬过去之后两个实现共用同一份出口校验，而不是各自复制一份"顺手改一点"的版本。`app/mcp/factory.py`
的 `create_mcp_client(settings)` 按配置返回其中之一，所以装配代码里没有任何 `if transport ==` 分支。

下面 §3.4.1–3.4.6 讲 stdio 实现（`app/mcp/client.py`），它是理解协议语义最短的路径；§3.4.7 起讲
生产实现（`app/mcp/streamable_http.py`）在网络那一跳上多出来的问题。

### 3.4.1 子进程参数：编码错误必须炸

```python
    def _server_parameters(self) -> StdioServerParameters:
        # 复制而非原地修改 os.environ，避免客户端配置影响当前进程和其他并发测试。
        environment = dict(os.environ)
        environment["PYTHONUTF8"] = "1"
        return StdioServerParameters(
            command=sys.executable,
            args=["-m", self._server_module],
            env=environment,
            cwd=str(self._cwd),
            encoding="utf-8",
            encoding_error_handler="strict",
        )
```

五个决定各有理由：

- **`command=sys.executable`**：用当前解释器，而不是字符串 `"python"`。虚拟环境里 `python` 可能指向
  系统解释器，那个解释器没装项目依赖，子进程会以 `ModuleNotFoundError` 立刻退出，而客户端看到的
  只是"握手失败"。docstring 直接写了这条：确保服务端与客户端共享同一个已安装依赖环境。
- **`args=["-m", module]`**：用模块形式启动，包导入路径与父进程一致，不依赖脚本文件位置。
- **`dict(os.environ)` 而不是就地改**：客户端不能污染当前进程的环境变量，否则并发测试之间互相
  影响。
- **`PYTHONUTF8=1`**：Windows 默认控制台编码是 GBK。本项目 Fixture 全是中文，走 GBK 会乱码。
- **`encoding_error_handler="strict"`**：这条最值得学。默认的 `replace` 会把无法解码的字节换成
  `�` 然后**继续**，于是一份证据内容被悄悄改写，而 JSON 结构完好、校验全过。`strict` 让
  编码问题变成一个立刻可见的异常。

这是本项目反复出现的取舍：**宁可失败，也不要"看起来成功的错误结果"。**

### 3.4.2 握手也必须有超时

```python
        try:
            # 总超时必须包住会话创建，否则服务进程卡在 initialize 时 read timeout 尚未生效。
            async with asyncio.timeout(self._timeout_seconds):
                async with self._session() as session:
                    result = await session.list_tools()
        except TimeoutError as exc:
            raise McpClientError("MCP list_tools timed out", ToolErrorCode.TIMEOUT) from exc
```

`ClientSession` 自带 `read_timeout_seconds`，但它只在会话建立**之后**生效。如果服务进程在
`initialize` 阶段就卡住（比如导入一个死循环模块），read timeout 还没开始工作，调用会永久挂起。
外层 `asyncio.timeout` 覆盖的正是这段空窗期——注释就是这么写的。

`call_tool` 里两层超时同时用上：

```python
            async with asyncio.timeout(self._timeout_seconds):
                async with self._session() as session:
                    result = await session.call_tool(
                        tool_name.value,
                        arguments=request.model_dump(mode="json"),
                        read_timeout_seconds=timedelta(seconds=self._timeout_seconds),
                    )
```

注释解释了为什么要两层："同时限制整个生命周期和单次读取，覆盖子进程启动慢与服务响应慢两类问题。"

一个有界 ReAct 循环（第 8 章）最怕的就是某一步永不返回：预算按步数和墙钟算，而墙钟超时靠
`asyncio.timeout` 生效——但如果内层的 await 不可取消或干脆不返回，整轮就烂在那里。所以"每个外部
等待都必须有超时"不是防御性编程的口号，而是有界性的前提。

`arguments=request.model_dump(mode="json")` 里的 `mode="json"` 也不是可选项：请求里有 `datetime`
字段，默认 `mode="python"` 会给出 `datetime` 对象，JSON-RPC 序列化时报错。`mode="json"` 输出
ISO 8601 字符串。

### 3.4.3 三段 `except` 的顺序是有含义的

```python
        except TimeoutError as exc:
            raise McpClientError(..., ToolErrorCode.TIMEOUT) from exc
        except McpClientError:
            raise
        except Exception as exc:
            raise McpClientError(..., ToolErrorCode.SERVICE_UNAVAILABLE) from exc
```

- `TimeoutError` → `TIMEOUT`：**可重试**（第 1 章的 `RETRYABLE_TOOL_ERRORS`）。
- `except McpClientError: raise` 这句看起来是废话，其实很关键：`extract_payload` 抛出的
  `INTERNAL_ERROR` 属于**不可重试**，如果没有这一句，它会被下面的 `except Exception` 捕获并
  改写成 `SERVICE_UNAVAILABLE`，于是一个确定性的协议错误被错分成瞬时错误，白白重试一次。
- 兜底的 `except Exception` → `SERVICE_UNAVAILABLE`：子进程启动失败、管道断开这类都属于此类，
  重试一次是合理的。

**错误分类错了，重试策略就错了**，而这条链路上唯一能做分类的地方就是这里——再往上执行器只读
`error_code`。

### 3.4.4 `_McpSessionContext`：两层上下文必须反序退出

SDK 给了两个异步上下文：`stdio_client`（拥有子进程和管道）和 `ClientSession`（拥有协议会话）。
它们必须**正序进入、反序退出**，而 Python 的 `async with A() as a, B(a) as b` 只能写在同一个
函数里，不能作为一个可复用的"会话工厂"返回。于是项目自己实现了一个上下文管理器：

```python
    async def __aenter__(self) -> ClientSession:
        # 第一层拥有子进程和 stdio 管道，必须比使用这些流的 ClientSession 更晚退出。
        self._stdio_context = stdio_client(self._parameters, errlog=sys.stderr)
        read_stream, write_stream = await self._stdio_context.__aenter__()

        # 第二层负责 MCP 消息会话；initialize 是协议协商，不可用“能写入管道”替代。
        self._session_context = ClientSession(read_stream, write_stream)
        session = await self._session_context.__aenter__()
        await session.initialize()
        return session

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        if self._session_context is not None:
            await self._session_context.__aexit__(exc_type, exc, traceback)
        if self._stdio_context is not None:
            await self._stdio_context.__aexit__(exc_type, exc, traceback)
```

三个细节：

1. **`__aenter__` 返回的是已经 `initialize` 完成的会话。** 调用方拿到 session 就可以直接用，不存在
   "忘了握手"的可能。注释点明了要害：不能用"能写入管道"替代协议协商。
2. **两个 `None` 检查**支持"第一层成功、第二层失败"的部分初始化状态。如果无条件 `__aexit__`，清理
   阶段会抛 `AttributeError`，把真正的原始异常覆盖掉——这是异步资源管理里最常见的一种 bug。
3. **`errlog=sys.stderr`**：子进程的 stderr 直连父进程 stderr。呼应 3.3.2 的纪律——协议走 stdout，
   人看的东西走 stderr。

`__aexit__` 不吞异常（返回 `None` 而非 `True`），所以原始错误还能一路传到 `except Exception` 被
分类成 `SERVICE_UNAVAILABLE`。

### 3.4.5 `extract_payload`：`isError` 必须先判

```python
    if result.isError:
        message = "\n".join(
            block.text for block in result.content if isinstance(block, TextContent)
        )
        raise McpClientError(message or "MCP tool returned an error",
                             ToolErrorCode.INTERNAL_ERROR)

    if result.structuredContent is not None:
        return result.structuredContent

    for block in result.content:
        if not isinstance(block, TextContent):
            continue
        try:
            payload = json.loads(block.text)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload

    raise McpClientError("MCP tool returned no structured JSON payload",
                         ToolErrorCode.INTERNAL_ERROR)
```

docstring 里那句话是本节的重点：**"`isError` 始终先处理，防止错误文本碰巧是 JSON 时被误当成功
响应。"** 服务端抛异常时，FastMCP 会把异常文本放进 `content`，而一个 Pydantic
`ValidationError` 的文本里就带 JSON 片段。如果先解析文本再看 `isError`，一次服务端崩溃可能被
读成一份合法业务响应。

`structuredContent` 优先于文本解析，理由是它已经由 SDK 解析过，二次转换只会引入偏差。文本 JSON
分支是"兼容旧返回形式的受限回退"，并且只接受**顶层是对象**的载荷——数组或裸标量不符合统一响应
Schema。

最后一行的 `raise` 也是一条设计立场：**客户端不允许凭空构造默认成功载荷。** 一个"什么都没解析到
就返回 `{}`"的实现会让 `McpToolResponse.model_validate` 因缺 `ok`/`observed_at` 而失败，报错位置
飘到无关的地方；这里直接给出 `INTERNAL_ERROR`，语义准确且不可重试。

注意最后一步：`return McpToolResponse.model_validate(payload)`。服务端返回的载荷在客户端**再校验
一遍**，注释写着"传输成功不代表业务契约合法"。这不是重复劳动——客户端不信任 Mock 服务端，等到换成
真实系统时，这条校验就是防契约漂移的唯一防线。

### 3.4.6 工具发现：注解缺失一律按 False 处理

```python
                    McpToolDescriptor(
                        name=tool.name,
                        read_only=bool(tool.annotations and tool.annotations.readOnlyHint),
                        destructive=bool(tool.annotations and tool.annotations.destructiveHint),
                        idempotent=bool(tool.annotations and tool.annotations.idempotentHint),
                        has_output_schema=tool.outputSchema is not None,
                    )
```

`tool.annotations` 和各个 hint 在协议里都是可选字段，可能是 `None`。这里的
`bool(x and x.readOnlyHint)` 把"没声明"折叠成 `False`——也就是**没声明只读就当作不是只读**。

这个方向选得对：如果折叠成 `True`，一个没有任何注解的服务端会被当成"九个只读工具都在"，安全门禁
形同虚设。保守方向的错误（把只读工具当成可能有副作用）代价是启动失败，激进方向的错误代价是把写
工具当只读放行。

`McpToolDescriptor` 只抽五个字段而不是保存整个 SDK 对象，docstring 给了理由：不把 SDK 类型泄漏到
应用领域层，并减少启动状态体积（`/health` 会投影它）。

### 3.4.7 为什么生产路径是 HTTP：与"被观测服务在不在云上"无关

先驳掉一条听起来很顺但不成立的推理："LTS/BDS/FlashSync 在云上，所以 MCP 必须走 HTTP。"传输选的是
**client↔server 那一跳**，也就是 Agent 到工具服务；工具服务自己用什么协议去访问被观测系统是另一件
事。一个 stdio 子进程完全可以在内部调用远端 API。真正站得住的是五条：

1. **审计记录点要在 Agent 信任边界之外。** stdio 下"谁在什么时候取了哪条证据"只有 Agent 自己的记录；
   HTTP 下网关是独立进程，它的访问日志不受 Agent 影响。
2. **多客户端复用同一套工具。** 人工排查、定时巡检、自愈流程、运维大盘都要这九个工具；stdio 意味着
   每个客户端各起一份子进程、各复制一份启动逻辑。
3. **限流闸门要放在被观测服务侧。** 保护 LTS 不被打爆是网关的职责，不是每个调用方自觉。
4. **凭据面收敛。** 三套后端凭据只需存在网关一处，Agent 只持有一个网关令牌。
5. **资源定界有现成答案。** 连接池、超时、健康检查 HTTP 生态都有；stdio 要自己写进程池化与孤儿
   进程回收。

反过来 stdio 的优势也很实在（零网络配置、无鉴权面、天然随进程回收），所以它不删——但**不再演进**：
不为它新增功能，也不为它新增测试。这条决定必须写下来，否则下一个读代码的人会以为两条路径地位相同，
然后在 stdio 上花时间。

### 3.4.8 共享连接池，但每次调用新建 MCP 会话

这是本章最值得记住的取舍。

```python
        self._http_client = httpx.AsyncClient(
            headers=headers,
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=False,
            trust_env=False,
        )
```

```python
    @asynccontextmanager
    async def _session(self) -> AsyncIterator[ClientSession]:
        async with streamable_http_client(self._url, http_client=self._http_client) as (...):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                yield session
```

实例持有**一个长寿命连接池**，每次 `list_tools` / `call_tool` 新建**一个一次性 MCP 会话**。池化省掉
每次调用的 TCP（以及将来的 TLS）握手，比 stdio 每次起一个解释器便宜得多；而会话一次性让"调用之间
完全隔离"这个性质保留下来——`execute_tools` 用 `asyncio.gather` 并发 1–3 个 Action 时不需要任何额外的
并发推理，因为没有共享的可变协议状态。

那为什么不干脆长期持有一个 `ClientSession`（那才是"HTTP 长连接"的极致形态）？因为 SDK 的
`streamable_http_client` 内部持有 anyio task group 与 cancel scope；`call_tool` 在 Worker 任务里被
`asyncio.timeout` 取消时，取消会穿进这套内部结构，而 enter/exit 跨任务的 cancel scope 是已知的踩坑
区。当前写法拿到了绝大部分收益，同时避开了这类难以复现的挂死。**这是取舍，不是偷懒**，所以它同时
写在模块 docstring、实现指南 5.2 和这里。

顺带两个 httpx 参数也值得记：`follow_redirects=False`（网关地址是显式配置的内网地址，跟随重定向等于
把 Bearer 令牌送去一个没打算信任的主机）和 `trust_env=False`（httpx 默认读环境变量与 Windows 注册表
里的系统代理，那会让网关令牌经一个不在信任边界内的代理转发，还会把"端口无人监听"变成代理侧读超时，
让 SERVICE_UNAVAILABLE 被误分类成 TIMEOUT）。令牌只进 `Authorization` 头、绝不进 URL，并且在构造期
就拒绝含空白或非 ASCII 的值——`\r\n` 会变成请求头注入，而 httpx 要到第一次真正调用工具时才报错，那时
进程已经对外宣称健康。

### 3.4.9 HTTP 特有的错误分类：401 不是瞬时故障

stdio 的失败形态是"子进程崩了/挂住了"；HTTP 多出一整族 HTTP 状态码。分类规则只有三行，但顺序有讲究：

```python
    for candidate in flattened:
        if isinstance(candidate, httpx.HTTPStatusError):
            status = candidate.response.status_code
            if status in {401, 403}:
                return (ToolErrorCode.PERMISSION_DENIED, ...)
            return (ToolErrorCode.SERVICE_UNAVAILABLE, ...)
    for candidate in flattened:
        if isinstance(candidate, httpx.TimeoutException):
            return (ToolErrorCode.TIMEOUT, ...)
```

- **状态码优先于超时。** 网关在 401 之后关闭连接可能顺带产生一次读超时；若先看超时，"令牌配错了"会
  被判成瞬时故障并白重试一次。
- **401/403 → `PERMISSION_DENIED`**，它不在 `RETRYABLE_TOOL_ERRORS`（只含 TIMEOUT 与
  SERVICE_UNAVAILABLE）里，所以错令牌恰好产生一个 ToolEvent。反过来把它归成 SERVICE_UNAVAILABLE 的
  代价是：一个配置错误会在每个 Action 上都翻倍消耗预算。
- **异常要先展平。** SDK 用 `tg.start_soon` 派发请求，因此一个 401 会以 `BaseExceptionGroup` 的形式
  冒出 anyio task group，只看最外层类型只能得到"某个组失败了"。`_flatten_exceptions` 递归展开，让
  分类规则与传输实现细节解耦。
- **兜底只报异常类型名**（`ConnectError` / `ReadError`），不搬第三方消息原文：ToolEvent 是对外可见的。

`_guarded` 里的外层 `asyncio.timeout` 与 stdio 同理——它必须包住**会话创建**，因为网关接受了 TCP
连接却不回 `initialize` 时，单次读超时还没开始计。`aclose()` 幂等，且关闭后的调用直接返回分类错误
而不是等 httpx 抛 "client has been closed"：那条消息随版本变化，一旦变了分类就退化成兜底值。

## 3.5 执行器：重试语义的唯一实现处

`app/mcp/executor.py` 只有 110 行，但它是"一次工具调用"的全部控制流。

### 3.5.1 重试预算在构造期就被钉死

```python
        if retry_count not in {0, 1}:
            raise ValueError("retry_count must be 0 or 1")
```

不是 `min(retry_count, 1)`，是直接拒绝。理由和第 2 章的配置门禁一致：静默截断会让一个配了 5 的
部署以为自己在重试 5 次。而且这条上界和第 1 章 `ToolEvent.attempt: Field(le=2)` 是同一条边界的
两处表达——事件模型装不下第三次尝试。

### 3.5.2 循环写法：先记录，再判断

```python
        observations: list[ToolObservation] = []
        for attempt in range(1, self._retry_count + 2):
            # 无论成功失败都先记录尝试，确保后续成功不会抹掉首次超时的审计事实。
            observation = await self._execute_once(action, attempt=attempt)
            observations.append(observation)

            # 只有预先批准的瞬时错误值得重复同一只读调用，其余结果直接成为终态。
            if observation.response.error_code not in RETRYABLE_TOOL_ERRORS:
                break
        return merge_observations(observations)
```

四个点值得注意：

1. **`range(1, retry_count + 2)`**：`attempt` 从 1 开始计数，总次数 = 初次 + 重试预算。写成
   `range(retry_count + 1)` 也能跑，但 `attempt` 会从 0 开始，与协议事件里的"第几次尝试"语义错位。
2. **成功也走同一条判断。** 成功响应的 `error_code is None`，而 `None not in RETRYABLE_TOOL_ERRORS`
   为真，于是立刻 break。没有 `if observation.response.ok: break` 这种特例分支——**成功只是"不可
   重试"的一种**。这让整个函数只有一个判据。
3. **`append` 在判断之前。** 这是"重试成功也保留首次失败"的实现处。如果写成"失败就丢掉再试"，
   `tool_attempt_success_rate` 这个指标就没有分母了。
4. **`merge_observations` 决定终态。** 执行器自己不选"哪个响应算数"，交给适配器（见 3.6.3）。

### 3.5.3 客户端异常必须变成"空证据的失败响应"

```python
            try:
                response = await self._client.call_tool(action.tool_name, action.arguments)
            except McpClientError as exc:
                # 传输层没有可信工具事实，因此失败响应必须保持空 evidence，避免制造伪证据。
                response = McpToolResponse(
                    ok=False,
                    data={},
                    evidence=[],
                    error_code=exc.error_code,
                    error_message=str(exc)[:1000],
                    observed_at=datetime.now(UTC),
                )
```

这段代码把"异常"翻译成"领域事实"。翻译的三条纪律：

- **`evidence=[]`**：传输失败没有任何可信工具事实。假如这里塞一条 `content="调用超时"` 的证据，
  它就会带着 `evidence_id` 进入报告的 `evidence_refs`，于是"超时"变成了可引用的根因依据——第 1 章
  §1.7 那条"失败响应不可能携带 Evidence"的不变量在这里落地。
- **`str(exc)[:1000]`**：截断。底层传输异常的文本可能极长（带完整 traceback 的子进程输出），而
  `error_message` 字段限长 1000。截断在这里做，而不是让 Pydantic 抛校验错——否则"工具失败"会被
  升级成"程序崩溃"。
- **`ok=False` 配 `error_code`**：满足 `validate_success_or_error` 的互斥要求，否则这个对象根本
  构造不出来。

注意 `except` 只抓 `McpClientError`。其他异常（比如编程错误）不被吞掉，会一路抛到 ReAct 循环，
在那里变成一个明确的失败终态，而不是伪装成"工具不可用"。

### 3.5.4 每次尝试一个 span

```python
        with trace_span(
            TraceSpanKind.TOOL_CALL,
            "mcp.tool_attempt",
            tool_name=action.tool_name.value,
            attempt=attempt,
        ) as span:
            ...
            span.annotate(
                ok=response.ok,
                error_code=(response.error_code.value if response.error_code else "none"),
                evidence_count=len(response.evidence),
            )
            if not response.ok:
                span.mark(TraceSpanStatus.ERROR)
```

注释解释了粒度选择：

> 每次尝试单独开 span：重试对上层是透明的，但"第一次超时、第二次成功"是可靠性分析的关键事实，
> 如果只给整个 Action 一个 span，重试延迟会被平均掉而无法归因。

`error_code` 属性用 `"none"` 字符串而不是 `None`——span 属性值受 ASCII 标识符白名单限制（第 12 章），
类型统一为字符串可以避免 `None` 在不同后端被渲染成 `null`/`"None"`/空值三种形态。

`span.annotate` 里刻意**没有**证据内容，只有 `evidence_count`。可观测性出口从设计上就不承载业务
文本，这是"绝不外泄"边界的一部分。

### 3.5.5 时间戳采样位置

```python
        started_at = datetime.now(UTC)
        with trace_span(...) as span:
            ...
        # completed_at 在异常标准化后采集，使事件耗时覆盖错误映射但不包含后续模型处理。
        completed_at = datetime.now(UTC)
```

`completed_at` 在 `with` 块之后、`normalize_observation` 之前取值。含义很精确：事件耗时 = 传输 +
错误归一化，不含后续的证据构造和模型处理。

这类"耗时到底算到哪"的决定通常没人写下来，于是后来读指标的人不知道 `p95` 里包不包含序列化。这里
用一行注释把口径固定了——评测报告里的延迟数字才有可比性。

## 3.6 Observation 适配器：把协议载荷变成可引用证据

`app/mcp/observation.py` 是第 1 章那个"两个证据模型"设计的兑现处：服务端给
`ToolEvidencePayload`（三字段），这里补齐成领域 `Evidence`（七字段）。

### 3.6.1 `ToolObservation` 是执行器写回状态前的中间边界

```python
class ToolObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    response: McpToolResponse
    evidence: list[Evidence] = Field(default_factory=list)
    tool_events: list[ToolEvent] = Field(min_length=1)
    observation_refs: list[str] = Field(default_factory=list)
```

`tool_events: Field(min_length=1)` 说的是"一次观察至少对应一次真实尝试"——不存在"没调用过的观察"。
`evidence` 可以为空（失败），`observation_refs` 可以为空（同上），但事件不行。

还有一个便捷视图：

```python
    @property
    def tool_event(self) -> ToolEvent:
        return self.tool_events[-1]
```

docstring 特意声明："完整审计仍应使用 `tool_events`；该属性不删除前序失败，只提供便捷视图。"
这种注释值得学——一个"取最后一个"的属性很容易被误当成"这次调用的事件"，从而在审计代码里悄悄丢掉
重试历史。

### 3.6.2 稳定 ID：为什么不用 UUID

```python
def _stable_id(prefix: str, *parts: str) -> str:
    digest = sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]
    return f"{prefix}_{digest}"
```

证据 ID 是内容摘要，不是随机数。这带来两个性质：

1. **可重放。** 同一个 `(trace_id, tool_name, 请求, source_id)` 组合永远得到同一个 `ev_xxxx`。评测
   要复现一次诊断、要比较两次运行引用了同样的证据，靠的就是这个。UUID 会让每次运行的引用集合都
   不同，无法比较。
2. **同来源自动去重。** 重试成功时，两次尝试对同一个 `source_id` 生成同一个 ID，`merge_observations`
   用字典一合就去掉了重复。

哪些东西进摘要，是这里最讲究的一点：

```python
            evidence_id=_stable_id(
                "ev",
                action.arguments.trace_id,
                action.tool_name.value,
                request_identity,
                item.source_id,
            ),
```

注释写着：**"证据 ID 不包含可变自然语言内容，避免措辞微调破坏同一来源在报告中的稳定引用。"**
也就是 `item.content` 故意不参与摘要。如果 content 进摘要，Fixture 里改一个标点，所有引用 ID 全变，
Golden 案例里写死的期望值全部失效。ID 标识的是**来源**，不是**文本**。

事件 ID 多了一个 `attempt`：

```python
    # attempt 进入事件 ID，使一次重试的两个事件各自可寻址，同时共享同一 trace。
    event = ToolEvent(
        event_id=_stable_id("evt", action.arguments.trace_id, tool_slug, request_identity,
                            str(attempt)),
```

如果不加 `attempt`，两次尝试会得到同一个 `event_id`，写库时主键冲突，或者更糟：被当成同一条事件
覆盖掉，首次失败就消失了。

`_request_identity` 保证"同工具不同请求"不共享 ID：

```python
    return json.dumps(
        action.arguments.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
```

`sort_keys=True` 是关键——字典顺序不影响身份。`separators=(",", ":")` 去掉空格，`ensure_ascii=False`
让中文按原样参与（反正最后要 UTF-8 编码成字节）。三个参数一起把"同一请求"定义成一个规范形式。

### 3.6.3 可靠性由客户端赋值，且失败响应也给分

```python
            reliability=0.95 if response.ok else 0.3,
```

第 1 章说过：可靠性是审计属性，不能由被审计方声明。这里就是唯一赋值处，一个确定性规则。

`0.95` 而不是 `1.0`：即使协议成功，工具读到的也是某一时刻的快照，可能已经过期。留出余量，让"实时
工具证据"和未来可能出现的更强来源之间还有序关系。

`else 0.3` 这一支看起来矛盾——3.5.3 不是说失败响应必须 `evidence=[]` 吗？那这个分支怎么会走到？
区别在于**谁失败**：

- 传输失败（`McpClientError`）→ 执行器构造 `evidence=[]` → 循环体不产生任何 Evidence。
- 业务失败（服务端返回 `ok=false` 却带了 evidence）→ 这里给 0.3。

第二种情况在当前 Mock 服务端不会出现，但客户端不假设服务端守规矩。给低可靠性而不是丢弃，是因为
"接了真实系统之后"确实可能出现"部分失败但仍有片段数据"的响应，那时低分证据比无证据更有用——而
0.3 的分值会让它在精排（第 5 章）里排到很后面。

### 3.6.4 `merge_observations`：证据去重，事件不去重

```python
    if not observations:
        raise ValueError("at least one tool observation is required")

    # 字典保持首次插入顺序；相同来源重放不会重复污染证据集合。
    evidence_by_id = {
        item.evidence_id: item for observation in observations for item in observation.evidence
    }
    # 事件采用扁平列表完整串联，终态 response 则只取最后一次尝试，二者语义刻意不同。
    return ToolObservation(
        response=observations[-1].response,
        evidence=list(evidence_by_id.values()),
        tool_events=[event for observation in observations for event in observation.tool_events],
        observation_refs=list(evidence_by_id),
    )
```

三种字段三种合并语义，这是本函数的全部内容：

| 字段 | 合并方式 | 为什么 |
|---|---|---|
| `response` | 取最后一次 | 终态就是最后一次尝试的结果 |
| `evidence` | 按 ID 去重 | 同来源重放不该在报告里出现两次 |
| `tool_events` | 全部保留 | 每次真实调用都是审计事实 |

`raise ValueError` 那一句针对的是"执行器控制流错误"——空列表意味着循环一次都没跑，那是 bug，
不能用"构造一个没有事件的观察"来掩盖（而且 `Field(min_length=1)` 也不允许）。

Python 3.7+ 的字典保持插入顺序，所以 `list(evidence_by_id)` 给出的引用顺序是首次出现顺序，
稳定可比。

### 3.6.5 `observation_refs` 只引用实际创建的证据

```python
    # observation_refs 只引用实际创建的证据；失败响应因此自然得到空引用列表。
    return ToolObservation(
        ...
        observation_refs=[item.evidence_id for item in evidence],
    )
```

"自然得到"这个说法很准确：不需要写 `if not response.ok: observation_refs = []`，因为空 evidence
列表推导出来就是空引用。**不变量由数据流保证，不由分支保证**——少一个 if 就少一个写错的地方。

`observation_refs` 会进入 `AgentState.observation_refs`，Planner 下一轮能看到"这一步拿到了哪些
可引用 ID"，报告里的 `evidence_refs` 必须来自这个集合。

## 3.7 集成测试怎么证明"真协议"

`tests/integration/test_mcp_protocol.py` 有二十多个用例，全部走真实子进程。几个代表：

```python
async def test_real_mcp_protocol_lists_read_only_lts_tool() -> None:
    """验证独立 FastMCP 进程通过 list_tools 暴露完整九工具及统一安全注解。"""

async def test_action_crosses_mcp_protocol_and_becomes_observation() -> None:
    """验证成功的 LTS Action 穿过真实 MCP 后生成响应、证据引用和单次 ToolEvent。"""

async def test_mcp_failure_response_is_preserved_without_fake_evidence() -> None:
    """验证 EMPTY_RESULT 作为非瞬时失败原样保留，且不会重试或生成伪 Evidence。"""

async def test_transient_mcp_failure_retries_once_and_preserves_both_events() -> None:
    """验证 TIMEOUT 恰好重试一次，并保留两个具有不同稳定 ID 的失败事件。"""

async def test_bds_permission_denied_is_not_retried_or_turned_into_evidence() -> None:
    """验证权限拒绝不会因重试预算而重复调用，也不会被包装成可信证据。"""
```

注意这些测试名的写法——它们是**边界的自然语言表述**，而不是 `test_call_tool_2`。第 0 章那条建议
"先跑测试再读实现"之所以成立，就是因为测试名本身构成了一份边界清单。

后半部分的用例已经不只是协议测试，而是**跨组件事实一致性**测试：

```python
async def test_lts_to_bds_dependency_chain_crosses_real_mcp_protocol() -> None:
    """验证 LTS 失败现象可沿真实 MCP 工具结果追到 BDS 分区读取阻塞。"""

async def test_customer_profile_schema_failure_propagates_across_real_mcp_protocol() -> None:
    """验证同一 600 条 Schema 缺口经真实 MCP 从 FlashSync 传播到 BDS 和 LTS。"""
```

它们断言的是"同一个数字在三个组件的工具返回里对得上"（600 条缺口、1200 位点回退）。这类测试保护的
不是代码，而是 **Fixture 数据的自洽性**：如果 FlashSync 说丢了 600 条而 BDS 说缺了 500 条，那么无论
模型多聪明都推不出正确根因，而评测失败会被误读成"模型能力不足"。第 14 章讲评测诚实性时会回到这一点。

`tests/integration/test_mcp_streamable_http.py` 对生产传输做同样的事，但它必须自己**拉起一个真实
uvicorn 网关**（session 级 fixture，端口用预绑定 socket 交给 uvicorn，避免"先随机选端口再祈祷没被
占用"的竞态）。不用 `ASGITransport` 在内存里直连应用，因为鉴权中间件的真实位置、TCP 连接被拒、跨进程
JSON-RPC 往返恰好就是这条传输新增的失败面，内存替身会把它们全部抹掉。

九工具与注解的断言与 stdio 文件**逐字相同**，两者因此构成"契约与传输无关"的对照组。剩下的用例覆盖
HTTP 独有的失败面：

```python
async def test_rejected_token_is_permission_denied_without_retry(...) -> None:
    """验证缺令牌/错令牌/错 scheme 都归 PERMISSION_DENIED，且执行器不重试（只有一个 ToolEvent）。"""

async def test_service_name_host_header_reaches_the_mcp_app(...) -> None:
    """验证按 service 名访问（Host: mcp-gateway:8900）不会被 DNS rebinding 防护挡成 421。"""

async def test_unreachable_gateway_is_service_unavailable_and_retries_once() -> None:
    """验证端口无人监听时归 SERVICE_UNAVAILABLE，并按预算恰好重试一次。"""

async def test_concurrent_calls_share_one_pool_with_independent_sessions(...) -> None:
    """验证三个并发 call_tool 全部成功——共享池 + 独立会话的组合真的成立。"""

def test_gateway_healthcheck_probe_covers_both_legs(...) -> None:
    """验证容器探针两段判定：匿名 GET 必须 401，带令牌的 initialize 必须打到 MCP 应用。"""
```

最后那条是**给探针本身做的测试**，理由见第 13 章：`api` 用 `service_healthy` 当启动闸门，探针放过的
故障就是 compose 会放过的故障。421 那次正是这么跑到容器退出码 3 的。

## 3.8 本章小结

| 边界 | 实现处 | 如果不这么写会怎样 |
|---|---|---|
| 工具调用必须跨真实协议 | stdio 子进程 / HTTP 网关 | "调用了工具"变成自我声明 |
| 上层只依赖 Protocol | `McpToolClient` + `create_mcp_client` | 换传输要改执行器与循环 |
| 服务端注册名与枚举同源 | `name=tool_name.value` | 客户端与服务端可能拼错不一致 |
| 只读注解一份共享 | `READ_ONLY_ANNOTATIONS` | 某个工具漏声明且无人发现 |
| 编码错误必须炸 | `encoding_error_handler="strict"` | 中文证据静默乱码且校验全过 |
| 握手也要有超时 | 外层 `asyncio.timeout` | 服务卡在 initialize 时永久挂起 |
| 协议错误不可重试 | `except McpClientError: raise` | 确定性错误被误分类并白重试 |
| 401 不是瞬时故障 | 状态码分类先于超时分类 | 令牌配错在每个 Action 上翻倍消耗预算 |
| 网络端点 fail-closed | `build_gateway_guard` 缺令牌抛错 | 全链路排障证据裸奔在网络上 |
| 池共享但会话一次性 | `http_client=` 注入 + 每调用新会话 | 要么每次重握手，要么并行批次需要并发推理 |
| `isError` 先判 | `extract_payload` 首个分支 | 错误文本恰好是 JSON 时被当成功 |
| 失败不生成证据 | `evidence=[]` + 空引用推导 | "超时"变成可引用的根因依据 |
| 重试成功保留首次失败 | 先 `append` 后判断 | 真实失败率不可测 |
| 引用 ID 稳定可重放 | `_stable_id` + `sort_keys` | 评测无法复现、无法比较 |
| 每次尝试可归因 | per-attempt span + `attempt` 进 ID | 重试延迟被平均掉 |

一句话总结本章：**协议边界的价值不在于"能调通"，而在于失败形态是真实的。** 超时、权限拒绝、空结果
这三种形态在本项目里都能被真实触发、被正确分类、被完整记录——这才是后面所有评测数字的地基。换到
生产传输后又多了两种真实形态（401 与连接被拒），而它们同样有各自的分类与各自的测试。

下一章看 `app/capabilities/`：五项固定能力为什么是"配置"而不是"Agent"。

