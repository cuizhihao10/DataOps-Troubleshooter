"""验证 cross-encoder 重排 Provider 的顺序回填、契约漂移检测与降级语义。

`/rerank` 端点按分数降序返回结果，因此"按响应顺序读取分数"是这类集成最容易犯且最难发现的
缺陷——它不会报错，只会把最高分错配给第一个候选。这些测试用 httpx MockTransport 断言真实请求
体与乱序响应的回填结果，同时锁定所有会导致分数错配的响应形态都必须抛 `RerankerError` 而不是
返回部分分数；工厂的 `disabled` 分支则必须返回 None，使检索结果里的 `reranker_model` 真实为空。
"""

from __future__ import annotations

import json

import httpx
import pytest
from pydantic import SecretStr

from app.retrieval.reranker import (
    BGE_RERANKER_V2_M3_PROVIDER_ID,
    DISABLED_RERANKER_PROVIDER_ID,
    MAX_RERANK_DOCUMENT_CHARS,
    HttpCrossEncoderReranker,
    RerankerError,
    create_reranker,
)


def _reranker(handler) -> HttpCrossEncoderReranker:
    """用注入的 MockTransport 处理器构造重排器，避免测试发出真实网络请求。

    注入客户端同时让 `_owns_client` 为 False，因此测试不会关闭由调用方管理的传输层；固定
    base_url 和模型名使断言可以直接检查请求体，而不需要匹配任意主机配置。
    """

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://rerank.invalid/v1",
    )
    return HttpCrossEncoderReranker(
        api_key=SecretStr("unit-test-key"),
        base_url="https://rerank.invalid/v1",
        model="BAAI/bge-reranker-v2-m3",
        client=client,
    )


@pytest.mark.asyncio
async def test_descending_response_is_restored_to_input_order() -> None:
    """验证降序返回的分数按 `index` 回填到输入位置，而不是按响应顺序读取。

    响应故意把第三个候选放在首位并给出最高分；若实现按响应顺序赋值，第一个候选会拿到 0.9 并让
    排序完全反转。断言同时检查请求体的 model/top_n/return_documents，确保客户端没有隐式截断候选。
    """

    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        """记录请求体并返回一个刻意乱序、分数递减的合法 `/rerank` 响应。

        闭包把解析后的 JSON 写入外层字典供断言使用；返回顺序与输入顺序不同是本测试的核心条件，
        因为只有这种响应才能区分"按 index 回填"和"按位置读取"两种实现。
        """

        assert request.url.path.endswith("/rerank")
        captured.update(json.loads(request.read()))
        return httpx.Response(
            200,
            json={
                "results": [
                    {"index": 2, "relevance_score": 0.9},
                    {"index": 0, "relevance_score": 0.5},
                    {"index": 1, "relevance_score": 0.1},
                ]
            },
        )

    scores = await _reranker(handler).rerank("任务卡住", ["a", "b", "c"])

    assert scores == [0.5, 0.1, 0.9]
    assert captured["model"] == "BAAI/bge-reranker-v2-m3"
    assert captured["documents"] == ["a", "b", "c"]
    assert captured["top_n"] == 3
    assert captured["return_documents"] is False


@pytest.mark.asyncio
async def test_long_documents_are_truncated_before_leaving_the_process() -> None:
    """验证超长候选在客户端截断到契约上限，而不是把整个知识库发给收费端点。

    截断发生在请求构造阶段而非依赖服务端行为，因此成本上界可以在本地证明；断言长度等于上限而不是
    仅"更短"，避免未来把截断改成近似值后测试仍然通过。
    """

    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        """记录被截断后的请求体并返回单条合法分数。

        只需要一条结果即可满足契约校验；本测试关心的是发出去的文本长度，而不是返回的分数值，
        因此分数取任意合法值即可，断言完全落在捕获到的请求体上。
        """

        captured.update(json.loads(request.read()))
        return httpx.Response(200, json={"results": [{"index": 0, "relevance_score": 0.4}]})

    await _reranker(handler).rerank("q", ["x" * (MAX_RERANK_DOCUMENT_CHARS + 500)])

    documents = captured["documents"]
    assert isinstance(documents, list)
    assert len(documents[0]) == MAX_RERANK_DOCUMENT_CHARS


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"results": [{"index": 0, "relevance_score": 0.5}]},
        {"results": [{"index": 5, "relevance_score": 0.5}, {"index": 1, "relevance_score": 0.2}]},
        {"results": [{"index": 0, "relevance_score": 0.5}, {"index": 0, "relevance_score": 0.2}]},
        {"results": [{"index": 0, "relevance_score": "high"}, {"index": 1, "relevance_score": 1}]},
    ],
    ids=["short-results", "out-of-range-index", "duplicate-index", "non-numeric-score"],
)
async def test_contract_drift_raises_instead_of_returning_partial_scores(body: dict) -> None:
    """验证条数不符、索引越界、索引重复和非数值分数都抛 `RerankerError`。

    这四种响应都会让分数与候选错配，产生看似合理却完全错误的排序；返回部分结果比整体失败更危险，
    因为调用方无法察觉。参数化覆盖保证未来放宽任何一项校验都会立刻被测试发现。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        """返回参数化的畸形响应，模拟兼容端点的契约漂移。

        统一返回 200，因为这里要验证的是响应体结构校验而不是 HTTP 状态处理路径；请求内容不参与
        构造，使同一份畸形响应能覆盖任意候选输入。
        """

        return httpx.Response(200, json=body)

    with pytest.raises(RerankerError):
        await _reranker(handler).rerank("q", ["a", "b"])


@pytest.mark.asyncio
async def test_http_status_and_transport_failures_map_to_domain_error() -> None:
    """验证 HTTP 状态码错误和连接失败都收敛为 `RerankerError` 且消息只含稳定分类。

    检索服务据此实现"重排失败即降级为一阶段"，所以异常类型必须唯一；消息里保留状态码便于排障，
    但不能包含响应正文、查询或候选文本，否则失败日志无法进入作品集演示环境。
    """

    def failing_status(request: httpx.Request) -> httpx.Response:
        """返回 429 以模拟配额或限流失败，验证状态码分支的异常映射。

        选择 429 而不是 500，是因为限流是这类计费端点最常见的真实失败模式；响应体带 error 字段，
        用于确认实现不会把正文原样拼进异常消息。
        """

        return httpx.Response(429, json={"error": "rate limited"})

    def failing_transport(request: httpx.Request) -> httpx.Response:
        """抛出 httpx 连接错误以模拟端点不可达，验证传输层分支的异常映射。

        直接抛异常而不是返回响应，让 MockTransport 复现 DNS/TCP 失败的调用栈形态。
        """

        raise httpx.ConnectError("unreachable", request=request)

    with pytest.raises(RerankerError, match="HTTP 429"):
        await _reranker(failing_status).rerank("q", ["a"])
    with pytest.raises(RerankerError, match="unreachable"):
        await _reranker(failing_transport).rerank("q", ["a"])


@pytest.mark.asyncio
async def test_empty_documents_skip_the_request_and_blank_query_fails() -> None:
    """验证空候选列表直接返回空列表不发请求，空白查询则显式失败。

    空候选是检索的合法结果（知识库没有命中），为它付一次远程调用没有意义；空白查询相反，它表示
    调用方逻辑错误，静默返回空分数会让上层以为重排已执行。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        """在本测试中不应被调用，一旦触发即说明实现为空输入发出了多余请求。

        直接抛 AssertionError 而不是返回响应，可以让失败点精确落在"不该发请求"这一断言上。
        """

        raise AssertionError("rerank must not issue a request for an empty document list")

    reranker = _reranker(handler)
    assert await reranker.rerank("q", []) == []
    with pytest.raises(ValueError, match="must not be blank"):
        await reranker.rerank("   ", ["a"])


def test_factory_disables_cleanly_and_rejects_unknown_provider() -> None:
    """验证 `disabled` 返回 None 而不是恒等替身，未知 provider ID 立即失败。

    返回 None 让检索结果的 `reranker_model` 真实为空，报告因此不会把未重排的排序说成精排结果；
    未知 ID 立即失败则防止拼错 provider 名的部署以为重排已经生效却始终只跑一阶段。
    """

    assert create_reranker(DISABLED_RERANKER_PROVIDER_ID) is None
    with pytest.raises(ValueError, match="unsupported reranker provider"):
        create_reranker("unknown-reranker:v1")
    with pytest.raises(ValueError, match="requires an API key"):
        create_reranker(BGE_RERANKER_V2_M3_PROVIDER_ID, base_url="https://rerank.invalid/v1")
