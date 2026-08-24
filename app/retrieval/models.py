"""GraphRAG 节点、关系、种子匹配和路径证据模型。

枚举与 Prompt 契约保持一致，种子 Bundle 会拒绝重复 ID、悬空边和自环。GraphPath 同时
保存完整节点、边、来源和稳定 path_id，使 Planner/Auditor 能逐项引用而非依赖文本摘要。
"""

from __future__ import annotations

from enum import StrEnum
from math import isfinite
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.retrieval.documents import BundledDocumentChunk
from app.retrieval.scoring import (
    RetrievalChannel,
    default_final_score,
    validate_rerank_consistency,
)

GRAPH_RETRIEVAL_CONTRACT_ID = "graphrag-retrieval:v3"
GRAPH_EVIDENCE_BUNDLE_CONTRACT_ID = "graphrag-evidence-bundle:v2"

__all__ = [
    "GRAPH_EVIDENCE_BUNDLE_CONTRACT_ID",
    "GRAPH_RETRIEVAL_CONTRACT_ID",
    "BundledGraphPath",
    "BundledKnowledgeNode",
    "EvidenceBundleBudget",
    "GraphEvidenceBundle",
    "GraphPath",
    "GraphRetrievalResult",
    "HybridScoringWeights",
    "HybridSeedMatch",
    "KnowledgeEdge",
    "KnowledgeNode",
    "KnowledgeNodeType",
    "KnowledgeRelationType",
    "KnowledgeSeedBundle",
    "LexicalSeedMatch",
    "RetrievalChannel",
    "RetrievalMode",
    "ScoredGraphPath",
    "VectorSeedMatch",
]


class KnowledgeNodeType(StrEnum):
    """限定人工知识图允许的八类实体节点。

    类型集合与产品设计和 Prompt 契约一致，覆盖组件、任务、数据集、症状、根因、方案、案例和 SOP；
    字符串枚举保证 JSON/数据库可序列化，并阻止未经评审的新实体类型静默进入检索。
    """

    COMPONENT = "component"
    TASK = "task"
    DATASET = "dataset"
    SYMPTOM = "symptom"
    ROOT_CAUSE = "root_cause"
    SOLUTION = "solution"
    CASE = "case"
    SOP = "sop"


class KnowledgeRelationType(StrEnum):
    """限定 GraphRAG 可以存储与扩展的八类有向关系。

    白名单让递归查询只沿有业务含义的边传播，避免任意文本关系造成不可解释路径。枚举值与数据库
    CheckConstraint 完全一致，修改时必须同步迁移、种子、Prompt 和测试。
    """

    RUNS_ON = "RUNS_ON"
    DEPENDS_ON = "DEPENDS_ON"
    PRODUCES = "PRODUCES"
    CONSUMES = "CONSUMES"
    MANIFESTS_AS = "MANIFESTS_AS"
    CAUSED_BY = "CAUSED_BY"
    RESOLVED_BY = "RESOLVED_BY"
    SIMILAR_TO = "SIMILAR_TO"


class RetrievalMode(StrEnum):
    """定义消融和生产检索允许的三种显式执行模式。

    `vector_only` 只保留向量种子且不扩图，`vector_graph` 隔离图关系增益，`hybrid_graph` 再加入
    全文通道并作为默认生产模式。显式枚举防止评测通过隐藏布尔开关得到无法复现的比较结果。
    """

    VECTOR_ONLY = "vector_only"
    VECTOR_GRAPH = "vector_graph"
    HYBRID_GRAPH = "hybrid_graph"


class HybridScoringWeights(BaseModel):
    """集中声明 GraphRAG 五项可解释评分权重，并强制总和等于一。

    语义、全文、路径、可靠性和案例新鲜度与产品基线一一对应。模型允许运行配置替换默认值，
    但拒绝负权重或总和漂移，从而让不同环境的 `hybrid_score` 始终保持可比较的零到一区间。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    semantic: float = Field(default=0.45, ge=0, le=1)
    lexical: float = Field(default=0.10, ge=0, le=1)
    path: float = Field(default=0.25, ge=0, le=1)
    reliability: float = Field(default=0.10, ge=0, le=1)
    freshness: float = Field(default=0.10, ge=0, le=1)

    @model_validator(mode="after")
    def validate_total_weight(self) -> HybridScoringWeights:
        """验证五项权重之和在浮点容差内等于一，并返回不可变配置对象。

        使用绝对误差容差处理十进制转二进制造成的微小偏差，但不自动归一化错误配置；显式失败
        能让部署者看见评分契约变化，而不是让服务悄悄采用与文档不同的实际权重。
        """

        total = self.semantic + self.lexical + self.path + self.reliability + self.freshness
        if abs(total - 1.0) > 1e-9:
            raise ValueError("hybrid scoring weights must sum to 1.0")
        return self


class KnowledgeNode(BaseModel):
    """表示一个有来源、可靠性和可选向量的知识图实体。

    节点保存名称、正文、别名和精确 source_span，便于全文/语义召回后回溯人工资料；embedding
    允许为空以区分已建存储与尚未生成向量，不能把空值宣称为语义检索结果。
    """

    model_config = ConfigDict(extra="forbid")

    node_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,99}$")
    node_type: KnowledgeNodeType
    name: str = Field(min_length=1, max_length=300)
    content: str = Field(min_length=1, max_length=4000)
    aliases: list[str] = Field(default_factory=list)
    source_id: str = Field(min_length=1, max_length=200)
    source_span: str = Field(min_length=1, max_length=2000)
    reliability: float = Field(default=1, ge=0, le=1)
    embedding: list[float] | None = None
    embedding_provider: str | None = Field(default=None, min_length=1, max_length=100)
    embedding_dimensions: int | None = Field(default=None, ge=8, le=4096)

    @model_validator(mode="after")
    def validate_embedding_metadata(self) -> KnowledgeNode:
        """保证向量、Provider ID 和维度元数据要么同时存在，要么同时为空。

        非空向量还必须长度匹配、只含有限数值且不能为全零，因为 pgvector cosine distance 无法为
        零向量提供有意义的方向相似度。该校验阻止不同 Provider 空间或损坏向量静默进入数据库。
        """

        metadata_present = (
            self.embedding_provider is not None or self.embedding_dimensions is not None
        )
        if self.embedding is None:
            if metadata_present:
                raise ValueError("embedding metadata requires an embedding vector")
            return self

        if self.embedding_provider is None or self.embedding_dimensions is None:
            raise ValueError("embedding vector requires provider and dimensions metadata")
        if len(self.embedding) != self.embedding_dimensions:
            raise ValueError("embedding length must match embedding_dimensions")
        if not all(isfinite(value) for value in self.embedding):
            raise ValueError("embedding values must be finite")
        if not any(value != 0 for value in self.embedding):
            raise ValueError("embedding vector must not be all zeros")
        return self


class KnowledgeEdge(BaseModel):
    """表示两个已知节点之间带来源和权重的有向知识关系。

    edge_id 提供稳定引用，source_span 解释关系依据，weight 参与路径组合评分但不代表事实概率；
    自环、悬空引用和唯一性由 Bundle 校验与数据库约束共同防守。
    """

    model_config = ConfigDict(extra="forbid")

    edge_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,99}$")
    from_node_id: str = Field(min_length=3, max_length=100)
    to_node_id: str = Field(min_length=3, max_length=100)
    relation_type: KnowledgeRelationType
    weight: float = Field(default=1, gt=0, le=1)
    source_id: str = Field(min_length=1, max_length=200)
    source_span: str = Field(min_length=1, max_length=2000)


class KnowledgeSeedBundle(BaseModel):
    """封装一个版本化人工知识种子的节点与边，并执行跨元素图完整性校验。

    单个 Node/Edge 模型只能检查字段，Bundle 进一步拒绝重复 ID、悬空边和自环。通过后才能进入
    数据库 upsert，使迁移后的图不会因坏 JSON 形成无法解释或递归循环的结构。
    """

    model_config = ConfigDict(extra="forbid")

    seed_version: str = Field(pattern=r"^graph-seed:v[0-9]+$")
    source_id: str = Field(min_length=1, max_length=200)
    nodes: list[KnowledgeNode] = Field(min_length=1)
    edges: list[KnowledgeEdge] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_and_linked_graph(self) -> KnowledgeSeedBundle:
        """验证节点/边 ID 唯一，并确保每条边连接两个不同的已声明节点。

        校验顺序先检查 ID 冲突，再建立节点集合检查引用，因而错误信息能准确区分覆盖风险与悬空边。
        任一错误都会在数据库连接前抛出，避免部分种子进入事务后才因外键失败。
        """

        # 重复节点会让后续字典索引静默覆盖，因此必须在构造集合前先比较数量。
        node_ids = [node.node_id for node in self.nodes]
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("knowledge seed contains duplicate node IDs")

        # 边 ID 是 path_id 和消融测试的基础，同样不能依赖数据库 upsert 覆盖重复定义。
        edge_ids = [edge.edge_id for edge in self.edges]
        if len(edge_ids) != len(set(edge_ids)):
            raise ValueError("knowledge seed contains duplicate edge IDs")

        # 只有 Bundle 级视角才能检查跨对象引用和自环，这些错误不属于单条边字段格式问题。
        known_nodes = set(node_ids)
        for edge in self.edges:
            if edge.from_node_id not in known_nodes or edge.to_node_id not in known_nodes:
                raise ValueError(f"edge {edge.edge_id} references an unknown node")
            if edge.from_node_id == edge.to_node_id:
                raise ValueError(f"edge {edge.edge_id} cannot be a self-loop")
        return self


class LexicalSeedMatch(BaseModel):
    """把全文召回的知识节点与非负 lexical score 绑定为种子候选。

    分数来自 PostgreSQL ts_rank 与短标识符 bonus，只用于当前候选排序，不冒充语义相似度；保留
    完整节点使服务层后续可沿 node_id 扩图并向 Planner 提供来源。
    """

    model_config = ConfigDict(extra="forbid")

    node: KnowledgeNode
    lexical_score: float = Field(ge=0)


class VectorSeedMatch(BaseModel):
    """把 pgvector cosine 相似度与对应知识节点绑定为语义种子候选。

    `semantic_score` 已从 cosine distance 转换并裁剪到零到一；匹配对象单独携带 Provider ID 与
    维度。原始向量不会进入检索结果，避免路径重复携带大数组并泄漏派生模型特征。
    """

    model_config = ConfigDict(extra="forbid")

    node: KnowledgeNode
    embedding_provider: str = Field(min_length=1, max_length=100)
    embedding_dimensions: int = Field(ge=8, le=4096)
    semantic_score: float = Field(ge=0, le=1)


class HybridSeedMatch(BaseModel):
    """表示全文与向量候选按节点 ID 合并、并可选经 cross-encoder 重排后的可解释种子评分。

    模型保留每个评分分量、实际命中通道和组合分数，避免服务只返回一个无法复核的排序值。
    当前知识节点没有案例时间字段，因此 freshness 默认为零；后续案例记忆可在同一契约中补值。
    `hybrid_score` 是五项加权的一阶段分数，`rerank_score` 是二阶段 cross-encoder 分数，
    `final_score` 是两者的显式融合结果——三者分开保存，才能在评测里判断名次变化来自哪一阶段。
    """

    model_config = ConfigDict(extra="forbid")

    node: KnowledgeNode
    channels: list[RetrievalChannel] = Field(min_length=1)
    semantic_score: float = Field(default=0, ge=0, le=1)
    lexical_score: float = Field(default=0, ge=0, le=1)
    reliability_score: float = Field(ge=0, le=1)
    freshness_score: float = Field(default=0, ge=0, le=1)
    hybrid_score: float = Field(ge=0, le=1)
    rerank_score: float | None = Field(default=None, ge=0, le=1)
    final_score: float = Field(ge=0, le=1)

    @model_validator(mode="before")
    @classmethod
    def default_final_score(cls, data: object) -> object:
        """未显式给出 `final_score` 时让它等于 `hybrid_score`，表示本次没有跑第二阶段。

        省略即"未重排"是唯一自洽的默认：一阶段结果的最终排序值本来就是 hybrid_score，如果要求
        每个调用点重复写一次相同数字，只会制造两者不一致的机会。显式传入的值不会被覆盖。
        """

        return default_final_score(data)

    @model_validator(mode="after")
    def validate_rerank_consistency(self) -> HybridSeedMatch:
        """禁止在没有 `rerank_score` 的情况下让 `final_score` 偏离 `hybrid_score`。

        这条不变量防止排序值被静默调整却无从溯源：只要最终分与一阶段分不同，就必须同时存在一个
        二阶段分数来解释差异，评测因此能把任何名次变化归因到召回或精排中确定的一个环节。
        """

        validate_rerank_consistency(self.hybrid_score, self.rerank_score, self.final_score)
        return self


class GraphPath(BaseModel):
    """保存一条一至两跳的完整节点、边、深度、来源和稳定引用。

    路径对象是 GraphRAG 可解释性的核心：报告可引用 path_id，Auditor 可逐边核对 source_span，
    删边消融可验证结果依赖真实关系。分数为边权乘积，不能替代实时工具证据。
    """

    model_config = ConfigDict(extra="forbid")

    path_id: str = Field(pattern=r"^path_[a-f0-9]{16}$")
    nodes: list[KnowledgeNode] = Field(min_length=2)
    edges: list[KnowledgeEdge] = Field(min_length=1)
    depth: int = Field(ge=1, le=2)
    score: float = Field(gt=0, le=1)
    source_ids: list[str] = Field(min_length=1)


class ScoredGraphPath(GraphPath):
    """在原始关系路径上附加种子来源、五项混合评分分量与继承的重排分数。

    继承字段中的 `score` 仍表示边权乘积形成的路径相关性，`final_score` 才是最终排序值；三层
    分数分开保存让删边消融、评分调参与审计都能判断结果变化来自图结构、种子召回还是二阶段重排。
    路径本身不单独送进 cross-encoder，`rerank_score` 由其种子节点继承，因为路径的相关性来源是
    "这个种子值得展开"，重复对拼接文本打分只会增加成本而不增加信息。
    """

    seed_node_id: str = Field(min_length=3, max_length=100)
    channels: list[RetrievalChannel] = Field(min_length=1)
    semantic_score: float = Field(ge=0, le=1)
    lexical_score: float = Field(ge=0, le=1)
    reliability_score: float = Field(ge=0, le=1)
    freshness_score: float = Field(ge=0, le=1)
    hybrid_score: float = Field(ge=0, le=1)
    rerank_score: float | None = Field(default=None, ge=0, le=1)
    final_score: float = Field(ge=0, le=1)

    @model_validator(mode="before")
    @classmethod
    def default_final_score(cls, data: object) -> object:
        """未显式给出 `final_score` 时让它等于 `hybrid_score`，与种子匹配保持同一默认语义。

        路径分数由 `score_graph_path` 计算，但消融测试和 Fixture 经常直接构造路径；共用同一默认
        规则可避免两处出现不同的"未重排"表示方式，让排序断言在两种构造路径下完全一致。
        """

        return default_final_score(data)

    @model_validator(mode="after")
    def validate_rerank_consistency(self) -> ScoredGraphPath:
        """禁止路径在没有继承 `rerank_score` 的情况下让 `final_score` 偏离 `hybrid_score`。

        路径的最终排序决定哪些证据进入上下文预算，因此这条不变量比种子层更重要：任何"看起来更相关"
        的重新排序都必须留下可核对的二阶段分数，而不能由某个中间步骤悄悄调整权重后无从追溯。
        """

        validate_rerank_consistency(self.hybrid_score, self.rerank_score, self.final_score)
        return self


class EvidenceBundleBudget(BaseModel):
    """定义注入 Planner 上下文前必须同时满足的字节、节点、路径和文档切片预算。

    字节预算使用模型无关且可精确重放的 UTF-8 JSON 长度，节点/路径/切片上限防止大量短记录绕过字节
    控制。该模型不可变并限制合理范围，调用方不能用零预算制造看似成功但完全无证据的 Bundle。
    `max_documents` 与节点预算分开计数，因为文档切片正文通常远长于知识节点，共用一个上限会让
    几条 Runbook 片段挤掉全部图证据，反而削弱"关系可解释"这一核心能力。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_bytes: int = Field(default=6000, ge=256, le=100_000)
    max_nodes: int = Field(default=8, ge=1, le=50)
    max_paths: int = Field(default=4, ge=0, le=20)
    max_documents: int = Field(default=3, ge=0, le=20)


class BundledKnowledgeNode(BaseModel):
    """表示进入上下文的紧凑知识节点证据及其稳定引用和检索优先级。

    Bundle 只保留 Planner/Auditor 需要的名称、正文、来源跨度、可靠性和最高检索分；embedding
    派生数组、别名和数据库状态不会注入 Prompt。`kn_<node_id>` 可直接进入 evidence_refs。
    """

    model_config = ConfigDict(extra="forbid")

    evidence_id: str = Field(pattern=r"^kn_[a-z][a-z0-9_-]{2,99}$")
    node_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,99}$")
    node_type: KnowledgeNodeType
    name: str = Field(min_length=1, max_length=300)
    content: str = Field(min_length=1, max_length=4000)
    source_id: str = Field(min_length=1, max_length=200)
    source_span: str = Field(min_length=1, max_length=2000)
    reliability: float = Field(ge=0, le=1)
    retrieval_score: float = Field(ge=0, le=1)


class BundledGraphPath(BaseModel):
    """表示进入上下文的紧凑图路径证据，不重复嵌入完整节点正文。

    节点正文由 `BundledKnowledgeNode` 去重保存；本模型保留有序 node/edge ID、关系类型、边来源跨度、
    原始路径分和最终混合分，使 Planner 可引用真实 `path_id`，Auditor 可逐边核对关系依据。
    """

    model_config = ConfigDict(extra="forbid")

    evidence_id: str = Field(pattern=r"^path_[a-f0-9]{16}$")
    path_id: str = Field(pattern=r"^path_[a-f0-9]{16}$")
    seed_node_id: str = Field(min_length=3, max_length=100)
    node_ids: list[str] = Field(min_length=2)
    edge_ids: list[str] = Field(min_length=1)
    relation_types: list[KnowledgeRelationType] = Field(min_length=1)
    edge_source_spans: list[str] = Field(min_length=1)
    source_ids: list[str] = Field(min_length=1)
    depth: int = Field(ge=1, le=2)
    path_score: float = Field(gt=0, le=1)
    hybrid_score: float = Field(ge=0, le=1)


class GraphEvidenceBundle(BaseModel):
    """封装预算化知识节点、图路径与文档切片，以及所有因预算被省略的稳定 ID。

    `used_bytes` 只计算 selected_nodes/selected_paths/selected_documents 的规范 JSON 载荷，便于
    精确断言上下文主体不超限；诊断元数据和 omitted IDs 不计入模型上下文预算。`truncated` 明确提示
    Planner 证据集合并非全量，防止其把预算裁剪误解为知识库不存在其他候选。文档切片与图证据放在
    同一个 Bundle 而不是两个并行对象，是为了让 Planner 与 Auditor 面对同一份证据清单和同一套
    `kn_*` / `path_*` / `dc_*` 引用空间，避免"报告引用了 Auditor 没看到的那一半上下文"。
    """

    model_config = ConfigDict(extra="forbid")

    contract_id: Literal["graphrag-evidence-bundle:v2"] = GRAPH_EVIDENCE_BUNDLE_CONTRACT_ID
    retrieval_contract_id: Literal["graphrag-retrieval:v3"] = GRAPH_RETRIEVAL_CONTRACT_ID
    query: str = Field(min_length=1, max_length=2000)
    retrieval_mode: RetrievalMode
    budget: EvidenceBundleBudget
    used_bytes: int = Field(ge=0)
    selected_nodes: list[BundledKnowledgeNode] = Field(default_factory=list)
    selected_paths: list[BundledGraphPath] = Field(default_factory=list)
    selected_documents: list[BundledDocumentChunk] = Field(default_factory=list)
    omitted_node_ids: list[str] = Field(default_factory=list)
    omitted_path_ids: list[str] = Field(default_factory=list)
    omitted_chunk_ids: list[str] = Field(default_factory=list)
    truncated: bool = False


class GraphRetrievalResult(BaseModel):
    """表示一次检索的原始查询、种子节点、去重图路径与二阶段重排溯源信息。

    服务层不生成自然语言结论，只返回可验证结构供 Planner 受上下文预算选择；空 seeds/paths 是
    合法“未召回”结果，调用方应降级并声明不确定性，不能伪造知识证据。`reranker_model` 为空即
    表示本次只跑了一阶段；`candidate_count` 记录重排前的候选规模，使"重排带来多少提升"这一问题
    在评测里有可核对的分母，而不是只能看最终名次。
    """

    model_config = ConfigDict(extra="forbid")

    contract_id: Literal["graphrag-retrieval:v3"] = GRAPH_RETRIEVAL_CONTRACT_ID
    query: str = Field(min_length=1, max_length=2000)
    mode: RetrievalMode = RetrievalMode.HYBRID_GRAPH
    seed_limit: int = Field(default=5, ge=1, le=20)
    max_hops: int = Field(default=2, ge=1, le=2)
    embedding_provider: str = Field(min_length=1, max_length=100)
    reranker_model: str | None = Field(default=None, min_length=1, max_length=200)
    candidate_count: int = Field(default=0, ge=0)
    score_weights: HybridScoringWeights
    rerank_blend_weight: float = Field(default=0, ge=0, le=1)
    seeds: list[HybridSeedMatch] = Field(default_factory=list)
    paths: list[ScoredGraphPath] = Field(default_factory=list)
