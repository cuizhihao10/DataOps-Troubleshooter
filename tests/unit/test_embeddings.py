"""验证可替换 Embedding Provider、确定性向量、远程模型契约与知识 Bundle 嵌入边界。

离线部分不依赖网络或凭据，直接检查默认 feature-hash Provider 的稳定性、维度、L2 归一化与工厂
失败语义；远程部分用注入的 AsyncOpenAI 替身锁定 bge-m3 分支的乱序回填、维度校验和异常收敛，
使真实凭据缺失的环境也能验证"要么整批拿到同一空间向量、要么整体失败"这一原子契约。
"""

from math import sqrt
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from openai import APIConnectionError
from pydantic import SecretStr

from app.retrieval.embeddings import (
    BGE_M3_DIMENSIONS,
    BGE_M3_PROVIDER_ID,
    DETERMINISTIC_HASH_PROVIDER_ID,
    DeterministicHashEmbeddingProvider,
    EmbeddingProviderError,
    OpenAICompatibleEmbeddingProvider,
    create_embedding_provider,
    embed_knowledge_bundle,
)
from app.retrieval.seeds import load_knowledge_seed


@pytest.mark.asyncio
async def test_deterministic_provider_returns_stable_normalized_vectors() -> None:
    """验证相同文本跨调用得到相同、固定维度、非零且 L2 归一化的向量。

    同批加入不同文本可同时确认输入顺序与内容敏感性；范数接近一证明 cosine 查询不会被文本长度
    的向量模长直接支配。该测试是替换 Provider 时仍必须满足的最小数学契约。
    """

    provider = DeterministicHashEmbeddingProvider(dimensions=64)
    first = await provider.embed_texts(["FlashSync duplicate key", "LTS scheduler"])
    second = await provider.embed_texts(["FlashSync duplicate key"])

    assert provider.provider_id == DETERMINISTIC_HASH_PROVIDER_ID
    assert first[0] == second[0]
    assert first[0] != first[1]
    assert len(first[0]) == 64
    assert sqrt(sum(value * value for value in first[0])) == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_provider_rejects_blank_text_and_factory_rejects_unknown_id() -> None:
    """验证无特征文本和未注册 Provider ID 都显式失败，不产生默认零向量或静默回退。

    零向量会让 cosine distance 失去意义，静默回退则会把部署声明与实际 embedding 空间分离；
    两种情况都必须在入库或查询前以清晰 ValueError 暴露。
    """

    provider = DeterministicHashEmbeddingProvider(dimensions=32)
    with pytest.raises(ValueError, match="must not be blank"):
        await provider.embed_texts(["   "])

    with pytest.raises(ValueError, match="unsupported embedding provider"):
        create_embedding_provider("unknown-provider:v1", dimensions=32)


@pytest.mark.asyncio
async def test_embedding_bundle_adds_provider_metadata_without_mutating_seed() -> None:
    """验证批量嵌入返回新 Bundle，并为全部节点补齐向量、Provider ID 与真实维度。

    原始 JSON 继续保持 embedding 为空，便于人工审阅且不把某个 Provider 输出提交为静态事实；
    新 Bundle 才进入事务 upsert。逐节点断言保护批量返回数量或维度错位不会静默写库。
    """

    original = load_knowledge_seed(Path("data/knowledge/cross_chain_graph.json"))
    provider = DeterministicHashEmbeddingProvider(dimensions=48)
    embedded = await embed_knowledge_bundle(original, provider)

    assert all(node.embedding is None for node in original.nodes)
    assert all(node.embedding is not None for node in embedded.nodes)
    assert all(node.embedding_provider == provider.provider_id for node in embedded.nodes)
    assert all(node.embedding_dimensions == 48 for node in embedded.nodes)
    assert all(len(node.embedding or []) == 48 for node in embedded.nodes)


_REMOTE_TEST_DIMENSIONS = 8


def _axis_vector(position: int) -> list[float]:
    """构造在指定维度上取一的八维单位向量，作为远程响应里可逐项比较的合法向量。

    单位向量彼此不同且非零，因此"顺序回填是否正确"可以直接用向量本身断言；固定小维度让期望值
    能写在测试里，而不必在断言阶段重新执行一遍被测的重排逻辑。
    """

    vector = [0.0] * _REMOTE_TEST_DIMENSIONS
    vector[position] = 1.0
    return vector


def _fake_client(responder: Any, calls: list[dict[str, Any]]) -> Any:
    """构造只实现 `embeddings.create` 的 AsyncOpenAI 替身，记录请求并返回注入的响应。

    真实 SDK 客户端构造时就要求凭据并准备连接池，注入替身让远程分支的契约校验可以在无网络、无
    key 的环境中完整执行；`calls` 由调用方持有，使断言能检查发出的 model 与实际批次切分。
    """

    async def create(*, model: str, input: Any, encoding_format: str) -> Any:
        """记录本次调用的模型、批次内容与编码格式，再把批次交给注入的响应构造器。

        关键字名必须与 SDK 完全一致，因此被测实现改变调用方式时测试会以 TypeError 直接失败——
        这正是需要捕获的契约漂移；返回值形态由各测试自行决定，本函数不做任何校验。
        """

        calls.append({"model": model, "input": list(input), "encoding_format": encoding_format})
        return responder(list(input))

    return SimpleNamespace(embeddings=SimpleNamespace(create=create))


def _remote_provider(
    responder: Any,
    calls: list[dict[str, Any]],
    *,
    batch_size: int = 32,
) -> OpenAICompatibleEmbeddingProvider:
    """用替身客户端构造远程 Provider，并固定八维以便测试直接书写期望向量。

    provider_id 保持真实的 `bge-m3:v1`，因为仓储按 Provider ID 与维度联合过滤，远程分支必须写出
    这个空间标识才不会与离线基线混算；注入客户端让 `_owns_client` 为 False，测试不会关闭它。
    """

    return OpenAICompatibleEmbeddingProvider(
        api_key=SecretStr("unit-test-key"),
        base_url="https://embed.invalid/v1",
        model="BAAI/bge-m3",
        dimensions=_REMOTE_TEST_DIMENSIONS,
        batch_size=batch_size,
        client=_fake_client(responder, calls),
    )


@pytest.mark.asyncio
async def test_remote_response_is_restored_to_input_order_with_declared_model() -> None:
    """验证乱序返回的向量按 `index` 回填到输入位置，且请求携带配置的模型与浮点编码。

    兼容端点允许乱序返回，"按响应顺序读取"不会报错，只会让每个节点静默拿到别人的向量——那是最难
    发现的检索缺陷。响应刻意把 index=1 放在首位，只有按 index 重排的实现才能通过断言。
    """

    calls: list[dict[str, Any]] = []

    def responder(batch: list[str]) -> Any:
        """返回一个刻意乱序但索引连续、维度合法的最小 `/embeddings` 响应。

        乱序是本测试的核心条件；返回值不依赖入参内容，因为这里验证的是顺序语义而不是模型质量。
        """

        return SimpleNamespace(
            data=[
                SimpleNamespace(index=1, embedding=_axis_vector(1)),
                SimpleNamespace(index=0, embedding=_axis_vector(0)),
            ]
        )

    provider = _remote_provider(responder, calls)
    vectors = await provider.embed_texts(["任务卡住", "duplicate key"])

    assert vectors == [_axis_vector(0), _axis_vector(1)]
    assert provider.provider_id == BGE_M3_PROVIDER_ID
    assert provider.dimensions == _REMOTE_TEST_DIMENSIONS
    assert calls == [
        {
            "model": "BAAI/bge-m3",
            "input": ["任务卡住", "duplicate key"],
            "encoding_format": "float",
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "data",
    [
        [SimpleNamespace(index=0, embedding=_axis_vector(0))],
        [
            SimpleNamespace(index=0, embedding=_axis_vector(0)),
            SimpleNamespace(index=2, embedding=_axis_vector(1)),
        ],
        [
            SimpleNamespace(index=0, embedding=_axis_vector(0)),
            SimpleNamespace(index=0, embedding=_axis_vector(1)),
        ],
        [
            SimpleNamespace(index=0, embedding=_axis_vector(0)),
            SimpleNamespace(index=1, embedding=[1.0, 0.0, 0.0, 0.0]),
        ],
        [
            SimpleNamespace(index=0, embedding=_axis_vector(0)),
            SimpleNamespace(index=1, embedding=[float("inf")] + [0.0] * 7),
        ],
        [
            SimpleNamespace(index=0, embedding=_axis_vector(0)),
            SimpleNamespace(index=1, embedding=[0.0] * 8),
        ],
    ],
    ids=[
        "short-batch",
        "non-contiguous-index",
        "duplicate-index",
        "wrong-dimensions",
        "non-finite-value",
        "all-zero-vector",
    ],
)
async def test_remote_contract_drift_fails_the_whole_batch(data: list[Any]) -> None:
    """验证条数不符、索引越界或重复、维度错误、非有限值和全零向量都让整批失败。

    这些响应都会让节点与向量错配或写入无法比较的记录，而部分成功比整体失败危险得多：数据库会同时
    存在两个语义空间且无从察觉。逐项参数化保证未来放宽任何一项校验都立刻被测试发现。
    """

    def responder(batch: list[str]) -> Any:
        """返回参数化的畸形响应，模拟兼容端点在模型或网关升级后的契约漂移。

        统一走成功路径返回对象，因为这里验证的是响应体结构校验，而不是 SDK 异常的映射分支；
        入参批次刻意不参与构造，使同一份畸形响应能覆盖任意输入。
        """

        return SimpleNamespace(data=data)

    with pytest.raises(EmbeddingProviderError):
        await _remote_provider(responder, []).embed_texts(["a", "b"])


@pytest.mark.asyncio
async def test_transport_failure_maps_to_domain_error_without_leaking_input() -> None:
    """验证 SDK 连接异常收敛为 `EmbeddingProviderError`，且消息只含稳定分类。

    入库与检索都据此决定"整批放弃"而不是逐条重试，所以异常类型必须唯一；消息不含查询文本、
    响应正文或凭据，失败日志才能安全地进入作品集演示环境。
    """

    def responder(batch: list[str]) -> Any:
        """抛出 openai 的连接异常，复现端点不可达时 SDK 实际抛出的类型。

        必须使用 SDK 异常而不是裸 OSError，因为被测实现按 SDK 异常层次分类，替换类型会掩盖漂移；
        request 参数是该异常的必填字段，指向真实端点路径以保持调用栈形态一致。
        """

        raise APIConnectionError(
            request=httpx.Request("POST", "https://embed.invalid/v1/embeddings")
        )

    with pytest.raises(EmbeddingProviderError, match="unreachable"):
        await _remote_provider(responder, []).embed_texts(["任务卡住"])


@pytest.mark.asyncio
async def test_batching_splits_requests_and_preserves_global_order() -> None:
    """验证超过 `batch_size` 的输入被切成多次请求，且拼接结果仍与原始输入顺序一致。

    批量是远程 Provider 的成本与超时控制手段，但跨批次拼接是顺序错位的第二个高风险点；用五条文本
    配二条批量可同时覆盖满批与残批，断言每批实际内容则防止实现重复发送或漏发某一段。
    """

    calls: list[dict[str, Any]] = []

    def responder(batch: list[str]) -> Any:
        """按当前批次长度返回索引连续的向量，向量位置编码该批次内的序号。

        响应只依赖批次长度而不依赖全局位置，因此若实现跨批次错拼，最终向量序列会立刻与期望不同；
        残批返回一条结果，用于覆盖最后一批不满 batch_size 的路径。
        """

        return SimpleNamespace(
            data=[
                SimpleNamespace(index=index, embedding=_axis_vector(index))
                for index in range(len(batch))
            ]
        )

    provider = _remote_provider(responder, calls, batch_size=2)
    vectors = await provider.embed_texts(["a", "b", "c", "d", "e"])

    assert [call["input"] for call in calls] == [["a", "b"], ["c", "d"], ["e"]]
    assert vectors == [
        _axis_vector(0),
        _axis_vector(1),
        _axis_vector(0),
        _axis_vector(1),
        _axis_vector(0),
    ]


@pytest.mark.asyncio
async def test_empty_batch_skips_the_request_and_blank_text_fails_before_billing() -> None:
    """验证空列表直接返回空列表不发请求，空白文本在任何远程调用前显式失败。

    空集合是种子管道的合法输入（某个可选分组没有节点），为它付一次远程调用没有意义；空白文本相反，
    它表示调用方组装检索文本时出错，静默生成一个向量会让知识库里出现无意义的空间坐标。
    """

    def responder(batch: list[str]) -> Any:
        """在本测试中不应被调用，一旦触发即说明实现为无效输入发出了多余的收费请求。

        直接抛 AssertionError 而不是返回响应，可以让失败点精确落在"不该发请求"这一断言上。
        """

        raise AssertionError("embed_texts must not issue a request for empty or blank input")

    provider = _remote_provider(responder, [])
    assert await provider.embed_texts([]) == []
    with pytest.raises(ValueError, match="must not be blank"):
        await provider.embed_texts(["任务卡住", "   "])


@pytest.mark.asyncio
async def test_factory_remote_branch_demands_credentials_and_real_dimensions() -> None:
    """验证远程分支缺 key 或缺 base_url 立即失败，成功时写出 bge-m3 的空间标识与维度。

    凭据缺失必须在启动审计阶段暴露，而不是等到第一次检索返回空结果；断言维度等于 `BGE_M3_DIMENSIONS`
    则锁定"配置维度与真实模型输出一致"，避免 1024 维模型被配成 128 维后写入库时才失败。
    """

    with pytest.raises(ValueError, match="requires an API key"):
        create_embedding_provider(BGE_M3_PROVIDER_ID, dimensions=BGE_M3_DIMENSIONS)
    with pytest.raises(ValueError, match="requires a base URL"):
        create_embedding_provider(
            BGE_M3_PROVIDER_ID,
            dimensions=BGE_M3_DIMENSIONS,
            api_key=SecretStr("unit-test-key"),
        )

    provider = create_embedding_provider(
        BGE_M3_PROVIDER_ID,
        dimensions=BGE_M3_DIMENSIONS,
        api_key=SecretStr("unit-test-key"),
        base_url="https://embed.invalid/v1",
    )

    assert isinstance(provider, OpenAICompatibleEmbeddingProvider)
    assert provider.provider_id == BGE_M3_PROVIDER_ID
    assert provider.dimensions == BGE_M3_DIMENSIONS
    # 工厂自建了真实 httpx 连接池；测试自己关闭它，避免事件循环结束后留下未关闭的传输层告警。
    await provider.aclose()
