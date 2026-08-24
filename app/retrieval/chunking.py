"""标题感知的确定性 Markdown 切片器。

文档 RAG 的召回质量在很大程度上由切片方式决定：按固定字符窗口盲切会把一条处置步骤劈成两半，
让检索命中的片段无法执行。本模块因此先按 Markdown 标题划出语义边界，再在段落边界内贪心装箱到
字符上限，并把"文档标题 > 章节 > 小节"的层级写进每个片段，使引用能说明步骤出自哪一节。

切片完全确定性且不依赖任何模型或第三方解析库：同一份文档任意次导入都得到相同的 `dc_*` 引用，
历史报告里的脚注因此不会在重新导入语料后指向别的正文。
"""

from __future__ import annotations

import re

from app.retrieval.documents import (
    MAX_CHUNK_CHARS,
    DocumentChunk,
    DocumentType,
    KnowledgeDocument,
    make_chunk_id,
)

_HEADING_PATTERN = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
_MAX_HEADING_PATH_CHARS = 500


def chunk_markdown_document(
    *,
    doc_id: str,
    doc_type: DocumentType,
    title: str,
    components: list[str],
    source_id: str,
    revision: str,
    reliability: float,
    markdown: str,
) -> KnowledgeDocument:
    """把一份 Markdown 运维文档确定性地切成带标题路径的片段并组装成文档对象。

    先按标题划分语义段落再装箱，因此每个片段至少落在同一小节内；正文为空的文档显式失败而不是返回
    零片段文档，因为一个没有片段的文档在检索侧完全不可见，静默通过只会让语料缺失在评测阶段才暴露。
    """

    sections = _split_sections(title, markdown)
    chunks: list[DocumentChunk] = []
    for heading_path, body in sections:
        for content in _pack_paragraphs(body):
            ordinal = len(chunks)
            chunks.append(
                DocumentChunk(
                    chunk_id=make_chunk_id(doc_id, ordinal),
                    doc_id=doc_id,
                    ordinal=ordinal,
                    heading_path=heading_path,
                    content=content,
                    char_count=len(content),
                )
            )
    if not chunks:
        raise ValueError(f"document {doc_id} produced no chunks")

    return KnowledgeDocument(
        doc_id=doc_id,
        doc_type=doc_type,
        title=title,
        components=components,
        source_id=source_id,
        revision=revision,
        reliability=reliability,
        chunks=chunks,
    )


def _split_sections(title: str, markdown: str) -> list[tuple[str, str]]:
    """按 Markdown 标题层级把正文划分为 `(标题路径, 正文)` 段落序列。

    标题栈按 `#` 数量裁剪，使 `## 处置步骤` 之后的 `### 回滚` 得到完整祖先路径；文档标题始终作为
    路径根，即使正文没有任何标题也能给出可读出处。只有正文非空的段落进入结果，纯标题不产生片段。
    """

    sections: list[tuple[str, str]] = []
    heading_stack: list[str] = []
    buffer: list[str] = []

    def flush() -> None:
        """把当前缓冲的正文与当前标题栈组成一个段落写入结果。

        缓冲在每次遇到新标题和遍历结束时清空；全为空白的缓冲直接丢弃，避免相邻标题之间的空行
        产生正文为空的片段，那种片段既无法执行也会白占上下文预算。
        """

        body = "\n".join(buffer).strip()
        buffer.clear()
        if body:
            sections.append((_heading_path(title, heading_stack), body))

    for line in markdown.splitlines():
        match = _HEADING_PATTERN.match(line)
        if match is None:
            buffer.append(line)
            continue
        flush()
        # 标题栈按层级裁剪而不是简单追加，否则同级标题会不断叠加出越来越长的虚假祖先路径。
        level = len(match.group(1))
        del heading_stack[level - 1 :]
        heading_stack.append(match.group(2))
    flush()
    return sections


def _heading_path(title: str, heading_stack: list[str]) -> str:
    """把文档标题与标题栈拼成有长度上限的可读路径。

    以文档标题为根让片段脱离原文后仍可溯源；Markdown 的 H1 通常与 manifest 声明的标题相同，因此
    重复的根级标题会被去掉，避免每个片段的出处都以同一句话开头两次而白占上下文预算。超长路径从
    尾部截断保留最具体的小节名，因为定位一条步骤时最深一级标题比顶层文档名更有信息量。
    """

    segments = list(heading_stack)
    if segments and segments[0].strip() == title.strip():
        del segments[0]
    path = " > ".join([title, *segments])
    if len(path) <= _MAX_HEADING_PATH_CHARS:
        return path
    return path[-_MAX_HEADING_PATH_CHARS:]


def _pack_paragraphs(body: str) -> list[str]:
    """在段落边界内贪心装箱，把一个小节正文切成不超过字符上限的片段。

    贪心而不是等分，是为了让绝大多数小节保持单片段完整；只有确实超限时才切开，并优先在空行、
    其次在行边界处切，最后才硬切字符，从而尽可能不把一条编号步骤劈成两半。
    """

    chunks: list[str] = []
    current: list[str] = []
    current_length = 0
    for paragraph in _split_paragraphs(body):
        for unit in _split_oversized_paragraph(paragraph):
            # +2 是重新拼接时的空行分隔符，先算进长度才能保证输出片段真的不超过上限。
            projected = current_length + (2 if current else 0) + len(unit)
            if current and projected > MAX_CHUNK_CHARS:
                chunks.append("\n\n".join(current))
                current = [unit]
                current_length = len(unit)
                continue
            current.append(unit)
            current_length = projected
    if current:
        chunks.append("\n\n".join(current))
    return chunks


def _split_paragraphs(body: str) -> list[str]:
    """按空行把小节正文拆成段落，并丢弃全为空白的片段。

    空行是 Markdown 里最可靠的语义边界，列表项之间通常没有空行，因此整份编号步骤会留在同一段落，
    装箱阶段也就更倾向于把它整体保留在一个片段内。
    """

    return [paragraph.strip() for paragraph in re.split(r"\n\s*\n", body) if paragraph.strip()]


def _split_oversized_paragraph(paragraph: str) -> list[str]:
    """把超过字符上限的单个段落按行、必要时按字符切成合法长度的单元。

    先按行切保留步骤编号的完整性；若单行本身仍然超限（例如一条很长的命令或日志样例），只能硬切
    字符——这种情况下继续保持"不超上限"比保持可读性更重要，否则该片段会被数据库长度约束整批拒绝。
    """

    if len(paragraph) <= MAX_CHUNK_CHARS:
        return [paragraph]

    units: list[str] = []
    current: list[str] = []
    current_length = 0
    for line in paragraph.splitlines():
        for piece in _split_oversized_line(line):
            projected = current_length + (1 if current else 0) + len(piece)
            if current and projected > MAX_CHUNK_CHARS:
                units.append("\n".join(current))
                current = [piece]
                current_length = len(piece)
                continue
            current.append(piece)
            current_length = projected
    if current:
        units.append("\n".join(current))
    return units


def _split_oversized_line(line: str) -> list[str]:
    """把超长单行按字符上限硬切成多段，短行原样返回。

    这是切片器的最后兜底：任何输入都必须产出长度合法的片段，否则一份文档里的一行超长日志就会让
    整批导入因数据库长度约束失败，而运维语料里恰好经常粘贴这类长行。
    """

    if len(line) <= MAX_CHUNK_CHARS:
        return [line]
    return [line[index : index + MAX_CHUNK_CHARS] for index in range(0, len(line), MAX_CHUNK_CHARS)]
