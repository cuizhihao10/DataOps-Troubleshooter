"""GraphRAG 节点、关系、种子匹配和路径证据模型。

枚举与 Prompt 契约保持一致，种子 Bundle 会拒绝重复 ID、悬空边和自环。GraphPath 同时
保存完整节点、边、来源和稳定 path_id，使 Planner/Auditor 能逐项引用而非依赖文本摘要。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


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


class GraphRetrievalResult(BaseModel):
    """表示一次检索的原始查询、种子节点和去重图路径集合。

    服务层不生成自然语言结论，只返回可验证结构供 Planner 受上下文预算选择；空 seeds/paths 是
    合法“未召回”结果，调用方应降级并声明不确定性，不能伪造知识证据。
    """

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=2000)
    seeds: list[LexicalSeedMatch] = Field(default_factory=list)
    paths: list[GraphPath] = Field(default_factory=list)
