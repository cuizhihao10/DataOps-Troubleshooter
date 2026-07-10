"""验证可替换 Embedding Provider、确定性向量和知识 Bundle 嵌入边界。

测试不依赖网络或模型凭据，直接检查默认 feature-hash Provider 的稳定性、维度、L2 归一化、
Provider 工厂失败语义，以及批量嵌入后每个节点都携带可审计向量空间元数据。
"""

from math import sqrt
from pathlib import Path

import pytest

from app.retrieval.embeddings import (
    DETERMINISTIC_HASH_PROVIDER_ID,
    DeterministicHashEmbeddingProvider,
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
