"""验证人工 GraphRAG 种子的类型、来源和拓扑完整性。

单元测试在不启动数据库时检查节点/关系白名单、source_span、两跳组件链路和悬空边拒绝，
让错误知识在进入 Alembic 管理的正式图表之前失败。
"""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.retrieval.models import (
    KnowledgeNodeType,
    KnowledgeRelationType,
    KnowledgeSeedBundle,
)
from app.retrieval.seeds import load_knowledge_seed

SEED_FILE = Path("data/knowledge/cross_chain_graph.json")


def test_curated_seed_uses_approved_node_and_relation_contracts() -> None:
    bundle = load_knowledge_seed(SEED_FILE)

    assert bundle.seed_version == "graph-seed:v1"
    assert len(bundle.nodes) == 11
    assert len(bundle.edges) == 13
    assert {node.node_type for node in bundle.nodes} <= set(KnowledgeNodeType)
    assert {edge.relation_type for edge in bundle.edges} <= set(KnowledgeRelationType)
    assert all(node.source_span for node in bundle.nodes)
    assert all(edge.source_span for edge in bundle.edges)
    assert all(node.embedding is None for node in bundle.nodes)


def test_cross_component_seed_contains_a_two_hop_three_component_path() -> None:
    bundle = load_knowledge_seed(SEED_FILE)
    edges = {edge.edge_id: edge for edge in bundle.edges}

    first = edges["edge_lts_depends_bds"]
    second = edges["edge_bds_depends_flashsync"]
    assert first.from_node_id == "component_lts"
    assert first.to_node_id == second.from_node_id == "component_bds"
    assert second.to_node_id == "component_flashsync"


def test_seed_rejects_dangling_edge_reference() -> None:
    payload = json.loads(SEED_FILE.read_text(encoding="utf-8"))
    payload["edges"][0]["to_node_id"] = "component_missing"

    with pytest.raises(ValidationError, match="references an unknown node"):
        KnowledgeSeedBundle.model_validate(payload)
