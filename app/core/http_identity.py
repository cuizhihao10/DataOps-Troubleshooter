"""统一本项目所有出站模型/检索 HTTP 客户端的身份标识。

这里存在的唯一原因是兼容性实测结果：OpenAI 官方 Python SDK 默认发送形如 `OpenAI/Python x.y.z`
的 `User-Agent`，而 OpenAI-compatible 第三方网关前面常挂 WAF，会按 UA 直接拦截请求。本项目实测
在同一密钥、同一模型、同一 strict JSON Schema 下：裸 `httpx` 请求返回 200，仅把 UA 换成 SDK 默认
值就返回 `403 Your request was blocked.`，而 SDK 的 `x-stainless-*` 遥测头并不触发拦截。也就是说
403 不是密钥、配额或 Schema 问题，而是客户端标识问题。

因此所有出站客户端（Planner、Auditor、Embedding、Reranker）都改用同一个中性 UA。它不改变任何
请求语义、不隐藏调用方身份，也不是为了绕过鉴权：请求仍带 Bearer 令牌，仍走配置里的 base_url。
把它集中在一个模块，是为了让"为什么不是 SDK 默认值"只需要解释一次，并且新增 Provider 时不会
漏掉——否则下一个 Provider 会在真实网关上重现同一个难以定位的 403。
"""

from __future__ import annotations

# 版本号跟随项目而不是 SDK：它标识调用方是本服务，便于网关侧按来源排障和限流。
OUTBOUND_USER_AGENT = "dataops-troubleshooter/1.0"


def outbound_default_headers() -> dict[str, str]:
    """返回所有出站 HTTP 客户端共用的默认请求头字典。

    返回新字典而不是共享常量，避免调用方在自己的客户端里就地修改后影响其它 Provider；只覆盖
    `User-Agent`，其余头部（Authorization、Content-Type、SDK 遥测头）仍由各客户端按需自行决定。
    """

    # 刻意不注入 Authorization：凭据属于各 Provider 配置，混进公共头会让密钥跨 Provider 泄漏。
    return {"User-Agent": OUTBOUND_USER_AGENT}
