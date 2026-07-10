"""GraphRAG 节点、关系、种子匹配和路径证据模型。

枚举与 Prompt 契约保持一致，种子 Bundle 会拒绝重复 ID、悬空边和自环。GraphPath 同时
保存完整节点、边、来源和稳定 path_id，使 Planner/Auditor 能逐项引用而非依赖文本摘要。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class KnowledgeNodeType(StrEnum):
    COMPONENT = "component"
    TASK = "task"
    DATASET = "dataset"
    SYMPTOM = "symptom"
    ROOT_CAUSE = "root_cause"
    SOLUTION = "solution"
    CASE = "case"
    SOP = "sop"


class KnowledgeRelationType(StrEnum):
    RUNS_ON = "RUNS_ON"
    DEPENDS_ON = "DEPENDS_ON"
    PRODUCES = "PRODUCES"
    CONSUMES = "CONSUMES"
    MANIFESTS_AS = "MANIFESTS_AS"
    CAUSED_BY = "CAUSED_BY"
    RESOLVED_BY = "RESOLVED_BY"
    SIMILAR_TO = "SIMILAR_TO"


class KnowledgeNode(BaseModel):
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
    model_config = ConfigDict(extra="forbid")

    edge_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,99}$")
    from_node_id: str = Field(min_length=3, max_length=100)
    to_node_id: str = Field(min_length=3, max_length=100)
    relation_type: KnowledgeRelationType
    weight: float = Field(default=1, gt=0, le=1)
    source_id: str = Field(min_length=1, max_length=200)
    source_span: str = Field(min_length=1, max_length=2000)


class KnowledgeSeedBundle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seed_version: str = Field(pattern=r"^graph-seed:v[0-9]+$")
    source_id: str = Field(min_length=1, max_length=200)
    nodes: list[KnowledgeNode] = Field(min_length=1)
    edges: list[KnowledgeEdge] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_and_linked_graph(self) -> KnowledgeSeedBundle:
        node_ids = [node.node_id for node in self.nodes]
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("knowledge seed contains duplicate node IDs")

        edge_ids = [edge.edge_id for edge in self.edges]
        if len(edge_ids) != len(set(edge_ids)):
            raise ValueError("knowledge seed contains duplicate edge IDs")

        known_nodes = set(node_ids)
        for edge in self.edges:
            if edge.from_node_id not in known_nodes or edge.to_node_id not in known_nodes:
                raise ValueError(f"edge {edge.edge_id} references an unknown node")
            if edge.from_node_id == edge.to_node_id:
                raise ValueError(f"edge {edge.edge_id} cannot be a self-loop")
        return self


class LexicalSeedMatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node: KnowledgeNode
    lexical_score: float = Field(ge=0)


class GraphPath(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path_id: str = Field(pattern=r"^path_[a-f0-9]{16}$")
    nodes: list[KnowledgeNode] = Field(min_length=2)
    edges: list[KnowledgeEdge] = Field(min_length=1)
    depth: int = Field(ge=1, le=2)
    score: float = Field(gt=0, le=1)
    source_ids: list[str] = Field(min_length=1)


class GraphRetrievalResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=2000)
    seeds: list[LexicalSeedMatch] = Field(default_factory=list)
    paths: list[GraphPath] = Field(default_factory=list)
