"""验证标题感知 Markdown 切片器的语义边界、长度上界与引用 ID 稳定性。

切片方式直接决定文档 RAG 能不能给出可执行步骤：按固定窗口盲切会把一条处置步骤劈成两半，而
标题路径丢失后引用只剩一段无出处的正文。这些测试因此锁定四件事——小节边界不被跨越、标题路径
带完整祖先且不重复文档标题、任何输入产出的片段都不超过 `MAX_CHUNK_CHARS`、`dc_*` 引用在重复
导入后保持不变。空正文显式失败也在这里锁定，因为零片段文档在检索侧完全不可见。
"""

from __future__ import annotations

import pytest

from app.retrieval.chunking import chunk_markdown_document
from app.retrieval.documents import MAX_CHUNK_CHARS, DocumentType, make_chunk_id

_RUNBOOK = """# FlashSync 主键冲突处置手册

## 判断依据

同步任务报 duplicate key，且源端在窗口内有重放。

## 处置步骤

1. 暂停同步任务并记录 offset。
2. 比对源端与目标端主键分布。

### 回滚

按暂停前 offset 恢复任务。
"""


def _chunk(markdown: str, *, title: str = "FlashSync 主键冲突处置手册"):
    """用固定元数据切一份 Markdown，让测试只关注切片行为本身。

    doc_id/来源/修订号等字段与切片逻辑无关，但 `KnowledgeDocument` 会校验它们，所以集中在这里
    给出一组合法取值；标题可覆盖，用于验证"H1 与 manifest 标题相同时去重"这条路径。
    """

    return chunk_markdown_document(
        doc_id="runbook_flashsync_primary_key_conflict",
        doc_type=DocumentType.RUNBOOK,
        title=title,
        components=["FlashSync"],
        source_id="synthetic-runbook-001",
        revision="v1.3",
        reliability=0.9,
        markdown=markdown,
    )


def test_sections_become_separate_chunks_with_full_heading_paths() -> None:
    """验证每个小节独立成片且标题路径包含完整祖先层级，不与相邻小节混合。

    `### 回滚` 必须得到"文档标题 > 处置步骤 > 回滚"，证明标题栈按 `#` 数量裁剪而不是无脑追加；
    若实现把同级标题不断叠加，路径会出现"判断依据 > 处置步骤"这类虚假祖先，引用出处随之失真。
    正文断言同时确认小节之间没有互相串味——处置步骤片段里不能出现判断依据的句子。
    """

    document = _chunk(_RUNBOOK)

    paths = [chunk.heading_path for chunk in document.chunks]
    assert paths == [
        "FlashSync 主键冲突处置手册 > 判断依据",
        "FlashSync 主键冲突处置手册 > 处置步骤",
        "FlashSync 主键冲突处置手册 > 处置步骤 > 回滚",
    ]
    assert "duplicate key" in document.chunks[0].content
    assert "duplicate key" not in document.chunks[1].content
    assert document.chunks[1].content.startswith("1. 暂停同步任务")


def test_heading_path_drops_an_h1_that_repeats_the_declared_title() -> None:
    """验证 Markdown H1 与 manifest 标题相同时只保留一次，标题不同时两级都保留。

    重复的根级标题会让每个片段的出处以同一句话开头两次，白占本就紧张的上下文预算；反过来，当
    正文 H1 确实是另一个名字（例如文档改名前的旧标题）时必须保留，否则引用会丢掉真实章节名。
    """

    deduped = _chunk(_RUNBOOK)
    assert deduped.chunks[0].heading_path == "FlashSync 主键冲突处置手册 > 判断依据"

    preserved = _chunk(_RUNBOOK, title="FlashSync 同步手册")
    assert preserved.chunks[0].heading_path == (
        "FlashSync 同步手册 > FlashSync 主键冲突处置手册 > 判断依据"
    )


def test_body_without_any_heading_still_gets_the_document_title_as_path() -> None:
    """验证没有任何 Markdown 标题的正文仍产出可溯源片段，路径退化为文档标题。

    运维语料里存在纯文本粘贴的短 FAQ；要求它必须带标题才可导入会把这类知识挡在语料之外，而给出
    空路径又会让引用无法说明出处，因此以文档标题作为根是唯一诚实的退化行为。
    """

    document = _chunk("同步延迟超过五分钟时先看 checkpoint 是否推进。", title="FlashSync FAQ")

    assert len(document.chunks) == 1
    assert document.chunks[0].heading_path == "FlashSync FAQ"


def test_oversized_paragraph_and_line_are_split_within_the_char_limit() -> None:
    """验证超长段落与超长单行都被切到不超过字符上限，且总正文没有丢失。

    上限存在的理由是硬约束而非美观：片段正文既受数据库长度约束，也要塞得进 `RemediationStep.action`
    的 2000 字上限。单行超限（一整行长日志）是最后兜底路径，若它不生效，整批导入会被数据库拒绝。
    断言逐片段检查长度并核对拼接后的字符总数，防止实现用"丢弃尾部"的方式满足长度要求。
    """

    long_line = "x" * (MAX_CHUNK_CHARS * 2 + 37)
    document = _chunk(f"## 日志样例\n\n{long_line}\n", title="BDS 排障手册")

    assert len(document.chunks) == 3
    assert all(chunk.char_count <= MAX_CHUNK_CHARS for chunk in document.chunks)
    assert all(chunk.char_count == len(chunk.content) for chunk in document.chunks)
    assert sum(chunk.char_count for chunk in document.chunks) == len(long_line)


def test_chunk_ids_and_ordinals_are_deterministic_across_reimports() -> None:
    """验证同一文档重复切片得到相同 `dc_*` 引用与从零连续的 ordinal。

    历史报告的脚注是 `dc_*`，一旦重新导入语料后 ID 变化，旧报告的引用就会指向别的正文——这种
    错误不会报错，只会让审计追溯悄悄失真。ordinal 连续则是"按序号拼回完整章节"这一读法的前提。
    """

    first = _chunk(_RUNBOOK)
    second = _chunk(_RUNBOOK)

    assert [chunk.chunk_id for chunk in first.chunks] == [
        chunk.chunk_id for chunk in second.chunks
    ]
    assert [chunk.ordinal for chunk in first.chunks] == [0, 1, 2]
    assert first.chunks[0].chunk_id == make_chunk_id(first.doc_id, 0)
    assert first.chunks[0].chunk_id != first.chunks[1].chunk_id


@pytest.mark.parametrize(
    "markdown",
    ["", "   \n\n\t\n", "# 只有标题\n\n## 也只有标题\n"],
    ids=["empty", "whitespace-only", "headings-only"],
)
def test_documents_without_body_text_fail_instead_of_producing_zero_chunks(
    markdown: str,
) -> None:
    """验证空正文、纯空白与纯标题文档都显式失败，而不是返回零片段文档。

    零片段文档在检索侧完全不可见：语料看起来导入成功，但那份 Runbook 永远不会被召回，这类缺失
    通常要等到评测阶段才暴露。纯标题单列一个用例，因为它最容易被"标题也算内容"的实现放过。
    """

    with pytest.raises(ValueError, match="produced no chunks"):
        _chunk(markdown)
