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
    """验证人工知识种子的版本、规模、类型白名单、来源跨度和向量阶段边界。

    节点/边数量保护已评审图结构，枚举子集与 source_span 断言保证每项可追溯；JSON 中 embedding
    保持为空，要求启动流程通过可替换 Provider 生成向量，避免把某个向量空间硬编码进人工知识。
    """

    bundle = load_knowledge_seed(SEED_FILE)

    assert bundle.seed_version == "graph-seed:v5"
    assert len(bundle.nodes) == 23
    assert len(bundle.edges) == 21
    assert {node.node_type for node in bundle.nodes} <= set(KnowledgeNodeType)
    assert {edge.relation_type for edge in bundle.edges} <= set(KnowledgeRelationType)
    assert all(node.source_span for node in bundle.nodes)
    assert all(edge.source_span for edge in bundle.edges)
    assert all(node.embedding is None for node in bundle.nodes)


def test_cross_component_seed_contains_a_two_hop_three_component_path() -> None:
    """验证种子显式包含方向正确的 LTS→BDS→FlashSync 两跳依赖链。

    测试按稳定 edge_id 读取两条边，并检查第一条终点等于第二条起点；这比只搜索组件名称更能
    证明 GraphRAG 有可递归连接的真实拓扑，为 PostgreSQL 路径扩展和删边消融建立前提。
    """

    bundle = load_knowledge_seed(SEED_FILE)
    edges = {edge.edge_id: edge for edge in bundle.edges}

    first = edges["edge_lts_depends_bds"]
    second = edges["edge_bds_depends_flashsync"]
    assert first.from_node_id == "component_lts"
    assert first.to_node_id == second.from_node_id == "component_bds"
    assert second.to_node_id == "component_flashsync"


def test_single_component_seed_contains_lts_parameter_cause_and_solution_path() -> None:
    """验证 v2 种子新增的 LTS 参数故障路径具有正确方向和独立来源。

    症状必须先以 CAUSED_BY 指向参数根因，再由根因以 RESOLVED_BY 指向校验方案；两条新增边使用
    v2 source，避免修改旧跨组件知识的出处后丢失演进记录。该静态门禁在 PostgreSQL 递归查询前
    捕获倒边、错关系或复制旧 source_id 的数据错误。
    """

    bundle = load_knowledge_seed(SEED_FILE)
    edges = {edge.edge_id: edge for edge in bundle.edges}

    cause = edges["edge_lts_parameter_failure_caused_by_invalid_format"]
    solution = edges["edge_lts_invalid_parameter_resolved_by_validation"]
    assert cause.from_node_id == "symptom_lts_parameter_validation_failure"
    assert (
        cause.to_node_id
        == solution.from_node_id
        == "root_cause_lts_invalid_partition_parameter"
    )
    assert solution.to_node_id == "solution_validate_lts_runtime_parameters"
    assert cause.relation_type is KnowledgeRelationType.CAUSED_BY
    assert solution.relation_type is KnowledgeRelationType.RESOLVED_BY
    assert {cause.source_id, solution.source_id} == {"synthetic_cross_chain_knowledge_v2"}


def test_single_component_seed_contains_bds_skew_cause_and_solution_path() -> None:
    """验证 v3 种子把 BDS 长尾、数据倾斜和再平衡方案连接为有序路径。

    两条边必须使用 v3 source 且方向为症状到根因再到方案。该门禁避免仅新增三个相似文本节点却
    没有可扩展关系，也防止把 v3 知识错误标成 v1/v2 来源而破坏面试时可解释的演进历史。
    """

    bundle = load_knowledge_seed(SEED_FILE)
    edges = {edge.edge_id: edge for edge in bundle.edges}

    cause = edges["edge_bds_long_tail_caused_by_data_skew"]
    solution = edges["edge_bds_skew_resolved_by_rebalance"]
    assert cause.from_node_id == "symptom_bds_long_tail_stage"
    assert (
        cause.to_node_id == solution.from_node_id == "root_cause_bds_data_skew"
    )
    assert solution.to_node_id == "solution_rebalance_bds_skew"
    assert cause.relation_type is KnowledgeRelationType.CAUSED_BY
    assert solution.relation_type is KnowledgeRelationType.RESOLVED_BY
    assert {cause.source_id, solution.source_id} == {"synthetic_cross_chain_knowledge_v3"}


def test_single_component_seed_contains_flashsync_checkpoint_recovery_path() -> None:
    """验证 v4 种子把位点落后、检查点回退和受控恢复连接为高风险路径。

    原因边与方案边必须按症状→根因→方案连接并保留 v4 source。静态检查不能衡量恢复安全性，但能
    防止知识 JSON 只写“重放”文本却缺少显式因果关系，或把新知识错误归入旧版本来源。
    """

    bundle = load_knowledge_seed(SEED_FILE)
    edges = {edge.edge_id: edge for edge in bundle.edges}

    cause = edges["edge_flashsync_checkpoint_lag_caused_by_regression"]
    solution = edges["edge_flashsync_checkpoint_regression_resolved_by_validation"]
    assert cause.from_node_id == "symptom_flashsync_checkpoint_lag"
    assert (
        cause.to_node_id
        == solution.from_node_id
        == "root_cause_flashsync_checkpoint_regression"
    )
    assert solution.to_node_id == "solution_validate_flashsync_checkpoint_restore"
    assert cause.relation_type is KnowledgeRelationType.CAUSED_BY
    assert solution.relation_type is KnowledgeRelationType.RESOLVED_BY
    assert {cause.source_id, solution.source_id} == {"synthetic_cross_chain_knowledge_v4"}


def test_single_component_seed_contains_flashsync_schema_mapping_path() -> None:
    """验证 v5 种子把 Schema 拒绝、映射滞后和兼容性验证连接为有序路径。

    两条新边必须来自 v5 且使用 CAUSED_BY/RESOLVED_BY。该检查保证映射方案来自显式图关系，并
    防止只在 Fixture 中硬编码错误码却没有可复用的知识解释和验证步骤。
    """

    bundle = load_knowledge_seed(SEED_FILE)
    edges = {edge.edge_id: edge for edge in bundle.edges}

    cause = edges["edge_flashsync_schema_rejection_caused_by_outdated_mapping"]
    solution = edges["edge_flashsync_outdated_mapping_resolved_by_validation"]
    assert cause.from_node_id == "symptom_flashsync_schema_rejection"
    assert (
        cause.to_node_id
        == solution.from_node_id
        == "root_cause_flashsync_schema_mapping_outdated"
    )
    assert solution.to_node_id == "solution_validate_flashsync_schema_mapping"
    assert cause.relation_type is KnowledgeRelationType.CAUSED_BY
    assert solution.relation_type is KnowledgeRelationType.RESOLVED_BY
    assert {cause.source_id, solution.source_id} == {"synthetic_cross_chain_knowledge_v5"}


def test_seed_rejects_dangling_edge_reference() -> None:
    """验证任一边指向未声明节点时 KnowledgeSeedBundle 在入库前拒绝数据。

    测试从合法 JSON 仅修改一个目标 ID，期望跨对象 validator 抛出 ValidationError；该失败保护
    不依赖数据库外键，因此坏知识能在容器建立连接和开启事务之前得到清晰反馈。
    """

    payload = json.loads(SEED_FILE.read_text(encoding="utf-8"))
    payload["edges"][0]["to_node_id"] = "component_missing"

    with pytest.raises(ValidationError, match="references an unknown node"):
        KnowledgeSeedBundle.model_validate(payload)
