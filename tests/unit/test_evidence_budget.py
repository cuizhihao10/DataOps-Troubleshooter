"""验证 GraphRAG Evidence Bundle 的精确字节预算、路径原子性和省略诊断信息。

测试使用完整 Pydantic 检索模型构造一条合成路径，不依赖数据库；它确保构建器不会超过 UTF-8
上下文预算，不会只纳入路径的一部分节点，并为所有未选候选保留稳定 omitted ID。

文档切片作为第二条知识通道共用同一个字节预算但拥有独立条数上限，因此这里额外锁定加载顺序
（路径 → 种子节点 → 文档切片）与"预算紧张时先保住图证据"这一优先级：反过来的实现同样不会
报错，只会让本系统区别于普通 RAG 的关系可解释性被几段长 Runbook 正文挤出上下文。
"""

import json

from app.retrieval.budget import build_evidence_bundle
from app.retrieval.documents import (
    DocumentChunk,
    DocumentMetadata,
    DocumentRetrievalResult,
    DocumentScoringWeights,
    DocumentType,
    ScoredDocumentChunk,
    make_chunk_id,
)
from app.retrieval.models import (
    EvidenceBundleBudget,
    GraphRetrievalResult,
    HybridScoringWeights,
    HybridSeedMatch,
    KnowledgeEdge,
    KnowledgeNode,
    RetrievalChannel,
    RetrievalMode,
    ScoredGraphPath,
)


def _retrieval_result() -> GraphRetrievalResult:
    """构造一个双节点一跳的 hybrid_graph 结果，包含种子和可引用完整路径。

    节点正文包含中文以验证 UTF-8 多字节计算，路径保留 edge source_span 和独立 hybrid_score；
    原始 embedding 为空，符合真实检索结果不会把派生向量注入上下文的边界。
    """

    seed_node = KnowledgeNode(
        node_id="symptom_demo_backlog",
        node_type="symptom",
        name="合成积压",
        content="合成同步吞吐下降并出现待处理记录。",
        source_id="synthetic_budget_source",
        source_span="同步吞吐下降并出现待处理记录",
        reliability=1.0,
    )
    root_node = KnowledgeNode(
        node_id="root_cause_demo_conflict",
        node_type="root_cause",
        name="合成主键冲突",
        content="目标端重复键导致同步批次暂停。",
        source_id="synthetic_budget_source",
        source_span="重复键导致同步批次暂停",
        reliability=0.95,
    )
    edge = KnowledgeEdge(
        edge_id="edge_demo_backlog_conflict",
        from_node_id=seed_node.node_id,
        to_node_id=root_node.node_id,
        relation_type="CAUSED_BY",
        weight=1.0,
        source_id="synthetic_budget_source",
        source_span="合成积压由主键冲突导致",
    )
    seed = HybridSeedMatch(
        node=seed_node,
        channels=[RetrievalChannel.LEXICAL, RetrievalChannel.VECTOR],
        semantic_score=0.9,
        lexical_score=0.7,
        reliability_score=1.0,
        freshness_score=0.0,
        hybrid_score=0.575,
    )
    path = ScoredGraphPath(
        path_id="path_0123456789abcdef",
        nodes=[seed_node, root_node],
        edges=[edge],
        depth=1,
        score=1.0,
        source_ids=["synthetic_budget_source"],
        seed_node_id=seed_node.node_id,
        channels=seed.channels,
        semantic_score=seed.semantic_score,
        lexical_score=seed.lexical_score,
        reliability_score=seed.reliability_score,
        freshness_score=seed.freshness_score,
        hybrid_score=0.825,
    )
    return GraphRetrievalResult(
        query="合成同步积压",
        mode=RetrievalMode.HYBRID_GRAPH,
        embedding_provider="unit-provider:v1",
        score_weights=HybridScoringWeights(),
        seeds=[seed],
        paths=[path],
    )


def _selected_payload_size(bundle) -> int:
    """按生产构建器相同的规范 JSON 规则计算测试 Bundle 主体 UTF-8 字节数。

    测试独立重算而不调用私有实现，能够发现 `used_bytes` 只写固定值或遗漏中文多字节的回归；
    三个 selected 列表必须一起计入，因为文档切片和图证据共用同一个字节预算，漏算任何一个都会让
    "预算内"这一结论失真。omitted IDs 和预算诊断元数据按契约不计入上下文主体。
    """

    payload = {
        "selected_nodes": [node.model_dump(mode="json") for node in bundle.selected_nodes],
        "selected_paths": [path.model_dump(mode="json") for path in bundle.selected_paths],
        "selected_documents": [
            chunk.model_dump(mode="json") for chunk in bundle.selected_documents
        ],
    }
    return len(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def test_generous_budget_selects_complete_path_and_exact_utf8_size() -> None:
    """验证充足预算会原子纳入路径及两个节点，并准确报告规范 JSON 字节数。

    断言 path_id/kn 引用、节点集合和未截断状态，证明路径没有只保留边而缺少正文；独立字节重算
    同时保证 `used_bytes <= max_bytes` 不是依靠低估中文字符得到的假通过。
    """

    budget = EvidenceBundleBudget(max_bytes=5000, max_nodes=2, max_paths=1)
    bundle = build_evidence_bundle(_retrieval_result(), budget=budget)

    assert {node.node_id for node in bundle.selected_nodes} == {
        "symptom_demo_backlog",
        "root_cause_demo_conflict",
    }
    assert [path.path_id for path in bundle.selected_paths] == ["path_0123456789abcdef"]
    assert all(node.evidence_id.startswith("kn_") for node in bundle.selected_nodes)
    assert bundle.used_bytes == _selected_payload_size(bundle)
    assert bundle.used_bytes <= budget.max_bytes
    assert bundle.truncated is False


def test_zero_path_budget_keeps_seed_but_omits_entire_path_and_unselected_node() -> None:
    """验证路径上限为零时不会泄漏半条路径，只保留可独立解释的高分种子节点。

    根因节点仅由路径引入，因此必须与 path_id 一起进入 omitted 列表；种子仍可作为知识节点证据，
    展示节点和路径预算是相互独立的，不会因为关闭图扩展而返回空成功。
    """

    budget = EvidenceBundleBudget(max_bytes=5000, max_nodes=2, max_paths=0)
    bundle = build_evidence_bundle(_retrieval_result(), budget=budget)

    assert [node.node_id for node in bundle.selected_nodes] == ["symptom_demo_backlog"]
    assert bundle.selected_paths == []
    assert bundle.omitted_node_ids == ["root_cause_demo_conflict"]
    assert bundle.omitted_path_ids == ["path_0123456789abcdef"]
    assert bundle.truncated is True


def _document_result(*, content_chars: int = 20) -> DocumentRetrievalResult:
    """构造一个含三条降序评分切片的文档检索结果，正文长度可调以便制造字节压力。

    分数刻意两两不同，使"按最终分加入"与"按 chunk_id 排序"两种实现能被区分；正文长度参数让同一
    组候选既能用于宽预算断言，也能用于"长 Runbook 片段挤不进剩余字节"的优先级断言。
    """

    document = DocumentMetadata(
        doc_id="runbook_budget_demo",
        doc_type=DocumentType.RUNBOOK,
        title="合成主键冲突处置手册",
        components=["flashsync"],
        source_id="synthetic_budget_document",
        revision="r1",
        reliability=0.9,
    )
    chunks: list[ScoredDocumentChunk] = []
    for ordinal, hybrid_score in enumerate((0.9, 0.6, 0.3)):
        content = f"第 {ordinal} 段处置步骤：" + "暂停任务并核对主键分布。" * content_chars
        chunks.append(
            ScoredDocumentChunk(
                document=document,
                chunk=DocumentChunk(
                    chunk_id=make_chunk_id(document.doc_id, ordinal),
                    doc_id=document.doc_id,
                    ordinal=ordinal,
                    heading_path=f"{document.title} > 处置步骤",
                    content=content,
                    char_count=len(content),
                ),
                channels=[RetrievalChannel.VECTOR],
                semantic_score=hybrid_score,
                authority_score=document.reliability,
                hybrid_score=hybrid_score,
            )
        )
    return DocumentRetrievalResult(
        query="合成同步积压",
        embedding_provider="unit-provider:v1",
        candidate_count=len(chunks),
        score_weights=DocumentScoringWeights(),
        chunks=chunks,
    )


def test_document_chunks_enter_the_bundle_with_stable_dc_references() -> None:
    """验证充足预算下文档切片按最终分进入 Bundle，引用为 `dc_*` 且不携带评分分量。

    `dc_*` 同时作为 evidence_id 与数据库主键，报告脚注、Auditor 核对和文档表因此指向同一标识；
    只保留 `retrieval_score` 是刻意的——把语义/全文/权威度分量注入 Prompt 只会让模型把内部排序
    数字当成事实强度。独立字节重算同时确认三类证据合并计费。
    """

    budget = EvidenceBundleBudget(max_bytes=6000, max_nodes=8, max_paths=4, max_documents=3)
    bundle = build_evidence_bundle(
        _retrieval_result(),
        budget=budget,
        documents=_document_result(),
    )

    assert [chunk.retrieval_score for chunk in bundle.selected_documents] == [0.9, 0.6, 0.3]
    assert all(chunk.evidence_id == chunk.chunk_id for chunk in bundle.selected_documents)
    assert all(chunk.evidence_id.startswith("dc_") for chunk in bundle.selected_documents)
    assert bundle.omitted_chunk_ids == []
    assert bundle.truncated is False
    assert bundle.used_bytes == _selected_payload_size(bundle)
    assert bundle.used_bytes <= budget.max_bytes


def test_document_cap_is_counted_separately_from_the_node_budget() -> None:
    """验证文档条数上限独立生效：图证据不受影响，落选切片全部进入 omitted 列表。

    共用一个上限会让几条 Runbook 片段挤掉全部图证据，而关系可解释性正是本系统区别于普通 RAG 的
    部分；反过来，被截断的切片 ID 必须可见，否则 Planner 无法判断上下文是完整的还是被预算裁过的。
    """

    documents = _document_result()
    budget = EvidenceBundleBudget(max_bytes=6000, max_nodes=8, max_paths=4, max_documents=1)
    bundle = build_evidence_bundle(_retrieval_result(), budget=budget, documents=documents)

    assert len(bundle.selected_documents) == 1
    assert bundle.selected_documents[0].chunk_id == documents.chunks[0].chunk.chunk_id
    assert bundle.omitted_chunk_ids == sorted(
        chunk.chunk.chunk_id for chunk in documents.chunks[1:]
    )
    assert len(bundle.selected_paths) == 1
    assert len(bundle.selected_nodes) == 2
    assert bundle.truncated is True


def test_zero_document_budget_and_absent_channel_are_both_graph_only() -> None:
    """验证 `max_documents=0` 与未传 documents 都得到只含图证据的 Bundle，但截断状态不同。

    两者在证据主体上同形，差异只体现在 omitted 列表：预算为零是"有候选但被裁掉"，未传通道是
    "本次没有文档通道"。把它们混为一谈会让报告无法区分"没查到"和"查到了但没放进上下文"。
    """

    budget = EvidenceBundleBudget(max_bytes=6000, max_nodes=8, max_paths=4, max_documents=0)
    capped = build_evidence_bundle(
        _retrieval_result(),
        budget=budget,
        documents=_document_result(),
    )
    absent = build_evidence_bundle(_retrieval_result(), budget=budget)

    assert capped.selected_documents == []
    assert len(capped.omitted_chunk_ids) == 3
    assert capped.truncated is True
    assert absent.selected_documents == []
    assert absent.omitted_chunk_ids == []
    assert absent.truncated is False


def test_byte_pressure_evicts_long_chunks_but_keeps_the_graph_path() -> None:
    """验证剩余字节装不下长切片时整段省略，图路径与节点保持完整。

    加载顺序是路径 → 种子节点 → 文档切片，因此预算紧张时先保住"故障如何沿依赖传播"；切片按整段
    省略而不是截断正文，因为半条处置步骤既不可执行，还会让引用看起来仍然完整。
    """

    documents = _document_result(content_chars=60)
    budget = EvidenceBundleBudget(max_bytes=1500, max_nodes=8, max_paths=4, max_documents=3)
    bundle = build_evidence_bundle(_retrieval_result(), budget=budget, documents=documents)

    assert [path.path_id for path in bundle.selected_paths] == ["path_0123456789abcdef"]
    assert len(bundle.selected_nodes) == 2
    assert bundle.selected_documents == []
    assert bundle.omitted_chunk_ids == sorted(
        chunk.chunk.chunk_id for chunk in documents.chunks
    )
    assert bundle.used_bytes == _selected_payload_size(bundle)
    assert bundle.used_bytes <= budget.max_bytes


def test_tiny_byte_budget_never_exceeds_limit_and_reports_all_omissions() -> None:
    """验证最小允许字节预算装不下候选时返回明确截断，而不是超限或截断文本字段。

    构建器允许只保留规范空载荷包装；所有节点和路径 ID 必须出现在 omitted 列表，正文保持完整地
    被省略而不是按字符切断，便于 Planner 知道需要扩大预算或重新检索。
    """

    budget = EvidenceBundleBudget(max_bytes=256, max_nodes=2, max_paths=1)
    bundle = build_evidence_bundle(_retrieval_result(), budget=budget)

    assert bundle.selected_nodes == []
    assert bundle.selected_paths == []
    assert bundle.used_bytes == _selected_payload_size(bundle)
    assert bundle.used_bytes <= budget.max_bytes
    assert set(bundle.omitted_node_ids) == {
        "symptom_demo_backlog",
        "root_cause_demo_conflict",
    }
    assert bundle.omitted_path_ids == ["path_0123456789abcdef"]
    assert bundle.truncated is True
