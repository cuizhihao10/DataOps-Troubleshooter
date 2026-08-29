# 第 6 章 文档 RAG 与证据预算：第二条知识通道，和"谁进得去 Prompt"

## 6.1 你会验证什么

```bash
.venv/Scripts/python -m pytest -q tests/unit/test_document_models.py \
  tests/unit/test_document_chunking.py tests/unit/test_document_seeds.py \
  tests/unit/test_document_retrieval_service.py tests/unit/test_evidence_budget.py
# 实测：40 passed in 0.16s

# 全文表达式索引、pgvector 距离、先删后插的 upsert 语义只能在真库验证
DATAOPS_TEST_DATABASE_URL='postgresql+asyncpg://...' \
  .venv/Scripts/python -m pytest -m postgres tests/integration/test_document_postgres.py
```

这一章读六个文件加一段装配代码：

| 文件 | 行数 | 职责 |
|---|---|---|
| `app/retrieval/documents.py` | 328 | 文档域契约：文档、切片、三因子权重、检索结果、Bundle 切片 |
| `app/retrieval/chunking.py` | 202 | 标题感知的确定性 Markdown 切片器 |
| `app/retrieval/document_seeds.py` | 72 | manifest + Markdown → 文档库（含路径逃逸校验） |
| `app/retrieval/document_repository.py` | 322 | 两张表的幂等导入、全文召回、向量召回 |
| `app/retrieval/document_service.py` | 233 | 双通道召回 → 三因子融合 → 可选精排 |
| `app/retrieval/budget.py` | 262 | 把图证据 + 文档证据裁剪成受四重预算约束的 Bundle |
| `app/orchestration/diagnosis_runtime.py` L163–229 | — | 两条通道的装配与双 trace span |

第 5 章的四段流水线（全文 + 向量 → 融合 → 精排 → 图扩展）在这一章会被**复用而不是重写**：文档
通道用同一个 `EmbeddingProvider`、同一个 `RerankerProvider`、同一份 `blend_scores` / `bounded_score`
/ `validate_rerank_consistency`。你要重点看的是**哪些地方刻意不复用**，以及每处不复用的理由。

## 6.2 为什么要第二条知识通道

知识图能回答"故障如何沿依赖传播"，但排障的最后一步是**给出可执行处置步骤**，而处置步骤只写在
Runbook / SOP / 复盘里。`documents.py` 的模块 docstring 第一句就是这个分工：

> 知识图回答"故障如何沿依赖传播"，但排障最后一步需要的是可执行处置步骤，而这些步骤只写在
> Runbook/SOP/复盘里。本模块因此定义与 GraphRAG 平行的第二条知识通道。

把处置步骤硬塞进知识图会立刻失败：一条 `remediation` 节点的 `content` 上限 4000 字符，而一份
Runbook 的"处置步骤"一节本身就有分支（"若判定为源端重复……若判定为目标端残留……"）。图节点适合
表达**一个实体**，文档切片适合表达**一段可执行流程**，两者的检索方式和评分因子都不一样。

于是本项目的做法是：两条通道并行召回，**但共享同一个引用空间**，最终合并进同一个
`GraphEvidenceBundle`。`kn_*`（知识节点）、`path_*`（图路径）、`dc_*`（文档切片）三种引用同时
出现在报告的 `evidence_refs` 里，Planner 和 Auditor 看到的是同一份清单。

`GraphEvidenceBundle` 的 docstring 把这个决定的理由写在最后一句：

> 文档切片与图证据放在同一个 Bundle 而不是两个并行对象，是为了让 Planner 与 Auditor 面对同一份
> 证据清单和同一套 `kn_*` / `path_*` / `dc_*` 引用空间，避免"报告引用了 Auditor 没看到的那一半
> 上下文"。

这是第 9 章审计能成立的前提：Auditor 逐条核对 `evidence_refs` 时，如果文档证据走另一条不进入
Auditor 视野的路，那么"引用了不存在的证据"这条规则就会对文档引用永久失效。

## 6.3 切片是唯一的检索与引用单元

先看契约的四个常量和四个模型的关系：

```python
DOCUMENT_RETRIEVAL_CONTRACT_ID = "document-retrieval:v1"
MAX_CHUNK_CHARS = 1200
MIN_CHUNK_CHARS = 40
MAX_DOCUMENT_CHUNKS = 200
```

```
DocumentMetadata（文档元信息，不含正文）
  └─ KnowledgeDocument(DocumentMetadata) ＋ chunks: list[DocumentChunk]   ← 只有导入路径用
       └─ DocumentLibrary  ← 一次导入的原子单元，带 library_version

检索路径用的是：DocumentMetadata ＋ 单个 DocumentChunk
```

`KnowledgeDocument` 用**继承**而不是组合，这样"元数据 + 切片"和"元数据"共用同一批字段定义，
不会出现两处字段漂移。它的 docstring 说明了为什么检索路径不用它：

> 只有导入路径需要完整切片；检索路径一律使用父类元数据加单个命中切片。

这条分工在仓储层有一个直接后果，`_document_from_mapping` 的 docstring 讲得最清楚：

> 检索路径每次只关心命中的那一个切片，因此这里不回查同文档其它切片，也不构造任何占位正文——
> 检索结果中出现凭空拼装的片段会直接污染证据链，元数据与切片分离正是为了排除这种可能。

**"不构造占位正文"是一条证据完整性要求，不是性能优化。** 如果模型层拿到的是一个 `content=""`
的占位切片，它无法区分"这一节确实没内容"和"程序没把内容取出来"，而报告会照样引用这个 `dc_*`。

### 切片自身的两条不变量

```python
    @model_validator(mode="after")
    def validate_chunk_invariants(self) -> DocumentChunk:
        if self.char_count != len(self.content):
            raise ValueError("char_count must match the chunk content length")

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
```

第一条是 `char_count == len(content)`。为什么要冗余存一个长度、还要校验它？docstring 给了理由：

> char_count 是数据库层的成本与预算依据，若它和正文脱节，上下文预算就会按错误长度裁剪证据。

第二条是**全有或全无的 embedding 三元组**，与第 5 章知识节点的规则逐条相同：向量、Provider ID、
维度必须同时在或同时不在；长度必须等于声明维度；值必须有限；不能全零。全零向量在 cosine 距离
下是未定义的（分母为零），而 pgvector 会返回 NaN 后被排序静默吞掉——这类错误不报错、只是永远
召不回，所以必须在**建模层**拒绝。

### 库级校验：为什么重复 doc_id 必须在加载阶段失败

```python
    @model_validator(mode="after")
    def validate_unique_documents(self) -> DocumentLibrary:
        doc_ids = [document.doc_id for document in self.documents]
        if len(doc_ids) != len(set(doc_ids)):
            raise ValueError("document library contains duplicate document IDs")
        return self
```

docstring 把因果链写全了：

> 文档 upsert 按 doc_id 先删切片再插入，因此同一 ID 出现两次会让先写入的那份正文被静默丢弃，
> 而检索结果只会少召回、不会报错——这类缺陷必须在加载阶段就暴露。

注意这条校验和 6.6 的"先删后插"是**成对设计**的：单看 upsert 实现你不会觉得有问题，单看这条
校验你不知道为什么要它。这是本项目反复出现的模式——一个实现选择带来的风险，用一条上游校验封住。

### 切片序列的连续性

```python
    @model_validator(mode="after")
    def validate_chunk_sequence(self) -> KnowledgeDocument:
        chunk_ids = [chunk.chunk_id for chunk in self.chunks]
        if len(chunk_ids) != len(set(chunk_ids)):
            raise ValueError(f"document {self.doc_id} contains duplicate chunk IDs")
        for expected, chunk in enumerate(self.chunks):
            if chunk.doc_id != self.doc_id:
                raise ValueError(f"chunk {chunk.chunk_id} does not belong to {self.doc_id}")
            if chunk.ordinal != expected:
                raise ValueError(f"document {self.doc_id} chunk ordinals must be contiguous")
        return self
```

`ordinal` 必须从 0 连续递增。理由不是洁癖：

> 序号连续是"按 ordinal 拼回完整章节"这一读取方式成立的前提，同时也让数据库唯一约束成为
> 真正的最后防线。

`uq_document_chunks_doc_ordinal`（见 6.6）只能保证"同一文档内序号不重复"，保证不了"没有空洞"。
空洞意味着"相邻切片拼回处置流程"会跳步——一份 Runbook 少了第 3 步比完全没有更危险。

## 6.4 确定性切片：为什么不按固定窗口切

`chunking.py` 的模块 docstring 是整章最重要的一段：

> 文档 RAG 的召回质量在很大程度上由切片方式决定：按固定字符窗口盲切会把一条处置步骤劈成两半，
> 让检索命中的片段无法执行。本模块因此先按 Markdown 标题划出语义边界，再在段落边界内贪心装箱到
> 字符上限，并把"文档标题 > 章节 > 小节"的层级写进每个片段，使引用能说明步骤出自哪一节。
>
> 切片完全确定性且不依赖任何模型或第三方解析库：同一份文档任意次导入都得到相同的 `dc_*` 引用，
> 历史报告里的脚注因此不会在重新导入语料后指向别的正文。

两个关键词：**语义边界优先**、**完全确定性**。第二点是引用稳定性的前提，回头看 6.5 的
`make_chunk_id` 就明白——ID 由 `(doc_id, ordinal)` 决定，所以只要切片边界不变，ID 就不变；
一旦引入"用模型帮我切片"，同一份文档两次导入可能得到不同的切片数，所有历史 `dc_*` 引用集体失效。

切片是三层嵌套的循环：

```python
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
```

`ordinal = len(chunks)` 是**跨小节全局递增**的，不是每节从 0 开始——这正是 6.3 那条连续性校验
要求的形态。最后那句 `raise` 也不是防御性代码：

> 正文为空的文档显式失败而不是返回零片段文档，因为一个没有片段的文档在检索侧完全不可见，静默
> 通过只会让语料缺失在评测阶段才暴露。

### 标题栈：一行 `del` 是整个切片器的核心

```python
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
```

`del heading_stack[level - 1:]` 值得手动推演一遍。Markdown 的 `#` 数量就是层级，栈的下标
`level - 1` 正是这个标题应当占据的位置，所以"删掉它自己及其后的一切"再 append，等价于
"替换本级并丢弃所有更深层级"。用 `runbook_flashsync_primary_key_conflict.md` 走一遍：

| 输入行 | `level` | `del` 之后的栈 | append 之后的栈 |
|---|---|---|---|
| `# FlashSync 同步任务主键冲突处置手册` | 1 | `[]` | `["FlashSync…处置手册"]` |
| `## 适用症状` | 2 | `["FlashSync…"]` | `["FlashSync…", "适用症状"]` |
| `## 确认步骤` | 2 | `["FlashSync…"]` | `["FlashSync…", "确认步骤"]` |
| `### 回滚`（假设） | 3 | `["FlashSync…", "确认步骤"]` | `[…, "确认步骤", "回滚"]` |
| `## 根因判定` | 2 | `["FlashSync…"]` | `["FlashSync…", "根因判定"]` |

如果写成 `heading_stack.append(...)` 而不裁剪，第三行之后的路径会变成
`… > 适用症状 > 确认步骤`——一个**根本不存在的祖先关系**。而这个路径会进入每个切片的
`heading_path`，进入 embedding 文本（见 6.5），最终进入报告脚注。**错误的祖先路径不会报错，
只会让引用说谎。**

`flush()` 是嵌套函数，只在遇到新标题和遍历结束时被调用：

```python
    def flush() -> None:
        body = "\n".join(buffer).strip()
        buffer.clear()
        if body:
            sections.append((_heading_path(title, heading_stack), body))
```

`if body:` 这一行让"纯标题行"不产生片段（两个相邻标题之间只有空行时，缓冲 strip 后为空）。
docstring 解释了为什么这不只是省点空间：那种片段"既无法执行也会白占上下文预算"——它会占掉
6.11 里 `max_documents=3` 的一个名额。

### 路径拼接：去重根标题 + 从尾部截断

```python
def _heading_path(title: str, heading_stack: list[str]) -> str:
    segments = list(heading_stack)
    if segments and segments[0].strip() == title.strip():
        del segments[0]
    path = " > ".join([title, *segments])
    if len(path) <= _MAX_HEADING_PATH_CHARS:
        return path
    return path[-_MAX_HEADING_PATH_CHARS:]
```

两个细节都有理由，而且都写在 docstring 里：

- **去掉与文档标题重复的 H1。** manifest 里的 `title` 和 Markdown 的 `# 一级标题` 通常是同一句
  （本项目五份文档全都如此），不去重的话每个 `heading_path` 都以同一句话开头两次。
- **超长时从尾部截断**（`path[-500:]` 而不是 `path[:500]`）："定位一条步骤时最深一级标题比顶层
  文档名更有信息量"。

`_MAX_HEADING_PATH_CHARS = 500` 与 `DocumentChunk.heading_path` 的 `max_length=500`、迁移里
`heading_path` 列的 `String(length=500)` 是同一个数字的三处表达。截断在切片器里做，是为了让
Pydantic 与数据库约束成为"不该被触发的最后防线"而不是常规失败路径。

### 贪心装箱与 `+2`

```python
def _pack_paragraphs(body: str) -> list[str]:
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
```

**贪心而不是等分**，理由是"让绝大多数小节保持单片段完整；只有确实超限时才切开"。本项目五份文档
的每个小节都远小于 1200 字符，所以实际效果是**一小节一片段**，`heading_path` 与内容严格对应。

那个 `+2` 是容易写错的地方：输出用 `"\n\n".join(current)` 拼接，所以每加一个单元就多两个字符的
分隔符。如果只累加 `len(unit)`，一个正好卡在 1200 边界的小节会产出 1202 字符的片段，触发
`DocumentChunk.content` 的 `max_length=MAX_CHUNK_CHARS`——**在导入阶段整批失败**。
`(2 if current else 0)` 保证第一个单元不算分隔符。

### 三级兜底：段落 → 行 → 硬切

```
_pack_paragraphs        按空行切段落，贪心装箱      ← 首选，保住编号步骤完整
  └─ _split_oversized_paragraph   段落超限 → 按行切  ← 保住每一行步骤完整
       └─ _split_oversized_line   单行超限 → 按字符硬切  ← 最后兜底
```

`_split_paragraphs` 用 `re.split(r"\n\s*\n", body)` 按空行切分。docstring 解释了为什么这对
Runbook 特别有效：

> 空行是 Markdown 里最可靠的语义边界，列表项之间通常没有空行，因此整份编号步骤会留在同一段落，
> 装箱阶段也就更倾向于把它整体保留在一个片段内。

也就是说，`## 处置步骤` 下的 1.–4. 四条会被当成**一个段落**，从而尽可能整体进同一个片段。这正是
文档 RAG 想要的：命中"处置步骤"时拿到的是完整流程，不是第 2 步的下半句。

最后一级 `_split_oversized_line` 是纯兜底：

```python
    if len(line) <= MAX_CHUNK_CHARS:
        return [line]
    return [line[index : index + MAX_CHUNK_CHARS] for index in range(0, len(line), MAX_CHUNK_CHARS)]
```

> 这是切片器的最后兜底：任何输入都必须产出长度合法的片段，否则一份文档里的一行超长日志就会让
> 整批导入因数据库长度约束失败，而运维语料里恰好经常粘贴这类长行。

硬切会破坏可读性，但它保证的性质更重要：**切片器对任意输入都终止且输出合法**。缺了这一级，
一条 3000 字符的 SQL 或堆栈粘贴就能让整个语料导入失败，而失败点在数据库约束上——错误信息里
只有列长度，没有"哪份文档哪一行"。

> 补一个可以自己去 grep 验证的事实：`MIN_CHUNK_CHARS = 40` 在 `documents.py` 里声明，但切片器
> 并没有引用它，`_pack_paragraphs` 也没有下限逻辑。所以一个只有一句话的小节会成为独立的短片段。
> 这不影响正确性（`content` 的下限是 `min_length=1`），但读代码时别把这个常量当成生效的约束。

## 6.5 稳定引用与检索文本

两个短函数，但它们决定了"引用能不能长期成立"和"能不能召回"。

```python
def make_chunk_id(doc_id: str, ordinal: int) -> str:
    digest = sha256(f"{doc_id}|{ordinal}".encode()).hexdigest()
    return f"dc_{digest[:16]}"
```

为什么不用 `f"dc_{doc_id}_{ordinal}"`？docstring：

> 使用摘要而不是 `doc_id:ordinal` 拼接，是为了让引用 ID 长度有界且字符集固定（正则可校验），
> 同时保持确定性：同一文档重新导入后引用不变，历史报告里的 `dc_*` 脚注仍然指向同一段正文。

"长度有界且字符集固定"直接支撑了三处正则：`DocumentChunk.chunk_id` 的
`pattern=r"^dc_[a-f0-9]{16}$"`、`BundledDocumentChunk` 的 `evidence_id` 与 `chunk_id` 同一模式。
`doc_id` 最长 100 字符，拼接方案会让引用 ID 长度不可控，也会把 `-` 之类字符带进引用空间。

这里的 `sha256` **不是为了安全，是为了确定性 + 定长**。同一 `(doc_id, ordinal)` 永远得到同一
ID，跨机器、跨 Python 版本都一样（不像 `hash()` 会被 `PYTHONHASHSEED` 影响）。

```python
def document_chunk_text(document: DocumentMetadata, chunk: DocumentChunk) -> str:
    return "\n".join((document.title, chunk.heading_path, chunk.content))
```

这个函数**同时**用于三个地方，这是它必须存在的理由：

1. 导入时生成 embedding（`embed_document_library`，见 6.10）；
2. 检索时给 cross-encoder 重排打分（`_rerank_candidates`，见 6.9）；
3. 因此"入库文本"和"查询时的文档侧文本"逐字符相同。

为什么标题必须参与编码：

> 标题与章节路径必须参与编码：SOP 的关键词经常只出现在小节标题上（"限流阈值调整"），只编码正文
> 会让这类片段在语义通道彻底消失。拼接顺序固定，因此同一片段在入库与查询时得到完全相同的文本。

对照本项目的语料：`sop_lts_parameter_validation.md` 的正文里可能通篇不写"参数校验"四个字，但
`heading_path` 是 `LTS 调度参数校验失败标准处置流程 > …`。只编码正文的话，用户问"参数校验失败
怎么办"在语义通道一条也召不回，只能靠全文通道的 LIKE bonus 救回来——而中文分词在
`websearch_to_tsquery('simple')` 下本来就弱（见 6.7）。

## 6.6 数据库层：两张表、六条 CHECK、一个表达式索引

迁移 `20260716_0008_documents.py`（`revision = "20260716_0008"`，`down_revision = "20260716_0007"`）
建两张表。为什么不与知识节点共表，`document_repository.py` 的模块 docstring 说了：

> 文档域与知识图共用同一个数据库和同一个 embedding Provider 契约，但保持独立表与独立仓储：切片
> 是"一段可执行步骤"，节点是"一个实体"，把两者混在一张表里会让向量空间过滤和评分因子互相污染。

`documents` 表的三条 CheckConstraint 把领域枚举复制到数据库层：

```python
        sa.CheckConstraint(
            "doc_type IN ('runbook','sop','postmortem','faq')",
            name="ck_documents_type",
        ),
        sa.CheckConstraint(
            "reliability >= 0 AND reliability <= 1",
            name="ck_documents_reliability",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(components) = 'array' AND jsonb_array_length(components) >= 1",
            name="ck_documents_components",
        ),
```

这是第 1 章"把边界写成类型"的数据库版本：Pydantic 保护**应用写入路径**，CheckConstraint 保护
**任何写入路径**（psql 手改、迁移脚本、别的进程）。`DocumentType` 枚举的 docstring 也提醒了
同步义务："枚举值与数据库 CheckConstraint 一致，新增类型必须同步迁移。"

`document_chunks` 表最值得看的是那条 embedding 约束——它是 6.3 那条 Pydantic 校验的 SQL 版：

```python
        sa.CheckConstraint(
            "(embedding IS NULL AND embedding_provider IS NULL AND "
            "embedding_dimensions IS NULL) OR "
            "(embedding IS NOT NULL AND embedding_provider IS NOT NULL AND "
            "embedding_dimensions >= 8 AND vector_dims(embedding) = embedding_dimensions)",
            name="ck_document_chunks_embedding_metadata",
        ),
        sa.UniqueConstraint("doc_id", "ordinal", name="uq_document_chunks_doc_ordinal"),
```

`vector_dims(embedding) = embedding_dimensions` 是 pgvector 提供的函数，让"声明维度"和"实际
维度"在数据库层强制一致。注意列类型是 `Vector()` 不带维度参数——本项目允许通过配置切换
embedding 维度（第 2 章），所以维度一致性只能按行校验，不能写进列类型。

### GIN 表达式索引：为什么必须用 `op.execute`

```python
    # 标题路径与正文一起进入同一个 tsvector：SOP 的关键词常只出现在小节标题上，只索引正文会漏召回。
    op.execute(
        "CREATE INDEX ix_document_chunks_search ON document_chunks USING gin "
        "(to_tsvector('simple', coalesce(heading_path, '') || ' ' || coalesce(content, '')))"
    )
```

`upgrade()` 的 docstring 说明了原因："GIN 表达式索引用 `op.execute` 创建，因为 Alembic 的
`create_index` 无法表达 `to_tsvector` 这类函数索引。" `downgrade()` 也必须显式先删它：
"表达式索引不随 drop_index 的默认推断被识别。"

这里有一个**逐字符耦合**，是这一章最容易出线上问题的地方。仓储侧的查询必须写出完全相同的
表达式，否则索引不会被使用：

```python
        # 表达式必须与迁移中的 GIN 索引逐字符一致，否则 PostgreSQL 会退化为顺序扫描。
```

`to_tsvector('simple', coalesce(c.heading_path, '') || ' ' || coalesce(c.content, ''))` —— 词典
名 `'simple'`、`coalesce` 的默认值 `''`、中间那个 `' '`，任何一处不同（哪怕改成
`'english'` 或去掉 `coalesce`）都会让 PostgreSQL 认为这是另一个表达式，索引直接不生效。**它不会
报错，只会变慢**，而在五份文档的演示数据量下你根本察觉不到——直到语料规模上去。

### 先删后插，不是逐条 upsert

```python
        for document in library.documents:
            values = { ... }
            statement = insert(DocumentRecord).values(**values)
            statement = statement.on_conflict_do_update(
                index_elements=[DocumentRecord.doc_id],
                set_={**values, "updated_at": func.now()},
            )
            await self._session.execute(statement)

            # 同一事务内先清空旧切片，使 (doc_id, ordinal) 唯一约束不会与上一版切片边界冲突。
            await self._session.execute(
                delete(DocumentChunkRecord).where(DocumentChunkRecord.doc_id == document.doc_id)
            )
            for chunk in document.chunks:
                await self._session.execute(insert(DocumentChunkRecord).values(...))
```

文档元数据用 `on_conflict_do_update`（幂等 upsert），切片用**整批替换**。这个不对称是刻意的：

> 切片先删后插而不是逐条 upsert：重新切片会改变片段数量与边界，逐条 upsert 会让旧版本的尾部
> 切片以过时正文残留在库里继续被召回，而这种污染在检索结果上完全看不出来。

推演一次：某份文档原来切出 12 个片段，编辑后变成 9 个。逐条 upsert 会更新 `ordinal` 0–8，而
`ordinal` 9–11 三个旧片段**仍在库里、仍有向量、仍会被召回**，内容是上一版的。报告会引用它们，
`dc_*` 校验也通过（ID 稳定），一切看起来正常。

注意这里也没有 `commit()`：

> AsyncSession 由调用方管理事务，写入不自动 commit，因此整库导入可以原子提交，集成测试也能在
> 事务内删数据后回滚做消融对比。

这条所有权边界与第 5 章图仓储完全一致，正因如此 `seed_database()` 才能把"图 + 文档"放进**同一个
事务**（见 6.10）。

### 两个计数方法为什么要分开

```python
    async def count_documents(self) -> tuple[int, int]:
        document_count = await self._session.scalar(
            select(func.count()).select_from(DocumentRecord)
        )
        chunk_count = await self._session.scalar(
            select(func.count()).select_from(DocumentChunkRecord)
        )
        return int(document_count or 0), int(chunk_count or 0)
```

> 两个独立 COUNT 让健康检查能区分"文档没导入"和"文档导入了但切片为空"两种故障。

`count_embedded_chunks` 则带着与 cosine 查询完全一致的过滤条件：

```python
        count = await self._session.scalar(
            select(func.count())
            .select_from(DocumentChunkRecord)
            .where(
                DocumentChunkRecord.embedding.is_not(None),
                DocumentChunkRecord.embedding_provider == provider_id,
                DocumentChunkRecord.embedding_dimensions == dimensions,
            )
        )
```

> 过滤条件与 cosine 查询完全一致，因此该数字不会把旧 Provider 或其他维度的历史记录误报为当前
> 空间可用数据。

这三个数字最终在启动门禁里被比较（见 6.13）：`document_chunks_embedded != document_chunks_loaded`
就拒绝启动。**统计口径与查询口径必须逐字对齐**，否则切换 embedding 模型后计数依然对得上，服务
照常启动，而语义通道实际上一条也召不回——和第 5 章那条结论是同一个道理。

## 6.7 两路召回 SQL

### 全文通道：ts_rank + LIKE bonus

```sql
            WITH ranked AS (
                SELECT
                    c.chunk_id, c.doc_id, c.ordinal, c.heading_path, c.content, c.char_count,
                    d.doc_type, d.title, d.components, d.source_id, d.revision, d.reliability,
                    ts_rank(
                        to_tsvector(
                            'simple',
                            coalesce(c.heading_path, '') || ' ' || coalesce(c.content, '')
                        ),
                        websearch_to_tsquery('simple', :query)
                    ) AS text_rank,
                    CASE
                        WHEN lower(c.heading_path) LIKE lower(:pattern) THEN 0.5
                        WHEN lower(c.content) LIKE lower(:pattern) THEN 0.25
                        ELSE 0
                    END AS lexical_bonus
                FROM document_chunks c
                JOIN documents d ON d.doc_id = c.doc_id
            )
            SELECT *, greatest(text_rank + lexical_bonus, 0.001) AS lexical_score
            FROM ranked
            WHERE text_rank > 0 OR lexical_bonus > 0
            ORDER BY lexical_score DESC, reliability DESC, chunk_id
            LIMIT :limit
```

四个设计点：

1. **`JOIN documents` 在 SQL 里做完**，所以每个命中切片的文档元数据一次取回，不存在 N+1。
2. **LIKE bonus 补中文分词的短板。** docstring："LIKE bonus 补足 `websearch_to_tsquery('simple')`
   对中文和短组件名切分能力不足的部分。" `'simple'` 词典按空白与标点切词，对"主键冲突"这类连写
   中文基本无效，对 `flashsync` 这种短标识符也容易被当成整词。命中标题路径给 0.5，命中正文给
   0.25——标题命中更强，因为标题是人写的语义标签。
3. **`greatest(..., 0.001)`** 保证进了结果集的行分数严格为正。`WHERE` 已经过滤掉两项都为零的行，
   这个下限是给"`text_rank` 极小但非零"的行兜底，避免下游按 0 处理。
4. **`ORDER BY` 以 `chunk_id` 收尾**，与第 5 章一样：分数并列时顺序必须确定，否则同一查询两次执行
   可能返回不同的前 N 条，评测数字随执行计划漂移。

参数化纪律：

```python
        # 查询文本、LIKE 模式和 limit 全部走绑定参数，用户输入不参与 SQL 结构拼接。
        result = await self._session.execute(
            statement,
            {"query": query, "pattern": f"%{query}%", "limit": limit},
        )
```

用户问题**直接来自 HTTP 请求体**，所以这条不是形式主义。`f"%{query}%"` 是在 Python 里构造 LIKE
的**值**，`:pattern` 仍然是绑定参数——这和"把 query 拼进 SQL 字符串"有本质区别。

### 向量通道：过滤在 SQL、裁剪在 Python

```python
        if not query_embedding:
            raise ValueError("query_embedding must not be empty")
        if not all(isinstance(value, int | float) and isfinite(value) for value in query_embedding):
            raise ValueError("query_embedding values must be finite numbers")
        if not any(value != 0 for value in query_embedding):
            raise ValueError("query_embedding must not be an all-zero vector")
        if not 1 <= limit <= 40:
            raise ValueError("limit must be between 1 and 40")
        if not provider_id.strip():
            raise ValueError("provider_id must not be blank")
```

五条前置校验，其中"全零向量"那条最容易被忽略：全零向量的 cosine 距离分母为零，pgvector 返回
NaN，NaN 在 `ORDER BY` 里的位置由实现决定——**查询成功、结果无意义**。

```python
        distance = DocumentChunkRecord.embedding.cosine_distance(query_embedding)
        statement = (
            select(DocumentChunkRecord, DocumentRecord, distance.label("cosine_distance"))
            .join(DocumentRecord, DocumentRecord.doc_id == DocumentChunkRecord.doc_id)
            .where(
                DocumentChunkRecord.embedding.is_not(None),
                DocumentChunkRecord.embedding_provider == provider_id,
                DocumentChunkRecord.embedding_dimensions == len(query_embedding),
            )
            .order_by(
                distance,
                DocumentRecord.reliability.desc(),
                DocumentChunkRecord.chunk_id,
            )
            .limit(limit)
        )
```

`embedding_dimensions == len(query_embedding)` 用的是**查询向量的实际长度**，不是配置里声明的
维度——如果 Provider 返回了错长度向量，这里会自然召回零条而不是拿错维度去比较（服务层还有一条
显式长度校验，见 6.8）。距离计算和排序全在数据库里：

> 距离由数据库计算并转成 `[0, 1]` 相似度，负相关裁剪到零以保持与图侧完全相同的评分契约，不在
> Python 里扫全表。

```python
        for chunk_record, document_record, raw_distance in result:
            # cosine similarity 理论范围是 [-1, 1]，检索分数把负相关裁剪到零以保持评分契约闭合。
            semantic_score = max(0.0, min(1.0, 1.0 - float(raw_distance)))
```

`1.0 - distance` 把 cosine distance 转成相似度，`max(0, min(1, ...))` 把 `[-1, 1]` 夹到 `[0, 1]`。
为什么必须夹：`VectorChunkMatch.semantic_score` 声明了 `ge=0, le=1`，而后面三因子加权也假定每项
在 `[0, 1]`。**负相似度在排序里等价于"不相关"，把它当成 -0.3 参与加权会让权重公式失去可解释性。**

两个转换器 `_chunk_from_mapping` / `_chunk_from_record` 都刻意不带 `embedding`：

> 向量只用于数据库距离计算，Provider 与维度由 `VectorChunkMatch` 单独保留，因此检索结果既不会
> 泄漏派生模型特征，也不会让上下文预算被几千个浮点数挤占。

全文通道那一侧还有一个具体理由："全文查询不选择 embedding 列，避免驱动把未声明类型的 vector
解码成文本。"原生 SQL 走 `text()`，asyncpg 不知道 `vector` 类型，取回来会是字符串——这类问题
在类型转换层报错，很难定位。

## 6.8 三因子评分：为什么不是五因子

第 5 章的 GraphRAG 用五因子（semantic / lexical / reliability / path / freshness）。文档域只用三项：

```python
class DocumentScoringWeights(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    semantic: float = Field(default=0.60, ge=0, le=1)
    lexical: float = Field(default=0.25, ge=0, le=1)
    authority: float = Field(default=0.15, ge=0, le=1)
```

这是本章最值得学的取舍。docstring 讲得很直白：

> 文档域刻意不复用 GraphRAG 的五因子：`path` 对没有关系边的片段没有意义，`freshness` 也无法从
> 静态语料得到诚实取值，硬塞进去只会让权重看起来更复杂而不更准确。

`path` 因子在图侧衡量"这个节点在传播链上的位置"，文档切片没有边，这一项只能恒为常数——**一个
恒定项等于把权重白送给所有候选，不改变任何名次**。`freshness` 更糟：文档表有 `created_at` /
`updated_at`，但那是"什么时候导入的"，不是"这份 Runbook 什么时候被评审的"。用导入时间冒充新鲜度
会让"重新导入一次语料"整体抬高所有文档的分数。

于是 authority 直接取人工声明的 `reliability`：

```python
        # authority 直接取文档声明可靠性：文档域没有边权也没有可信时间戳，人工评审是唯一诚实依据。
        authority_score = source.document.reliability
```

对照 manifest：`runbook_flashsync_primary_key_conflict` 是 0.95，`faq_cross_component_triage`
是 0.7。所以在语义/全文分接近时，Runbook 会压过 FAQ——"一份经过验证的复盘应当压过一条未评审的
FAQ，而这个判断只能来自人工声明而非模型推断"。

权重之和必须精确为 1：

```python
        total = self.semantic + self.lexical + self.authority
        if abs(total - 1.0) > 1e-9:
            raise ValueError("document scoring weights must sum to 1.0")
```

> 与 GraphRAG 权重同样拒绝自动归一化：一个错配的权重被静默修正后，评测报告里的分数区间仍
> 看起来正常，却已经无法与文档基线比较，因此必须在启动阶段显式失败。

用 `1e-9` 容差而不是 `== 1.0`，是因为十进制权重转二进制浮点后求和常有末位误差。默认的
`0.60 + 0.25 + 0.15` 恰好等于 `1.0`，但换一组同样"看起来加得起来"的权重就不一定——可以自己在
REPL 里试：`0.06 + 0.57 + 0.37` 得到的是 `0.9999999999999999`。严格相等会让这类合法配置被拒。
这个校验在 `Settings.__init__` 里被主动调用（第 2 章的 `self.document_scoring_weights()`），
因此配错权重是**启动失败**，不是运行时才发现。

### 合并：并集去重，两路分数都留着

```python
    lexical_by_id = {match.chunk.chunk_id: match for match in lexical_matches}
    vector_by_id = {match.chunk.chunk_id: match for match in vector_matches}
    merged: list[ScoredDocumentChunk] = []
    for chunk_id in lexical_by_id.keys() | vector_by_id.keys():
        lexical = lexical_by_id.get(chunk_id)
        vector = vector_by_id.get(chunk_id)
        # 两路都命中时优先采用向量分支的对象：它与查询在同一 Provider 空间，元数据来源也更完整。
        source = vector if vector is not None else lexical
        if source is None:  # pragma: no cover - 键集合是两个字典的并集，不可能同时缺失。
            raise RuntimeError("merged chunk ID is absent from both retrieval channels")

        lexical_score = bounded_score(lexical.lexical_score if lexical is not None else 0)
        semantic_score = vector.semantic_score if vector is not None else 0.0
        channels: list[RetrievalChannel] = []
        if lexical is not None:
            channels.append(RetrievalChannel.LEXICAL)
        if vector is not None:
            channels.append(RetrievalChannel.VECTOR)
```

**并集不是交集。** 交集会丢掉"只在一路命中"的片段，而这恰恰是混合检索的价值：SOP 的小节标题命中
全文但语义分不高，或者用户用完全不同的说法描述同一现象（语义命中、字面不命中）。单路命中时另一
项按 0 计入，所以混合分自然低于双路命中的片段——**这就是融合，不需要额外的"命中通道数"加分项**。

`bounded_score(lexical.lexical_score ...)` 那一句为什么必须裁剪：全文分是
`greatest(text_rank + lexical_bonus, 0.001)`，`ts_rank` 最大接近 1，再加 0.5 的 bonus 就可能超过 1。
docstring 明说了这一点："全文 ts_rank 与 LIKE bonus 之和可能超过一，因此先裁剪到评分契约范围；
向量分数已由仓储标准化。"

`channels` 保留了"这条片段从哪几路来"，是 `ScoredDocumentChunk` 的可解释性字段之一。加上三层
分数（`hybrid_score` / `rerank_score` / `final_score`）和三个分量（semantic / lexical / authority），
"为什么这段被选中"始终可以逐项复核。

```python
        hybrid_score = bounded_score(
            semantic_score * weights.semantic
            + lexical_score * weights.lexical
            + authority_score * weights.authority
        )
```

```python
    return sorted(
        merged,
        key=lambda match: (-match.hybrid_score, match.chunk.chunk_id),
    )[:limit]
```

排序键 `(-hybrid_score, chunk_id)` —— 又一次以 ID 兜底。`merge_chunk_matches` 只产出一阶段结果：
`final_score = hybrid_score`，`rerank_score` 留空。

### 三层分数的一致性由共享实现保证

```python
    @model_validator(mode="before")
    @classmethod
    def default_final_score(cls, data: object) -> object:
        return default_final_score(data)

    @model_validator(mode="after")
    def validate_rerank_consistency(self) -> ScoredDocumentChunk:
        validate_rerank_consistency(self.hybrid_score, self.rerank_score, self.final_score)
        return self
```

两个 validator 都只是转调 `app.retrieval.scoring` 里的函数。这不是偷懒，是**刻意的单一实现**：

> 复用 GraphRAG 的同一个补齐函数而不是重写一遍，避免两条检索通道对"未重排"给出不同表示，
> 否则同一批断言在文档侧和图侧会得到不一致的默认排序值。

> 文档片段的最终排序直接决定哪几条处置步骤进入报告，因此这条不变量必须与图侧共用一份实现：
> 任何排序改写都要留下可核对的二阶段分数，而不能由某个中间步骤悄悄调权后无从追溯。

规则本身（第 5 章讲过）：**没有 `rerank_score` 时 `final_score` 不允许偏离 `hybrid_score`。**
换句话说，任何名次改写都必须留下"是谁改的"的证据。

## 6.9 服务编排：放大候选、精排、失败降级

```python
    async def retrieve(
        self,
        query: str,
        *,
        chunk_limit: int = 4,
    ) -> DocumentRetrievalResult:
        if not query.strip():
            raise ValueError("query must not be blank")
        if not 1 <= chunk_limit <= 20:
            raise ValueError("chunk_limit must be between 1 and 20")

        query_vectors = await self._embedding_provider.embed_texts([query])
        if len(query_vectors) != 1:
            raise ValueError("embedding provider must return exactly one query vector")
        query_embedding = query_vectors[0]
        if len(query_embedding) != self._embedding_provider.dimensions:
            raise ValueError("query embedding length does not match provider dimensions")
```

> Provider 必须返回恰好一个固定维度向量，否则说明配置维度与真实模型不一致，此时显式失败远比
> 用一个错长度向量去查库更安全。

"用错长度向量查库"的后果是 6.7 那条 `embedding_dimensions == len(query_embedding)` 过滤掉全部行
——**语义通道静默返回空**。显式失败把它变成一条清晰的配置错误。

两路 SQL 是**顺序 await**，不是 `asyncio.gather`：

```python
        candidate_limit = self._candidate_limit(chunk_limit)
        lexical_matches = await self._repository.search_lexical_chunks(
            query,
            limit=candidate_limit,
        )
        vector_matches = await self._repository.search_vector_chunks(
            query_embedding,
            provider_id=self._embedding_provider.provider_id,
            limit=candidate_limit,
        )
```

类 docstring 给了理由："两路 SQL 顺序执行以免并发复用同一 AsyncSession。" SQLAlchemy 的
`AsyncSession` **不是并发安全的**，在同一个 session 上并发发两条查询会得到
`InterfaceError: another operation is in progress`。这一点和第 3 章 MCP 那边形成对照：那里
`execute_tools` 用 `asyncio.gather` 并发，因为每次 `call_tool` 都开**独立的 stdio 子进程**。
**"能不能并发"取决于底层资源是否独立，不取决于操作是否只读。**

### `_candidate_limit`：放大是两阶段检索唯一的收益来源

```python
    def _candidate_limit(self, chunk_limit: int) -> int:
        if self._reranker is None:
            return chunk_limit
        multiplied = chunk_limit * self._rerank_candidate_multiplier
        return max(chunk_limit, min(multiplied, MAX_CHUNK_CANDIDATES, MAX_RERANK_DOCUMENTS))
```

> 放大是两阶段检索唯一的收益来源——精排只能在候选集内部改名次。上限同时受仓储 top-k 契约与
> 重排端点单次文档数约束，避免配置一个大倍数后请求被远程静默截断却无人察觉。

默认 `chunk_limit=4`、倍数 3，所以召回 12 条候选、精排后取前 4。三个上限的来源各不相同：
`MAX_CHUNK_CANDIDATES = 40`（本模块，也是仓储 `limit` 的上限）、`MAX_RERANK_DOCUMENTS = 64`
（reranker 模块的端点契约）、`multiplied`（配置）。`max(chunk_limit, ...)` 兜住"上限比
`chunk_limit` 还小"的极端配置，保证至少召回够用的条数。

**如果不放大**（倍数 1），精排就是在 4 条里重排 4 条——名次可能变，但进入 Prompt 的**集合完全
不变**，等于白付一次 rerank 调用。这也是 `candidate_count` 要进契约的原因：

> `candidate_count` 记录重排前的候选规模，使重排增益在评测里有分母。

### 精排的四条降级路径

```python
        if self._reranker is None or not candidates:
            return candidates, None

        documents = [
            document_chunk_text(candidate.document, candidate.chunk) for candidate in candidates
        ]
        try:
            scores = await self._reranker.rerank(query, documents)
        except RerankerError:
            return candidates, None
        if len(scores) != len(candidates):
            return candidates, None
```

四种情况都返回 `(candidates, None)`：没配重排器、候选为空、`RerankerError`、分数条数不齐。
关键是**第二个返回值为 `None`**：

> 未配置重排、候选为空或分数条数不齐时都原样返回并把模型名留空，使报告不会把一阶段排序说成
> 精排结果；`RerankerError` 同样降级而不抛出，因为文档证据缺失只会降低结论质量，不该中断诊断。

`reranker_model=None` 一路传到输出契约，并且联动 `rerank_blend_weight`：

```python
            rerank_blend_weight=self._rerank_blend_weight if reranker_model else 0,
```

**没跑精排时融合权重必须报 0，不能报配置值。** 否则报告会声称"用了 0.4 的融合权重"，而实际
`final_score` 完全等于 `hybrid_score`——这属于第 14 章反复强调的口径造假。

```python
        reranked = [
            candidate.model_copy(
                update={
                    "rerank_score": bounded_score(score),
                    "final_score": blend_scores(
                        candidate.hybrid_score,
                        bounded_score(score),
                        blend=self._rerank_blend_weight,
                    ),
                }
            )
            for candidate, score in zip(candidates, scores, strict=True)
        ]
        return (
            sorted(reranked, key=lambda match: (-match.final_score, match.chunk.chunk_id)),
            self._reranker.model,
        )
```

`model_copy(update=...)` 而不是原地改字段：Pydantic 模型在这里当值对象用，`model_copy` 会重跑
validator，所以 `validate_rerank_consistency` 对新的三层分数生效。`zip(..., strict=True)` 是
第二道保险（前面已经比过长度），Python 3.10+ 的 `strict` 让长度不等直接抛 `ValueError` 而不是
静默截断。

一个刻意的构造：类 docstring 说"绝不让一个计费外部服务成为诊断链路的可用性依赖"。这条原则在
第 12 章还会再出现一次（`/metrics` 与 trace 的失败不影响 run 终态）。

## 6.10 导入链路：从 Markdown 到带向量的库

```
data/knowledge/documents/manifest.json  ← 结构化元数据（人工评审 + Pydantic 校验）
  + *.md                                 ← 正文（人工可 diff）
      ↓ load_document_library            ← 路径解析 + 逃逸校验 + 确定性切片
   DocumentLibrary（无向量）
      ↓ embed_document_library           ← 整库一次性嵌入
   DocumentLibrary（全部切片带向量 + Provider 溯源）
      ↓ upsert_document_library          ← 与知识图同一事务
   PostgreSQL documents / document_chunks
```

为什么正文单独放 Markdown：

> 正文用 Markdown 单独存放而不是塞进 JSON 字符串，因为 Runbook/SOP 需要人工评审与 diff，转义后的
> 单行 JSON 无法阅读；结构化元数据（类型、组件、可靠性、修订号）则留在 manifest 里由 Pydantic 校验。

manifest 的一条记录长这样（`data/knowledge/documents/manifest.json`，`library_version` 为
`document-seed:v1`，共五份文档）：

```json
    {
      "doc_id": "runbook_flashsync_primary_key_conflict",
      "doc_type": "runbook",
      "title": "FlashSync 同步任务主键冲突处置手册",
      "components": ["flashsync"],
      "source_id": "synthetic_runbook_flashsync_pk_v1",
      "revision": "r3",
      "reliability": 0.95,
      "path": "runbook_flashsync_primary_key_conflict.md"
    },
```

五份文档的可靠性分别是 0.95（FlashSync 主键冲突 Runbook）、0.9（LTS 参数校验 SOP）、0.9（BDS 数据
倾斜 Runbook）、0.85（FlashSync checkpoint 复盘）、0.7（跨组件 FAQ）——这组数字就是 6.8 里
authority 因子的全部来源。

### 路径逃逸校验

```python
    relative_path = entry.get("path")
    if not isinstance(relative_path, str) or not relative_path.strip():
        raise ValueError("document manifest entry requires a path")
    # 解析后校验前缀，阻止 manifest 用 `../` 把仓库外的任意文件当成知识语料读进来。
    markdown_path = (base_dir / relative_path).resolve()
    if not markdown_path.is_relative_to(base_dir.resolve()):
        raise ValueError(f"document path escapes the manifest directory: {relative_path}")
    if not markdown_path.is_file():
        raise FileNotFoundError(f"document file does not exist: {markdown_path}")
```

顺序很关键：**先 `resolve()` 再比前缀**。`resolve()` 会展开 `..` 和符号链接，所以
`../../.env` 会被解析成真实绝对路径，然后 `is_relative_to` 拒绝它。如果反过来先检查字符串里有没有
`..`，`data/knowledge/documents/../../../.env` 这类变体、以及指向仓库外的符号链接都能绕过。

有人会问：manifest 是我自己写的，为什么要防？因为**知识语料是会被"贡献"的资产**。这条校验的成本
是三行，而它封住的是"任意文件读取 → 内容进入向量库 → 通过检索出现在报告里"的完整链路。

加载器刻意不做字段校验：

> 这里只做"取值 + 交给切片器"，字段合法性全部由 `KnowledgeDocument` 的 Pydantic 校验负责，避免
> 在加载器里重复一套弱化的类型检查。

所以 `entry.get("doc_id", "")` 用空字符串兜底——空串会被 `doc_id` 的正则拒绝，错误信息来自
Pydantic（带字段名和实际值），比加载器里手写的 `if not doc_id: raise` 更精确。

### 整库嵌入：ID 索引回填，不依赖嵌套顺序

```python
    flattened = [
        (document, chunk) for document in library.documents for chunk in document.chunks
    ]
    texts = [document_chunk_text(document, chunk) for document, chunk in flattened]
    vectors = await provider.embed_texts(texts)
    if len(vectors) != len(flattened):
        raise ValueError("embedding provider returned a different number of vectors")

    # 按切片 ID 建索引再回填，避免依赖"文档顺序 × 切片顺序"这一隐式假设重新展开一次嵌套循环。
    vectors_by_chunk_id = {
        chunk.chunk_id: vector for (_, chunk), vector in zip(flattened, vectors, strict=True)
    }
```

> 整库一次性嵌入而不是逐份文档，是为了让"要么全部切片进入同一向量空间、要么整体失败"这一原子
> 语义与单次数据库事务对齐；返回向量数量与切片总数不符即视为 Provider 契约漂移并立即中止，
> 因为错位的向量会让每个切片静默拿到别人的语义坐标，而检索结果看不出任何异常。

"错位的向量"是这一章第四次出现的同一类风险（前三次：重复 doc_id、残留尾部切片、伪造占位正文），
它们的共同特征是：**不报错、不崩溃、只是结论变差**。这类缺陷只能靠结构约束封住，靠测试碰不到。

回填时按 `chunk_id` 建字典再取，而不是第二次展开 `for document: for chunk:` 嵌套循环。两种写法
在当前代码下等价，但字典写法**不依赖两次遍历顺序一致**这个隐式假设。

### 单事务：拒绝"图已就绪但文档缺失"

```python
    # 在连接数据库前完成文件与图引用校验，让坏种子以更清晰、低成本的错误提前失败。
    bundle = load_knowledge_seed(settings.knowledge_seed_file)
    library = load_document_library(settings.document_manifest_file)
    ...
    embedded_bundle = await embed_knowledge_bundle(bundle, embedding_provider)
    embedded_library = await embed_document_library(library, embedding_provider)
    # 远程 Provider 持有 httpx 连接池；种子是短进程，向量生成完成后立刻释放而不是等解释器退出。
    if hasattr(embedding_provider, "aclose"):
        await embedding_provider.aclose()
```

```python
        async with factory() as session:
            repository = PostgresGraphRepository(session)
            document_repository = PostgresDocumentRepository(session)

            # 节点必须先于边写入，整个 Bundle 只提交一次以保证图结构原子可见。
            await repository.upsert_seed_bundle(embedded_bundle)
            await document_repository.upsert_document_library(embedded_library)
            await session.commit()

            # 提交后计数验证数据库实际状态，而不是简单回报输入文件中的元素数量。
            node_count, edge_count = await repository.count_graph()
            document_count, chunk_count = await document_repository.count_documents()
            return node_count, edge_count, document_count, chunk_count
```

`seed.py` 模块 docstring 说明了为什么两者必须同事务：

> 知识图与文档语料在同一个事务里提交，避免出现"图已就绪但文档缺失"的中间状态——那种状态下检索
> 仍会返回结果，只是永远少了一条通道，而这在演示时几乎不可能被发现。

三个顺序上的讲究，每个都对应一类真实故障：

1. **文件校验在连库之前**——坏 manifest 的错误信息不必等到数据库连接超时之后。
2. **向量生成在开事务之前**——远程 embedding 可能耗时几秒到几十秒，不该占着数据库事务。
3. **计数在 commit 之后、从数据库读**——"回报输入文件里的元素数"会把"写入被约束静默拒绝"报成成功。

这三个计数最终成为 `/health` 的 `documents_loaded` / `document_chunks_loaded` /
`document_chunks_embedded` 三个字段，也是 6.13 那条启动门禁的输入。

## 6.11 证据预算：谁进得去 Prompt

到这里两条通道都能返回排好序的结果了。但**检索结果不能直接进 Prompt**——第 5 章的图检索一次可能
返回 5 个种子 + 若干条 1–2 跳路径，文档通道再来 4 个切片，全部序列化可能几万字节。上下文是有限、
计费的共享资源，必须裁剪。

关键问题是：**谁来裁剪？** 一个常见做法是让模型自己摘要（"先让 LLM 压缩证据，再喂给 Planner"）。
本项目明确拒绝，`budget.py` 的模块 docstring 第一句就是：

> 预算选择是确定性基础设施，不交给 LLM 自行摘要或删除证据。

理由不难理解：如果证据集合是模型删出来的，那么"报告为什么漏了这条根因"就有了两种可能——模型推理
不行，或者证据在摘要阶段就被删了。**这两种情况无法区分，整个评测体系失效。**

### 四重预算

```python
class EvidenceBundleBudget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_bytes: int = Field(default=6000, ge=256, le=100_000)
    max_nodes: int = Field(default=8, ge=1, le=50)
    max_paths: int = Field(default=4, ge=0, le=20)
    max_documents: int = Field(default=3, ge=0, le=20)
```

为什么字节和条数都要限，docstring：

> 字节预算使用模型无关且可精确重放的 UTF-8 JSON 长度，节点/路径/切片上限防止大量短记录绕过字节
> 控制。该模型不可变并限制合理范围，调用方不能用零预算制造看似成功但完全无证据的 Bundle。

- **只限字节**：50 个极短节点可能只有 3000 字节，但 Planner 要在 50 条事实里做推理，注意力被摊薄。
- **只限条数**：8 个各含 4000 字符 `content` 的节点能撑到三万多字节。
- `ge=256` 的下限 + `max_nodes` 的 `ge=1`：不允许配一个"技术上成功但没有证据"的 Bundle。

`max_documents` 为什么单独计数、不并入 `max_nodes`：

> `max_documents` 与节点预算分开计数，因为文档切片正文通常远长于知识节点，共用一个上限会让
> 几条 Runbook 片段挤掉全部图证据，反而削弱"关系可解释"这一核心能力。

切片正文上限 1200 字符，知识节点 `content` 虽然上限 4000 但实际种子里普遍是一两句话。共用上限的
话，三条 Runbook 片段就能吃掉 8 个名额里的大半——**而图路径是本系统区别于普通 RAG 的地方**。

### 三段选择：路径优先、种子补充、文档最后

```python
    node_scores = _collect_node_scores(result)
    node_candidates = _collect_node_candidates(result, node_scores=node_scores)
    selected_nodes: dict[str, BundledKnowledgeNode] = {}
    selected_paths: list[BundledGraphPath] = []

    # 路径顺序沿用检索服务的最终混合分排序；每条路径必须连同全部节点原子进入上下文。
    for path in result.paths:
        if len(selected_paths) >= budget.max_paths:
            continue
        path_item = _bundle_path(path)
        path_nodes = {
            node.node_id: node_candidates[node.node_id]
            for node in path.nodes
            if node.node_id not in selected_nodes
        }
        proposed_nodes = [*selected_nodes.values(), *path_nodes.values()]
        proposed_paths = [*selected_paths, path_item]
        if len(proposed_nodes) > budget.max_nodes:
            continue
        if _payload_size(proposed_nodes, proposed_paths, []) > budget.max_bytes:
            continue
        selected_nodes.update(path_nodes)
        selected_paths.append(path_item)
```

这段代码的每个细节都值得解释。

**（1）原子性。** 一条路径和它**尚未被选中的全部节点**是一个候选，一起进或一起不进。为什么必须
这样：`BundledGraphPath` 只存 `node_ids`（见 6.3 表格上方的模型定义），正文在
`BundledKnowledgeNode` 里。如果路径进了、某个节点没进，Planner 看到的是一条**引用了不存在节点的
路径**——它无法判断那个节点是什么，却看得到它在传播链上。docstring 的说法是"保证 Planner 看不到
断裂路径"。

**（2）`continue` 而不是 `break`。** 三处超预算判断全都是 `continue`：跳过这一条，继续试下一条。
这是"尽力填满"策略——第 2 条路径太大装不下，但第 3 条可能刚好能装。用 `break` 会让一条巨大的
高分路径把后面所有路径全部挡住。

**（3）字节判断带上"提议后的全集"。** `_payload_size(proposed_nodes, proposed_paths, [])` 算的是
"如果把这条路径加进去，总量会是多少"，不是"这条路径本身多大"。这是唯一正确的算法：字节预算约束的
是最终载荷，而节点去重意味着新增路径的边际成本取决于已选内容。

**（4）第三个参数是 `[]`。** 图阶段还没有文档，此时字节预算全部可用于图证据。文档在第三段里才
与已选图证据一起计费。

```python
    # 路径节点完成去重后，再按种子排名补充孤立但高相关的知识证据。
    for seed in result.seeds:
        if seed.node.node_id in selected_nodes:
            continue
        if len(selected_nodes) >= budget.max_nodes:
            break
        candidate = node_candidates[seed.node.node_id]
        proposed_nodes = [*selected_nodes.values(), candidate]
        if _payload_size(proposed_nodes, selected_paths, []) > budget.max_bytes:
            continue
        selected_nodes[seed.node.node_id] = candidate
```

第二段补充**没出现在任何已选路径里**的高分种子。注意这里 `max_nodes` 用的是 `break`（节点数满了
就不可能再加任何节点，继续循环无意义），而字节仍用 `continue`（下一个种子可能更短）。**同一个
循环里两种控制流，各有各的理由——这不是笔误。**

```python
    chunk_candidates = list(documents.chunks) if documents is not None else []
    selected_documents: list[BundledDocumentChunk] = []
    for chunk in chunk_candidates:
        if len(selected_documents) >= budget.max_documents:
            break
        chunk_item = _bundle_chunk(chunk)
        proposed_documents = [*selected_documents, chunk_item]
        if (
            _payload_size(list(selected_nodes.values()), selected_paths, proposed_documents)
            > budget.max_bytes
        ):
            continue
        selected_documents.append(chunk_item)
```

第三段加文档切片，与已选图证据**合并计费**。为什么文档排最后：

> 文档切片排在图证据之后，是因为图路径是本系统区别于普通 RAG 的可解释部分：预算紧张时应先保住
> "故障如何沿依赖传播"，再补"处置步骤写在哪一节"，而不是让几段长文档把关系证据整体挤出上下文。

这是一个**产品判断写进了代码顺序**：本项目的差异化能力是关系可解释性，所以预算紧张时先牺牲处置
步骤。换一个产品（比如纯 Runbook 助手）这个顺序应该反过来。

`documents is None` 与 `documents.chunks == []` 在 Bundle 里同形，这一点在 docstring 里明确声明了：

> `documents` 为空表示本次没有文档通道，与"文档通道未召回"在 Bundle 里同形，差异由检索事件而不是
> 证据主体表达。

也就是说，"没启用文档通道"和"启用了但没召回"的区别要去 trace span（6.13）里看，不在证据主体里
编码。这样做的好处是 Prompt 结构不随部署配置变化。

### 字节怎么算：规范 JSON

```python
def _payload_size(
    nodes: list[BundledKnowledgeNode],
    paths: list[BundledGraphPath],
    documents: list[BundledDocumentChunk],
) -> int:
    payload = {
        "selected_nodes": [node.model_dump(mode="json") for node in nodes],
        "selected_paths": [path.model_dump(mode="json") for path in paths],
        "selected_documents": [chunk.model_dump(mode="json") for chunk in documents],
    }
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return len(serialized.encode("utf-8"))
```

四个参数每个都有作用，缺一个数字就不可重放：

| 参数 | 作用 | 缺了会怎样 |
|---|---|---|
| `ensure_ascii=False` | 中文按真实 UTF-8 计费 | 每个汉字变成 `\uXXXX` 六字节，预算凭空缩水 |
| `sort_keys=True` | 键顺序固定 | 同一批证据在不同 Python 版本/字段顺序下得到不同字节数 |
| `separators=(",", ":")` | 去掉默认的空格 | 每个键值对多两字节，量大时几百字节 |
| `.encode("utf-8")` | 按字节而非字符计 | 中文字符数 ≠ 字节数，一个汉字 3 字节 |

docstring 明确了预算的边界："预算只覆盖将注入 Prompt 的主体，不包含 omitted 诊断元数据或 Pydantic
字段描述。"以及三类合并计费的理由：

> 三类证据合并计费而不是各自独立，因为模型上下文是一个共享资源，分开计费会让总量在最坏情况下翻
> 三倍。

**注意这个数字不是 token 数，也不试图是。** 用 UTF-8 JSON 字节的理由是"模型无关且可精确重放"：
换模型不用改预算，本地断言不需要 tokenizer，跨平台结果一致。它只是 token 数的一个稳定代理。

### 优先分：为什么用 `final_score` 而不是 `hybrid_score`

```python
def _collect_node_scores(result: GraphRetrievalResult) -> dict[str, float]:
    scores = {seed.node.node_id: seed.final_score for seed in result.seeds}
    for path in result.paths:
        for node in path.nodes:
            scores[node.node_id] = max(scores.get(node.node_id, 0.0), path.final_score)
    return scores
```

> 优先分统一使用 `final_score`：它在只跑一阶段时等于 `hybrid_score`，启用 cross-encoder 后又能
> 让精排结论真正影响进入上下文的证据，而不是只改变返回列表的显示顺序。

这句话点出了一个容易被忽略的问题：**如果预算选择用 `hybrid_score`，那么精排就只改变了"返回列表
的显示顺序"，进入 Prompt 的证据集合完全不变**——花钱买了个排序，模型看到的东西一模一样。

两个细节：

- **非种子路径节点继承包含它的最高路径分**（`max(...)`）。一个节点出现在三条路径里，取最高的那条。
- **取 `max` 而不是累加。** docstring："既保证稳定排序信息，又不把多次出现机械累加成更强事实。"
  累加会让"在多条路径上出现"变成一种证据强度——但那只反映图的拓扑密度，不反映事实可信度。

最后一句话是整个函数的定位声明："该分数只用于上下文选择，不是根因置信度。" 检索分绝不能被当成
"这个根因有多可能"，这也是 `_bundle_chunk` 只留一个分数的理由（下面）。

### 三个 `_bundle_*` 转换器：只丢东西，不加东西

```python
def _bundle_node(node: KnowledgeNode, *, retrieval_score: float) -> BundledKnowledgeNode:
    return BundledKnowledgeNode(
        evidence_id=f"{KNOWLEDGE_EVIDENCE_ID_PREFIX}{node.node_id}",
        node_id=node.node_id,
        node_type=node.node_type,
        name=node.name,
        content=node.content,
        source_id=node.source_id,
        source_span=node.source_span,
        reliability=node.reliability,
        remediation_risk_level=node.remediation_risk_level,
        retrieval_score=retrieval_score,
    )
```

丢掉的是 `embedding`、Provider 元数据、`aliases`："只服务检索，不应消耗 Prompt 预算或被模型当作
额外事实。"留下的每一个字段都有下游消费者——`source_span` 给 Auditor 核对，`reliability` 给报告
表达置信度，而 `remediation_risk_level` 有一条专门的强调：

> 方案类节点的风险等级必须一起进入 Bundle：报告层的修复建议风险只能来自这条人工声明，丢掉它就
> 等于让所有方案退回同一个硬编码等级。

`BundledKnowledgeNode` 自己还会**再校验一次**风险声明范围：

```python
    @model_validator(mode="after")
    def validate_remediation_risk_scope(self) -> BundledKnowledgeNode:
        validate_remediation_risk_declaration(self.node_type, self.remediation_risk_level)
        return self
```

> Bundle 是报告层实际读取的对象，如果只在种子侧校验，任何绕过种子直接构造 Bundle 的路径
> （测试替身、缓存反序列化）都能让方案节点失去风险声明，报告风险等级随即退回默认值。

**"在实际被读取的那个对象上校验"** 是一条通用原则：种子模型是入口，Bundle 是出口，出口的约束不能
假设入口一定被走过。测试替身尤其危险——它们通常直接构造出口对象。

```python
def _bundle_chunk(chunk: ScoredDocumentChunk) -> BundledDocumentChunk:
    return BundledDocumentChunk(
        evidence_id=chunk.chunk.chunk_id,
        chunk_id=chunk.chunk.chunk_id,
        ...
        retrieval_score=chunk.final_score,
    )
```

> 只保留 `final_score` 作为 `retrieval_score`：语义/全文/权威度分量是检索可解释性所需，注入
> Prompt 却只会让模型把内部排序数字当成事实强度。

这是一条很清晰的分层：**可解释性字段留在检索层供人和评测查看，进 Prompt 的只留一个数**。
`_bundle_path` 同理丢掉节点正文（由 `BundledKnowledgeNode` 去重保存），保留有序 `node_ids` /
`edge_ids` / `relation_types` / `edge_source_spans`。

注意三个转换器里 `evidence_id` 的构造方式：节点是 `kn_` + `node_id`，路径**直接用 `path_id`**，
切片**直接用 `chunk_id`**。后两者本身已带前缀（`path_`、`dc_`），所以是同值双字段——冗余是为了让
"引用空间"和"数据库主键"在类型上分开，但值上可互查。

### 那个单列的常量

```python
# 知识节点引用前缀单列成常量，因为它已经不只是显示格式：`kn_<node_id>` 让一条报告引用精确编码知识图
# 节点 ID，评测侧因此可以离线反解出"报告引用了哪个 root_cause 节点"。两处各写一份字面量会在改前缀的
# 那天让反解静默失配，指标退化成恒为 0 而不是报错。
KNOWLEDGE_EVIDENCE_ID_PREFIX = "kn_"
```

这条注释是"唯一定义 + 多处比对"原则的一个微型案例。评测侧（第 14 章的根因锚点命中率）要做的事是：
拿到报告的 `evidence_refs`，剥掉 `kn_` 前缀，反查这是哪个知识节点，判断它是不是期望的 root_cause。
构造方和反解方各写一份 `"kn_"` 字面量，改前缀那天**反解全部失配，指标变成恒 0**——而 0 看起来像
"模型找不到根因"，不像"代码坏了"。

## 6.12 被裁掉的证据必须留痕

```python
    selected_node_ids = set(selected_nodes)
    selected_path_ids = {path.path_id for path in selected_paths}
    selected_chunk_ids = {chunk.chunk_id for chunk in selected_documents}
    all_node_ids = set(node_candidates)
    all_path_ids = {path.path_id for path in result.paths}
    all_chunk_ids = {chunk.chunk.chunk_id for chunk in chunk_candidates}
    omitted_node_ids = sorted(all_node_ids - selected_node_ids)
    omitted_path_ids = sorted(all_path_ids - selected_path_ids)
    omitted_chunk_ids = sorted(all_chunk_ids - selected_chunk_ids)
    used_bytes = _payload_size(list(selected_nodes.values()), selected_paths, selected_documents)

    return GraphEvidenceBundle(
        query=result.query,
        retrieval_mode=result.mode,
        budget=budget,
        used_bytes=used_bytes,
        selected_nodes=list(selected_nodes.values()),
        selected_paths=selected_paths,
        selected_documents=selected_documents,
        omitted_node_ids=omitted_node_ids,
        omitted_path_ids=omitted_path_ids,
        omitted_chunk_ids=omitted_chunk_ids,
        truncated=bool(omitted_node_ids or omitted_path_ids or omitted_chunk_ids),
    )
```

三对集合运算，得到的是"检索到但没进上下文"的稳定 ID。函数 docstring 声明了一条不变量：

> 所有候选 ID 最终分成 selected 或 omitted，两边不重叠。

`truncated` 是给模型看的：

> `truncated` 明确提示 Planner 证据集合并非全量，防止其把预算裁剪误解为知识库不存在其他候选。

这个区别在推理上是实质性的。"知识库里没有 BDS 的相关节点"支持"排除 BDS"这个结论；"有但因预算被
裁掉了"不支持。**如果不告诉模型证据被裁过，它会把上下文的边界当成世界的边界。**

`omitted_*_ids` 排序输出（`sorted`）是为了可重放：同一次检索两次执行得到逐字节相同的 Bundle。
`used_bytes` 只算三个 selected 列表，所以 `omitted_*` 再长也不占预算——这就是 `_payload_size`
docstring 里"不包含 omitted 诊断元数据"的含义。

还有一句容易被跳过的声明："空检索结果合法返回只含规范空列表包装的最小主体，输入结果本身不会被
修改。" 空召回**不是错误**——第 9 章的报告层会据此声明不确定性。而"输入不被修改"意味着
`build_evidence_bundle` 是纯函数：同一个 `GraphRetrievalResult` 可以用不同预算反复裁剪（消融实验
正需要这个）。

## 6.13 两条通道怎么接进 runtime

```python
        documents: DocumentRetrievalResult | None = None
        async with self._session_factory() as session:
            service = GraphRetrievalService(...)
            with trace_span(
                TraceSpanKind.RETRIEVAL,
                "retrieval.graph_channel",
                seed_limit=self._seed_limit,
                max_hops=self._max_hops,
            ) as span:
                result = await service.retrieve(...)
                span.annotate(
                    retrieval_mode=result.mode.value,
                    candidate_count=result.candidate_count,
                    seed_count=len(result.seeds),
                    path_count=len(result.paths),
                    reranker_model=result.reranker_model or "none",
                )
            if self._document_score_weights is not None:
                document_service = DocumentRetrievalService(...)
                # 两条通道分别开 span：文档 RAG 与图召回共享 embedding/精排服务，只有分开计时才能
                # 回答"哪条通道值得继续投入预算"，合并成一个 retrieval span 会永久掩盖这个问题。
                with trace_span(
                    TraceSpanKind.RETRIEVAL,
                    "retrieval.document_channel",
                    chunk_limit=self._document_chunk_limit,
                ) as span:
                    documents = await document_service.retrieve(...)
                    span.annotate(
                        candidate_count=documents.candidate_count,
                        chunk_count=len(documents.chunks),
                        reranker_model=documents.reranker_model or "none",
                    )
        return build_evidence_bundle(result, budget=self._budget, documents=documents)
```

四个装配细节：

**（1）同一个 session、顺序执行。** 与 6.9 同一个理由（`AsyncSession` 非并发安全），而且
"文档检索排在图检索之后但共享同一次会话与同一份查询文本，使两条通道的召回可在同一次 run 事件里
逐项对照"。同一份 `retrieval_query` 是可比性的前提。

**（2）查询截断在检索侧。**

```python
        # AgentState 保留最多 4000 字符原问题；GraphRAG v3 查询契约上限为 2000，检索侧显式截断。
        retrieval_query = normalized_query[:2000]
```

`AgentState` 允许 4000 字符，检索契约上限 2000（`DocumentRetrievalResult.query` 也是
`max_length=2000`）。**两个不同上限之间必须有人显式转换**，否则一个 3000 字符的问题会在检索层
抛 Pydantic 校验错——而用户看到的是"诊断失败"，不是"问题太长"。

**（3）两个 span 分开。** 注释已经说明了：合并计时就永远无法回答"哪条通道值得继续投入"。这是
第 12 章 `run-trace:v1` 的设计目的之一——trace 不是为了好看，是为了回答具体的工程问题。注意
`reranker_model or "none"` 那个兜底：span 属性值被限制为 ASCII 标识符，`None` 不是合法属性值。

**（4）`document_score_weights is None` 表示未启用文档通道。**

> `document_score_weights` 为 None 表示本进程未启用文档通道（例如尚未导入语料），此时 Bundle
> 里不会出现 `selected_documents`，而不是返回一批空壳切片让报告以为有出处可引。

### 启动门禁：所有切片必须都有向量

装配来自 `app/api/main.py` 的 lifespan（第 2 章讲过整套门禁的形状）：

```python
                document_repository = PostgresDocumentRepository(session)
                documents_loaded, document_chunks_loaded = (
                    await document_repository.count_documents()
                )
                document_chunks_embedded = await document_repository.count_embedded_chunks(...)
                if document_chunks_embedded != document_chunks_loaded:
                    raise ...
                        "all document chunks must be embedded in the configured provider space"
```

**部分嵌入被视为启动失败，不是降级。** 想清楚为什么：如果 100 个切片里只有 60 个有向量，语义通道
只能召回那 60 个，而**检索结果看不出任何异常**——分数正常、排序正常、`candidate_count` 正常。这
又是本章那类"不报错只是结论变差"的缺陷，只能靠启动门禁封住。

契约 ID 也在这里比对：

```python
    if settings.document_retrieval_contract_id != DOCUMENT_RETRIEVAL_CONTRACT_ID:
        raise ValueError("configured document retrieval contract ID does not match the package")
```

`settings.document_retrieval_contract_id`（默认 `"document-retrieval:v1"`）与
`app/retrieval/documents.py` 的模块常量必须一致——第 2 章那条"唯一定义 + 多处比对"规则的又一处实例。
升版本要同步改 settings 默认值、模块常量、`docs/prompt-contracts.md`、`docs/implementation-guide.md`
和 `tests/unit/test_documentation_policy.py` 里的字面量断言。

配置侧的六个旋钮（`app/core/settings.py`）：

| 配置项 | 默认值 | 作用 |
|---|---|---|
| `document_semantic_weight` | 0.60 | 三因子之一 |
| `document_lexical_weight` | 0.25 | 三因子之一 |
| `document_authority_weight` | 0.15 | 三因子之一，取文档 `reliability` |
| `document_retrieval_chunk_limit` | 4 | 一次检索返回几个切片 |
| `retrieval_context_max_documents` | 3 | Bundle 里最多进几个切片 |
| `document_manifest_file` | `data/knowledge/documents/manifest.json` | 语料入口 |

注意 `chunk_limit=4` 与 `max_documents=3` **不相等**：检索返回最多 4 条，预算最多放 3 条。因此只要
文档通道召回满 4 条，`omitted_chunk_ids` 就非空、`truncated` 就是 `True`。这个差值的直接效果是让
"证据被裁剪过"成为常态可见的信号，而不是一个只在极端情况下才出现、因此从未被真正走过的分支。

## 6.14 本章小结

| 设计选择 | 实现 | 拒绝的替代方案 |
|---|---|---|
| 处置步骤走独立通道 | `documents` / `document_chunks` 两张表 + 独立仓储 | 把 Runbook 塞进知识节点 `content` |
| 两条通道共享一个引用空间 | `kn_*` / `path_*` / `dc_*` 同进一个 `GraphEvidenceBundle` | 两个并行证据对象各自喂给不同 Agent |
| 切片沿语义边界 | 标题栈 + 段落贪心装箱 + 三级兜底 | 固定字符窗口滑动切片 |
| 标题层级进入引用与向量 | `heading_path` + `document_chunk_text` 三处共用 | 只编码正文 |
| 引用长期稳定 | `sha256(f"{doc_id}\|{ordinal}")[:16]` 定长确定性 ID | 自增主键或 `doc_id:ordinal` 拼接 |
| 切片完全确定性 | 纯正则 + 字符串处理，无模型无第三方解析器 | 让 LLM 帮忙切片 |
| 重新导入不留残渣 | 先 `DELETE` 全部切片再整批插入 | 逐条 upsert |
| 表达式索引必须被命中 | 查询与迁移里的 `to_tsvector(...)` 逐字符一致 | 各写一份"差不多"的表达式 |
| 中文分词短板显式补偿 | 标题 LIKE +0.5 / 正文 LIKE +0.25 | 只依赖 `websearch_to_tsquery('simple')` |
| 评分因子只留诚实的 | 三因子（语义/全文/权威），拒绝 path 与 freshness | 复用图侧五因子让公式"看起来更强" |
| 权威度来自人工 | `reliability` 由 manifest 声明并进 CheckConstraint | 用导入时间冒充新鲜度 |
| 权重错误必须暴露 | 总和 ≠ 1 直接失败（`1e-9` 容差） | 隐式归一化 |
| 融合口径两侧同源 | `blend_scores` / `bounded_score` / 两个 validator 共享实现 | 文档侧复制一份评分逻辑 |
| 精排是增强不是依赖 | 四条降级路径，`reranker_model=None` 且融合权重报 0 | 失败即中断诊断 |
| 精排必须影响进入 Prompt 的集合 | 预算优先分用 `final_score` | 用 `hybrid_score`，精排只改显示顺序 |
| 上下文裁剪是确定性基础设施 | `build_evidence_bundle` 纯函数、无模型参与 | 让 LLM 先摘要证据 |
| 路径不可断裂 | 路径 + 其全部未选节点为原子候选 | 按分数逐个节点填预算 |
| 尽力填满而不是遇阻即停 | 超预算 `continue`（节点数满才 `break`） | 一律 `break` |
| 字节预算可精确重放 | `sort_keys` + 紧凑分隔符 + `ensure_ascii=False` + UTF-8 字节 | 估算 token 数 |
| 三类证据合并计费 | `_payload_size(nodes, paths, documents)` 一次序列化 | 三类各自独立计费 |
| 图证据优先于文档 | 文档在第三段用剩余字节 | 按分数混排两类证据 |
| 裁剪必须留痕 | `omitted_*_ids` + `truncated` | 静默丢弃低分证据 |
| 出口对象自带校验 | `BundledKnowledgeNode` 重跑风险声明规则 | 只在种子入口校验 |
| 前缀只有一份定义 | `KNOWLEDGE_EVIDENCE_ID_PREFIX` | 构造侧与评测反解侧各写字面量 |
| 部分嵌入即启动失败 | `document_chunks_embedded != document_chunks_loaded` 拒绝启动 | 降级为"语义通道少召回一些" |
| 语料导入原子 | 图 + 文档同一事务提交，提交后从库重新计数 | 分两次提交、回报输入文件计数 |
| 语料不能引用仓库外文件 | `resolve()` 后 `is_relative_to` 前缀校验 | 检查字符串里有没有 `..` |

三句话概括这一章：

1. **文档 RAG 不是"再加一个向量库"。** 它要解决的是"处置步骤从哪来"，而它必须付出的代价是：切片
   要沿语义边界、引用要长期稳定、评分因子只能留下诚实的那几项、并且和图证据挤同一份上下文预算。
2. **这一章反复出现同一类缺陷：不报错、不崩溃、只是结论变差。** 重复 doc_id 静默覆盖正文、逐条
   upsert 留下过时尾部切片、错位向量让切片拿到别人的语义坐标、部分嵌入让语义通道少召回、表达式
   不一致让索引失效、`kn_` 前缀改动让评测指标恒为 0。测试很难碰到它们，所以每一条都被一个**结构性
   约束**封住：库级校验、先删后插、按 ID 回填、启动门禁、逐字符一致的注释提醒、单列的常量。
3. **上下文预算是确定性基础设施，这一点不能让步。** 一旦证据集合是模型自己删出来的，"报告为什么
   漏了这条根因"就永远无法归因，第 14 章那五层消融评测全部失去意义。

下一章进入 Agent 层：`app/agents/` 里的 Planner 与 Auditor 如何用 Structured Outputs 把"模型输出"
约束成可校验对象、Prompt 里到底注入了什么（以及刻意不注入什么）、以及模型返回不合法结构时系统
如何在不猜测的前提下失败。

