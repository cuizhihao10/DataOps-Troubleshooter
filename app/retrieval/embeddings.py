"""可替换 Embedding Provider 契约与离线确定性基线实现。

在线检索和知识入库只依赖 `EmbeddingProvider` 协议，不依赖具体模型 SDK。默认实现使用稳定
feature hashing 将中英文词元和字符片段映射为归一化向量，使测试、Docker 演示和离线学习环境
无需外部凭据即可真实执行 pgvector 查询；它是工程基线而非神经语义模型，后续可用相同接口替换。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from hashlib import sha256
from math import isfinite, sqrt
from typing import Protocol

from app.retrieval.models import KnowledgeNode, KnowledgeSeedBundle

DETERMINISTIC_HASH_PROVIDER_ID = "deterministic-hash:v1"
_TOKEN_PATTERN = re.compile(r"[a-z0-9_]+|[\u3400-\u4dbf\u4e00-\u9fff]+")


class EmbeddingProvider(Protocol):
    """定义所有本地或远程 embedding 实现必须满足的最小异步边界。

    Provider 必须公开稳定版本 ID 和固定维度，并按输入顺序批量返回向量。调用方只依赖该协议，
    因此未来接入 OpenAI-compatible、本地模型或测试替身时无需修改 PostgreSQL 仓储与混合评分。
    """

    @property
    def provider_id(self) -> str:
        """返回能够区分算法、模型及版本的稳定标识，供入库和查询过滤使用。

        实现升级分词、模型权重或归一化规则时必须更换该 ID；调用方会据此排除旧向量，避免两个
        数学空间即使维度相同也被 pgvector 直接比较。
        """

        ...

    @property
    def dimensions(self) -> int:
        """返回 Provider 固定输出维度，供领域校验、持久化和查询兼容过滤使用。

        同一 Provider 实例的所有向量必须保持该长度；仓储同时匹配 ID 和维度，防止模型配置改变
        后数据库把不同长度或不同空间的记录放入一次 cosine 排序。
        """

        ...

    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        """按输入顺序生成一批固定维度、非零且只含有限数值的向量。

        批次中任一输入或 Provider 调用失败时应抛出异常，不返回无法与节点一一对应的部分结果；
        该原子语义让知识写入可以在一个数据库事务中决定提交或回滚。
        """

        ...


class DeterministicHashEmbeddingProvider:
    """使用 SHA-256 feature hashing 生成可重放、无外部依赖的归一化向量。

    实现提取英文词元、英文字符三元组以及中文单字/二元组/三元组，再把每个特征稳定散列到固定
    维度并使用有符号累加降低碰撞偏差，最后执行 L2 归一化。它能验证 Provider 替换、向量存储、
    cosine 查询与融合链路，但不宣称具备模型级同义词理解能力。
    """

    def __init__(self, *, dimensions: int = 128) -> None:
        """配置固定向量维度，并拒绝不适合 pgvector 演示的过小或过大空间。

        八维下限避免极端碰撞导致所有节点近似相同，4096 上限与领域 Schema 一致并限制存储成本；
        构造过程不加载模型或执行 I/O，因此 Provider 可安全作为进程级依赖复用。
        """

        if not 8 <= dimensions <= 4096:
            raise ValueError("embedding dimensions must be between 8 and 4096")
        self._dimensions = dimensions

    @property
    def provider_id(self) -> str:
        """返回带版本的确定性 Provider ID，使数据库能区分未来算法升级后的向量空间。

        维度单独记录在 `embedding_dimensions`，因此 ID 只描述特征算法版本；修改分词、散列或归一化
        规则时必须提升版本，避免旧向量与新查询在同名空间中混算。
        """

        return DETERMINISTIC_HASH_PROVIDER_ID

    @property
    def dimensions(self) -> int:
        """返回当前实例固定输出维度，所有批次和文本都严格遵守该长度。

        仓储使用该值过滤兼容记录，领域模型也会验证实际列表长度；Provider 运行期间不得改变维度，
        否则相同进程中的向量将无法比较。
        """

        return self._dimensions

    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        """为非空文本批次生成与输入顺序一致的 L2 归一化 feature-hash 向量。

        空批次合法返回空列表，便于种子管道处理可选集合；任一文本为空、无法提取特征或产生非法
        数值时整个调用失败，避免部分节点写入新空间、部分节点保留旧空间的非原子状态。
        """

        vectors: list[list[float]] = []
        for text in texts:
            if not text.strip():
                raise ValueError("embedding text must not be blank")

            # 特征散列使用 SHA-256 而非 Python hash，保证跨进程、平台和重启得到相同索引与符号。
            vector = [0.0] * self._dimensions
            features = _extract_features(text)
            if not features:
                raise ValueError("embedding text produced no supported features")
            for feature in features:
                digest = sha256(feature.encode("utf-8")).digest()
                index = int.from_bytes(digest[:8], "big") % self._dimensions
                sign = 1.0 if digest[8] & 1 == 0 else -1.0
                vector[index] += sign

            # cosine distance 只关注方向；L2 归一化消除文本长短对向量模长的直接影响。
            norm = sqrt(sum(value * value for value in vector))
            if norm == 0:
                raise ValueError("embedding feature collisions produced a zero vector")
            normalized = [value / norm for value in vector]
            if not all(isfinite(value) for value in normalized):
                raise ValueError("embedding provider produced non-finite values")
            vectors.append(normalized)
        return vectors


def create_embedding_provider(provider_id: str, *, dimensions: int) -> EmbeddingProvider:
    """根据集中配置创建一个实现统一协议的 Embedding Provider。

    工厂目前只批准离线确定性版本，未知 ID 立即失败而不是静默回退，防止部署者以为正在使用外部
    语义模型。未来 Provider 只需新增实现和显式注册，不应让仓储或服务判断供应商名称。
    """

    if provider_id == DETERMINISTIC_HASH_PROVIDER_ID:
        return DeterministicHashEmbeddingProvider(dimensions=dimensions)
    raise ValueError(f"unsupported embedding provider: {provider_id}")


async def embed_knowledge_bundle(
    bundle: KnowledgeSeedBundle,
    provider: EmbeddingProvider,
) -> KnowledgeSeedBundle:
    """批量嵌入知识节点并返回带 Provider 溯源信息的新 Bundle，不修改输入对象。

    每个节点的名称、别名和正文组成检索文本；批量调用保持未来远程 Provider 的吞吐效率。返回数量、
    维度和有限值在构造 KnowledgeNode 时再次验证，任一失败都会阻止整个 Bundle 入库，边集合保持
    原样，从而让节点向量更新和图结构写入可在同一数据库事务提交。
    """

    texts = [_knowledge_node_text(node) for node in bundle.nodes]
    vectors = await provider.embed_texts(texts)
    if len(vectors) != len(bundle.nodes):
        raise ValueError("embedding provider returned a different number of vectors")

    embedded_nodes: list[KnowledgeNode] = []
    for node, vector in zip(bundle.nodes, vectors, strict=True):
        # 重新经过 model_validate，而非无校验 model_copy，确保第三方 Provider 也受领域约束。
        payload = node.model_dump()
        payload.update(
            embedding=vector,
            embedding_provider=provider.provider_id,
            embedding_dimensions=provider.dimensions,
        )
        embedded_nodes.append(KnowledgeNode.model_validate(payload))

    return KnowledgeSeedBundle.model_validate(
        {
            **bundle.model_dump(),
            "nodes": [node.model_dump() for node in embedded_nodes],
        }
    )


def _knowledge_node_text(node: KnowledgeNode) -> str:
    """按稳定字段顺序组合节点名称、别名和正文，作为 embedding 的可审计输入。

    source_span 通常与正文重复，故不再次加入以免重复内容获得不成比例权重；别名对组件缩写和英文
    故障术语很重要。字段使用换行分隔，便于未来模型 Provider 保留语义段落边界。
    """

    return "\n".join((node.name, " ".join(node.aliases), node.content))


def _extract_features(text: str) -> list[str]:
    """从 NFKC 规范化文本中提取带类型前缀的中英文词元和字符 n-gram。

    英文保留完整词并加入三元组以容忍词形片段，中文缺少空格分词边界，因此加入单字、二元组和
    三元组。类型前缀降低不同特征族碰撞，重复特征保留为词频信号并在最终 L2 归一化中受控。
    """

    normalized = unicodedata.normalize("NFKC", text).casefold()
    features: list[str] = []
    for token in _TOKEN_PATTERN.findall(normalized):
        if token.isascii():
            features.append(f"word:{token}")
            if len(token) >= 3:
                features.extend(
                    f"latin3:{token[index : index + 3]}" for index in range(len(token) - 2)
                )
            continue

        # 中文节点名和故障短语通常很短，单字与 2–3 gram 组合兼顾召回和局部语序信息。
        features.extend(f"cjk1:{character}" for character in token)
        for size in (2, 3):
            if len(token) >= size:
                features.extend(
                    f"cjk{size}:{token[index : index + size]}"
                    for index in range(len(token) - size + 1)
                )
    return features
