"""可替换 Reranker Provider 契约与 cross-encoder 重排实现。

双塔 embedding 为了可索引必须独立编码查询和文档，因此无法建模两者的交互；cross-encoder 把
查询与候选拼在一起联合打分，在小候选集上显著更准。本模块因此只服务"先召回、再重排"的第二
阶段：它不检索、不扩图、不产生结论，只把有界候选列表映射为对齐输入顺序的相关性分数，从而
让 GraphRAG 与文档 RAG 共用同一个重排边界，且重排失败永远只降级为不重排而不使检索失败。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

import httpx
from pydantic import SecretStr

from app.core.http_identity import outbound_default_headers
from app.observability.tracing import TraceSpanKind, trace_span

BGE_RERANKER_V2_M3_PROVIDER_ID = "bge-reranker-v2-m3:v1"
DISABLED_RERANKER_PROVIDER_ID = "disabled"
MAX_RERANK_DOCUMENTS = 64
MAX_RERANK_DOCUMENT_CHARS = 8000
MAX_RERANK_QUERY_CHARS = 4000


class RerankerError(RuntimeError):
    """把重排服务的传输、状态码和契约漂移失败收敛为单一领域异常。

    检索服务捕获该类型后按"不重排"降级继续返回一阶段结果，因此重排是可选增强而不是可用性
    依赖。异常消息只保留稳定分类与 HTTP 状态码，不包含 API key、完整响应体、用户查询或候选
    文档正文，使失败可以安全写入 trace 与作品集演示日志。
    """


class RerankerProvider(Protocol):
    """声明检索第二阶段依赖的最小异步重排接口。

    实现必须公开稳定版本 ID 与模型名供 trace 和评测溯源，并返回与输入 documents 顺序一一对应
    的分数列表。调用方只依赖该协议，因此换成本地 cross-encoder、其他厂商或测试替身时，检索
    服务、证据预算与文档仓储都无需修改。
    """

    @property
    def provider_id(self) -> str:
        """返回能够区分模型与实现版本的稳定标识，供检索结果与 trace 溯源使用。

        重排分数会参与最终排序，因此评测必须能区分"哪个重排器产生了这个名次"；更换模型或
        分数归一化方式时必须提升该 ID，否则历史评测结论无法复现。
        """

        ...

    @property
    def model(self) -> str:
        """返回底层模型名称，用于成本核算、trace 属性和评测报告中的口径说明。

        与 provider_id 分开保留是因为同一实现可以配置不同模型；报告需要同时说明"用哪套代码"
        和"用哪个模型"才能让第三方复现，仅有其中一项都不够。
        """

        ...

    async def rerank(self, query: str, documents: Sequence[str]) -> list[float]:
        """为每个候选文档返回一个 `[0, 1]` 相关性分数，顺序与输入严格对齐。

        空候选列表合法返回空列表；任何失败都必须抛出 `RerankerError` 而不是返回部分分数，
        因为长度不齐的结果会让调用方把分数错配到别的候选上，产生看似合理却完全错误的排序。
        """

        ...


class HttpCrossEncoderReranker:
    """通过 Jina/Cohere 风格 `/rerank` HTTP 端点调用托管 cross-encoder 模型。

    默认面向硅基流动托管的 `BAAI/bge-reranker-v2-m3`，它与 `bge-m3` 同源且原生支持中英混排的
    运维语料。该端点不属于 OpenAI Chat/Embeddings 规范，因此这里直接使用 httpx 而不是 OpenAI
    SDK；请求体、候选数量和单条长度都在客户端截断，避免一次把整个知识库发给收费接口。
    """

    def __init__(
        self,
        *,
        api_key: SecretStr,
        base_url: str,
        model: str,
        provider_id: str = BGE_RERANKER_V2_M3_PROVIDER_ID,
        timeout_seconds: float = 20,
        max_documents: int = MAX_RERANK_DOCUMENTS,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        """配置凭据、端点、模型与候选上限，并可注入 httpx 客户端以便测试断言真实请求体。

        SecretStr 只在构造 Authorization 头时解包一次并保存在私有客户端里，实例不保留明文副本。
        非法空模型、空 URL、非正超时或越界候选上限在任何网络请求前显式失败，保证配置错误在启动
        审计阶段而不是首次检索时才暴露。
        """

        if not base_url.strip():
            raise ValueError("reranker base_url must not be empty")
        if not model.strip():
            raise ValueError("reranker model must not be empty")
        if timeout_seconds <= 0:
            raise ValueError("reranker timeout must be positive")
        if not 1 <= max_documents <= MAX_RERANK_DOCUMENTS:
            raise ValueError(f"reranker max_documents must be between 1 and {MAX_RERANK_DOCUMENTS}")
        self._provider_id = provider_id
        self._model = model
        self._max_documents = max_documents
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout_seconds,
            headers={
                "Authorization": f"Bearer {api_key.get_secret_value()}",
                "Content-Type": "application/json",
                # rerank 没有官方 SDK，这里手写请求头，但出站身份必须与其它 Provider 完全一致。
                **outbound_default_headers(),
            },
        )

    @property
    def provider_id(self) -> str:
        """返回带版本的重排 Provider ID，使检索结果和评测能溯源到具体实现与模型代次。

        分数归一化规则属于该 ID 语义的一部分；若未来改为使用原始 logit 而非 sigmoid 分数，
        必须提升版本，否则融合权重的含义会在无人察觉的情况下改变。
        """

        return self._provider_id

    @property
    def model(self) -> str:
        """返回配置的远程模型名，供 trace 属性、成本核算与评测口径说明使用。

        该值直接进入检索结果契约，因此不做任何规范化或大小写转换，保持与供应商控制台一致，
        便于对账实际调用量。
        """

        return self._model

    async def rerank(self, query: str, documents: Sequence[str]) -> list[float]:
        """把有界候选发送给 cross-encoder，并返回与输入顺序严格对齐的相关性分数。

        查询和文档在客户端先截断到契约上限，候选数超限直接拒绝而不是静默丢弃尾部，避免调用方
        以为全部候选都被评估过。响应按 `index` 回填到原位置：该端点按分数降序返回，若直接按
        响应顺序读取会把最高分错配给第一个候选，这是此类 API 最容易出现且最难发现的缺陷。
        """

        if not query.strip():
            raise ValueError("rerank query must not be blank")
        if not documents:
            return []
        if len(documents) > self._max_documents:
            raise ValueError(f"rerank accepts at most {self._max_documents} documents")

        # 截断在客户端完成而不是依赖服务端策略，这样"单次调用最多送出多少字符"是本地可证明的成本
        # 上界；top_n 显式等于候选数，避免端点默认只返回前若干条后调用方拿到不完整的分数集合。
        payload = {
            "model": self._model,
            "query": query[:MAX_RERANK_QUERY_CHARS],
            "documents": [document[:MAX_RERANK_DOCUMENT_CHARS] for document in documents],
            "top_n": len(documents),
            "return_documents": False,
        }
        try:
            # 精排是可选增强，但它的耗时直接进入用户等待；单列 span 才能判断是否值得继续启用。
            with trace_span(
                TraceSpanKind.MODEL_CALL,
                "reranker.rerank",
                model=self._model,
                document_count=len(documents),
            ):
                response = await self._client.post("/rerank", json=payload)
                response.raise_for_status()
                body = response.json()
        except httpx.TimeoutException as exc:
            raise RerankerError("rerank request timed out") from exc
        except httpx.HTTPStatusError as exc:
            raise RerankerError(
                f"rerank endpoint returned HTTP {exc.response.status_code}"
            ) from exc
        except httpx.HTTPError as exc:
            raise RerankerError("rerank endpoint is unreachable") from exc
        except ValueError as exc:
            raise RerankerError("rerank endpoint returned a non-JSON body") from exc

        return _scores_in_input_order(body, expected=len(documents))

    async def aclose(self) -> None:
        """关闭由 Provider 自行创建的 httpx 连接池，注入客户端交由调用方管理。

        FastAPI lifespan 退出时应调用一次；连接池持有 Authorization 头，及时关闭可以缩短凭据
        驻留在进程内的时间窗口，也避免测试之间复用已失效的传输层。
        """

        if self._owns_client:
            await self._client.aclose()


def _scores_in_input_order(body: object, *, expected: int) -> list[float]:
    """校验 `/rerank` 响应结构并把降序结果按 index 回填成输入顺序的分数列表。

    缺失 results、条数不符、index 越界或重复都视为契约漂移并抛出 `RerankerError`，因为这些情况
    都会导致分数与候选错配。分数裁剪到 `[0, 1]`：bge-reranker 经 sigmoid 后本就落在该区间，
    裁剪只用于吸收浮点边界误差，从而让上层融合公式的取值范围保持可证明。
    """

    if not isinstance(body, dict):
        raise RerankerError("rerank response must be a JSON object")
    results = body.get("results")
    if not isinstance(results, list) or len(results) != expected:
        raise RerankerError("rerank response did not score every submitted document")

    # 预填 None 而不是 0.0：只有"未被赋值"与"分数为零"两种状态可区分，才能在最后检测出既没有
    # 重复索引也没有越界、却仍有候选未被打分的响应，而不是把缺失静默当成不相关。
    scores: list[float | None] = [None] * expected
    for item in results:
        if not isinstance(item, dict):
            raise RerankerError("rerank result entries must be JSON objects")
        index = item.get("index")
        score = item.get("relevance_score")
        if not isinstance(index, int) or not 0 <= index < expected:
            raise RerankerError("rerank result contained an out-of-range index")
        if scores[index] is not None:
            raise RerankerError("rerank result contained a duplicate index")
        if not isinstance(score, int | float):
            raise RerankerError("rerank result relevance_score must be numeric")
        scores[index] = max(0.0, min(1.0, float(score)))

    if any(score is None for score in scores):
        raise RerankerError("rerank response left at least one document unscored")
    return [score for score in scores if score is not None]


def create_reranker(
    provider_id: str,
    *,
    model: str | None = None,
    base_url: str | None = None,
    api_key: SecretStr | None = None,
    timeout_seconds: float = 20,
    max_documents: int = MAX_RERANK_DOCUMENTS,
) -> RerankerProvider | None:
    """根据集中配置创建重排 Provider，`disabled` 明确返回 None 表示只跑一阶段检索。

    返回 None 而不是一个"恒等重排"替身，是为了让检索结果里的 `reranker_model` 字段真实为空，
    从而评测和报告不会把未重排的排序说成重排结果。未知 ID 立即失败而不是静默降级，防止拼错
    provider 名的部署以为重排已经生效。
    """

    if provider_id == DISABLED_RERANKER_PROVIDER_ID:
        return None
    if provider_id == BGE_RERANKER_V2_M3_PROVIDER_ID:
        if api_key is None:
            raise ValueError(f"reranker provider {provider_id} requires an API key")
        if base_url is None:
            raise ValueError(f"reranker provider {provider_id} requires a base URL")
        return HttpCrossEncoderReranker(
            api_key=api_key,
            base_url=base_url,
            model=model or "BAAI/bge-reranker-v2-m3",
            provider_id=provider_id,
            timeout_seconds=timeout_seconds,
            max_documents=max_documents,
        )
    raise ValueError(f"unsupported reranker provider: {provider_id}")
