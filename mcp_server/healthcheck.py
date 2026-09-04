"""MCP 网关容器的存活探针：只用标准库，两段断言分别覆盖鉴权位置与 MCP 应用可达性。

这个探针的判定会变成 `api` 的启动闸门（compose 里 `depends_on: mcp-gateway: service_healthy`），
所以"网关 healthy"这句话值多少钱，完全取决于断言强度。只探"端口能连上"的版本会放过两类真实故障：

1. 装配时漏掉鉴权中间件——端口照样监听，匿名请求却能直接打到九个工具；
2. MCP 应用按**部署主机名**访问时被挡下——本项目实测过这一类：FastMCP 在 host 为回环地址时自动
   开启 DNS rebinding 防护，`Host: mcp-gateway:8900` 因此拿到 421，而端口探针一路 healthy，故障
   只在 `api` 容器启动期工具发现失败、进程以退出码 3 结束时才暴露。

因此本探针跑两段：不带令牌的 GET 必须被挡成 401（证明中间件在应用前面），带令牌且带真实 Host 的
`initialize` 必须拿到 200 且回出 protocolVersion（证明令牌与服务端一致、MCP 应用在部署地址下可达）。
两段合起来才等价于"api 容器接下来要走的那条路已经通了"。

刻意只依赖标准库，并且刻意不导入 `mcp_server.server`：探针每 10 秒起一个新进程，导入服务模块会在
探针里重建一份 FastMCP 和九个工具注册表，而"导入出来的应用是好的"根本不能证明"正在服务的那个进程
是好的"——探针必须走真实回环 TCP。
"""

from __future__ import annotations

import http.client
import json
import os
import sys

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8900
DEFAULT_PATH = "/mcp"
PROBE_TIMEOUT_SECONDS = 4.0

# 与客户端 SDK 协商用的版本号；服务端若只支持别的版本会回自己的版本而不是报错，所以这里断言的是
# "回出了 protocolVersion"，不是"回出了这个具体版本"——把探针绑死在某个协议版本上会让一次正常的
# SDK 升级表现为部署失败。
_PROBE_PROTOCOL_VERSION = "2025-06-18"

_INITIALIZE_BODY = json.dumps(
    {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": _PROBE_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "dataops-gateway-healthcheck", "version": "1.0"},
        },
    },
    separators=(",", ":"),
).encode("utf-8")


def probe(
    *,
    token: str | None,
    host_header: str,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    path: str = DEFAULT_PATH,
    timeout_seconds: float = PROBE_TIMEOUT_SECONDS,
) -> tuple[bool, str]:
    """按顺序执行两段探测，返回 (是否健康, 可诊断原因)。

    顺序是有意义的：先确认鉴权中间件在位，再用令牌穿过它去确认 MCP 应用可达。反过来做的话，一个
    "中间件没装但应用正常"的进程会在第二段先通过，第一段的失败原因就被淹没在一条成功日志后面。
    返回原因字符串而不是直接打印或抛异常，是为了让集成测试能对同一个函数断言，而不是复制一份逻辑。
    """

    if token is None:
        # 令牌缺失属于探针自身配置错误，必须与"网关拒绝了令牌"区分开：后者说明两个 service 的
        # 令牌不一致，前者说明 compose 的环境变量映射漏了一行，处置动作完全不同。
        return False, "probe misconfigured: gateway auth token is not set in the environment"
    ok, reason = _probe_security_boundary(
        host=host, port=port, path=path, host_header=host_header, timeout_seconds=timeout_seconds
    )
    if not ok:
        return False, reason
    return _probe_mcp_application(
        token=token,
        host=host,
        port=port,
        path=path,
        host_header=host_header,
        timeout_seconds=timeout_seconds,
    )


def _probe_security_boundary(
    *,
    host: str,
    port: int,
    path: str,
    host_header: str,
    timeout_seconds: float,
) -> tuple[bool, str]:
    """断言匿名 GET 被挡成 401，以此证明鉴权中间件确实插在 MCP 应用之前。

    401 比"端口可连接"强得多：它同时证明 uvicorn 在服务、中间件在位、且拒绝路径能完整写出响应。
    任何 2xx/404/405 都意味着匿名请求已经进到应用里，属于必须让容器 unhealthy 的部署事故。
    """

    status, _, error = _request(
        method="GET",
        host=host,
        port=port,
        path=path,
        host_header=host_header,
        headers={},
        body=None,
        timeout_seconds=timeout_seconds,
    )
    if error is not None:
        return False, f"gateway not serving: {error}"
    if status != 401:
        return False, f"auth middleware missing or bypassed: anonymous GET returned {status}"
    return True, "auth middleware rejects anonymous requests"


def _probe_mcp_application(
    *,
    token: str,
    host: str,
    port: int,
    path: str,
    host_header: str,
    timeout_seconds: float,
) -> tuple[bool, str]:
    """带令牌与真实 Host 发一次 initialize，断言 200 且回出 protocolVersion。

    连接仍打向回环地址而 Host 头写部署名，正是容器网络里的形态：传输层连的是 service 的 IP，应用层
    看到的是 service 名。这样探针不依赖容器内 DNS 就能覆盖"按部署名访问会不会被主机名校验挡下"。
    """

    status, body, error = _request(
        method="POST",
        host=host,
        port=port,
        path=path,
        host_header=host_header,
        headers={
            "Authorization": f"Bearer {token}",
            # MCP Streamable HTTP 要求 POST 同时接受两种响应形态；少一个服务端直接回 406，那会被
            # 误读成"应用不可达"，因此这两个头必须与生产客户端一致。
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        },
        body=_INITIALIZE_BODY,
        timeout_seconds=timeout_seconds,
    )
    if error is not None:
        return False, f"mcp initialize failed: {error}"
    if status == 401:
        return False, "gateway rejected the probe token: api and gateway tokens disagree"
    if status == 421:
        return False, f"host {host_header} rejected by transport security: initialize returned 421"
    if status != 200:
        return False, f"mcp initialize returned {status}"
    version = _extract_protocol_version(body)
    if version is None:
        return False, "mcp initialize response carried no protocolVersion"
    return True, f"mcp app reachable as {host_header}, protocol {version}"


def _extract_protocol_version(body: bytes) -> str | None:
    """从 JSON 或 SSE 形态的响应体里取出协商到的协议版本，取不到返回 None。

    默认部署下 `json_response=False`，initialize 的结果包在 `event: message` / `data: {...}` 的 SSE
    帧里，所以不能直接 `json.loads` 整个响应体；只做子串匹配又太弱——一条格式正确但结果为错误对象
    的响应也含有这个词。这里解析出真实字段，让失败原因能区分"应用没答"和"应用答了个错误"。
    """

    text = body.decode("utf-8", "replace")
    payloads = [
        line[len("data:") :].strip() for line in text.splitlines() if line.startswith("data:")
    ]
    payloads.append(text.strip())
    for candidate in payloads:
        if not candidate:
            continue
        try:
            message = json.loads(candidate)
        except ValueError:
            continue
        if not isinstance(message, dict):
            continue
        result = message.get("result")
        if isinstance(result, dict) and isinstance(result.get("protocolVersion"), str):
            return result["protocolVersion"]
    return None


def _request(
    *,
    method: str,
    host: str,
    port: int,
    path: str,
    host_header: str,
    headers: dict[str, str],
    body: bytes | None,
    timeout_seconds: float,
) -> tuple[int | None, bytes, str | None]:
    """发一次 HTTP 请求并返回 (状态码, 响应体, 错误说明)，异常一律转成错误说明。

    探针不能让任何异常逃出去：容器 healthcheck 只看退出码，一条 Python traceback 会被塞进
    `docker inspect` 的 Health.Log，把"连接被拒"这种一眼可判的信息埋在十行栈里。超时靠 socket
    超时兜住，因此一个挂住不回的 SSE 流也会按 unhealthy 处理，而不是把探针本身挂死。
    """

    connection = http.client.HTTPConnection(host, port, timeout=timeout_seconds)
    try:
        connection.request(method, path, body=body, headers={"Host": host_header, **headers})
        response = connection.getresponse()
        return response.status, response.read(), None
    except OSError as exc:
        # 只报异常类型与消息，绝不回显请求头：Health.Log 会被 `docker inspect` 持久化，令牌一旦
        # 进到那里就等于写进了容器元数据。
        return None, b"", f"{type(exc).__name__}: {exc}"
    except http.client.HTTPException as exc:
        return None, b"", f"{type(exc).__name__}: {exc}"
    finally:
        connection.close()


def main(argv: list[str] | None = None) -> int:
    """compose healthcheck 入口：读取环境与部署主机名，打印原因并以 0/1 表示健康。

    令牌从环境变量读而不从命令行读：`docker inspect` 会公开容器的 Cmd 与 healthcheck 配置，把令牌
    写进 argv 等于写进容器元数据。部署主机名反过来必须走 argv——它不是秘密，而且写在 compose 的
    探针命令里，正好让"探针用哪个 Host 探"这件事在部署描述里一眼可见。
    """

    arguments = sys.argv[1:] if argv is None else argv
    port = int(os.environ.get("DATAOPS_MCP_HTTP_PORT", DEFAULT_PORT))
    host_header = arguments[0] if arguments else f"{DEFAULT_HOST}:{port}"
    ok, reason = probe(
        token=os.environ.get("DATAOPS_MCP_AUTH_TOKEN"),
        host_header=host_header,
        port=port,
    )
    print(reason)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
