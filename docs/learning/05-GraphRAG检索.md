# 第 5 章 GraphRAG：向量 + 全文 + 图扩展 + 精排四段流水线

## 5.1 你会验证什么

```bash
.venv/Scripts/python -m pytest -q tests/unit/test_knowledge_seed.py tests/unit/test_embeddings.py \
  tests/unit/test_reranker.py tests/unit/test_retrieval_service.py tests/unit/test_graph_ablation.py

# 真实 SQL（递归 CTE、pgvector、全文排名）只能在数据库里验证
DATAOPS_TEST_DATABASE_URL='postgresql+asyncpg://...' \
  .venv/Scripts/python -m pytest -m postgres tests/integration/test_graph_postgres.py
```

这一章读 `app/retrieval/` 的核心六个文件（`models.py` 526 行、`embeddings.py` 457 行、
`repository.py` 436 行、`service.py` 332 行、`reranker.py` 268 行、`scoring.py` 73 行）。文档
检索（`documents.py`、`chunking.py`、`document_*.py`）和证据预算（`budget.py`）留到第 6 章，
因为它们复用本章建立的全部契约。

单元测试可以用测试替身跑完融合与排序逻辑，但**递归 CTE 的防环、`<=>` 运算符、`ts_rank` 的实际
排名都只有真库能验证**——这也是本项目把 `-m postgres` 单独标记而不是默认跳过的原因。

## 5.2 为什么不是"向量库 + top-k"

最常见的 RAG 实现是：文本切片 → embedding → 向量库 → 查询时取 top-k → 拼进 Prompt。这套做法
在本项目会立刻撞上三个问题：

1. **排障需要关系，而向量不表示关系。** "LTS 任务卡住"和"上游 BDS 分区未就绪"在语义上并不相似，
   但它们之间有一条 `DEPENDS_ON` 边。纯向量检索永远召回不到这条链，而这条链恰好是根因。
2. **引用必须可核对。** 向量检索只能给出"这段文本最相似"，没有稳定标识；报告写"根据知识库"
   等于没写。本项目要求每条根因和链路边都带 `evidence_id` 或 `path_id`，这要求检索结果本身
   是**带 ID 的结构**，不是一段拼接文本。
3. **删掉一条边，结论必须变。** 第 14 章的 GraphRAG 消融测试直接在事务里删边然后回滚，断言
   报告的传播链随之改变。只有显式图结构才能这样验证；向量空间里"删掉一条关系"没有对应操作。

所以本项目的检索是四段确定性流水线：

```
查询文本
  ├─ 通道 A：PostgreSQL 全文（ts_rank + 名称/别名 LIKE bonus）
  └─ 通道 B：pgvector cosine（embedding provider 生成查询向量）
        ↓ merge_seed_matches：按 node_id 并集去重，算五项加权 hybrid_score
        ↓ cross-encoder 重排（可选，失败降级为不重排）
        ↓ 截断到 seed_limit
        ↓ 每个种子 WITH RECURSIVE 扩展 1–2 跳，得到带 path_id 的完整路径
     GraphRetrievalResult(seeds=[...], paths=[...])
```

注意流水线里没有任何一步调用语言模型生成文本。`service.py` 的模块 docstring 第一句就是
"服务只协调确定性检索步骤，不生成自然语言答案"。**检索层的输出是证据，不是答案**——把证据变成
结论是 Planner 的事，而且要留下引用。

## 5.3 知识图的类型系统：八类节点、八类关系

```python
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
```

关系枚举的 docstring 点出了它的作用："白名单让递归查询只沿有业务含义的边传播，避免任意文本关系
造成不可解释路径。"以及一条运维提示："枚举值与数据库 CheckConstraint 完全一致，修改时必须同步
迁移、种子、Prompt 和测试。"

对比一下"开放关系"的图谱（比如从文本里抽取任意三元组）：那种图能长得很大，但路径不可读——
`(LTS, 相关, 分区)` 这样的边对排障毫无帮助。八类关系全部是**有方向、有排障语义**的：
`CAUSED_BY` 支撑根因、`MANIFESTS_AS` 连接根因与症状、`RESOLVED_BY` 指向方案。

### 5.3.1 风险等级：只有方案类节点能声明

这是一条值得单独读的双向校验：

```python
REMEDIATION_KNOWLEDGE_NODE_TYPES = frozenset(
    {KnowledgeNodeType.SOLUTION, KnowledgeNodeType.SOP}
)


def validate_remediation_risk_declaration(
    node_type: KnowledgeNodeType,
    remediation_risk_level: RiskLevel | None,
) -> None:
    is_remediation = node_type in REMEDIATION_KNOWLEDGE_NODE_TYPES
    if is_remediation and remediation_risk_level is None:
        raise ValueError("solution and sop nodes must declare remediation_risk_level")
    if not is_remediation and remediation_risk_level is not None:
        raise ValueError("only solution and sop nodes may declare remediation_risk_level")
```

它的 docstring 记录了两个方向各自的后果：

> 双向都必须显式失败：方案节点缺声明时，报告层只能退回硬编码默认值，于是"高风险操作"永远
> 产生不出来（这正是 `risk_level_hit_rate` 曾被实现卡住上限的原因）；非方案节点带声明时，
> 一个事实节点就能悄悄抬高报告风险等级。

第一个方向是**评测指标被实现卡住上限**的真实案例：如果没有任何节点声明风险等级，报告层只能给
所有建议一个默认等级，于是"高风险建议识别正确率"这个指标不管模型多聪明都上不去。这类缺陷极难
发现，因为它表现为"模型能力不足"。

第二个方向是**语义污染**：风险等级决定报告是否要求审批与回滚演练，如果一个 `component` 节点也能
带这个字段，那么一次种子编辑就能改变控制流。

同一个函数被三处复用：`KnowledgeNode.validate_remediation_risk_scope`（种子侧）、
`BundledKnowledgeNode.validate_remediation_risk_scope`（注入 Prompt 侧），以及数据库迁移
`20260716_0010_remediation_risk_level` 的约束。Bundle 侧的 docstring 说明了为什么不能只校验种子：

> Bundle 是报告层实际读取的对象，如果只在种子侧校验，任何绕过种子直接构造 Bundle 的路径
> （测试替身、缓存反序列化）都能让方案节点失去风险声明。

### 5.3.2 embedding 元数据：全有或全无

```python
    @model_validator(mode="after")
    def validate_embedding_metadata(self) -> KnowledgeNode:
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

三层结构：先判断"有没有元数据"，再分"无向量"与"有向量"两条路。四条具体检查各有原因，
docstring 只强调了最不直观的那一条：

> 非空向量还必须长度匹配、只含有限数值且不能为全零，因为 pgvector cosine distance 无法为
> 零向量提供有意义的方向相似度。

余弦相似度的定义是 `a·b / (|a||b|)`，全零向量的模长为 0，分母为零。数据库不会报错，
但排序结果没有意义——这是"静默错误"的典型形态，所以必须在类型层拦掉。

`embedding: list[float] | None = None` 允许为空这件事本身也有语义，节点 docstring 写着：
"embedding 允许为空以区分已建存储与尚未生成向量，不能把空值宣称为语义检索结果。"

### 5.3.3 Bundle 级校验的顺序

```python
    @model_validator(mode="after")
    def validate_unique_and_linked_graph(self) -> KnowledgeSeedBundle:
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
```

顺序是"重复 ID → 悬空引用 → 自环"，docstring 解释了理由："校验顺序先检查 ID 冲突，再建立节点
集合检查引用，因而错误信息能准确区分覆盖风险与悬空边。"这和第 1 章 `TimeRange` 先查时区再比
大小、第 4 章 `components` 先去重再判长度是同一类手法：**先排除会让后续检查产生错误结论的情况。**

如果不先查重复节点 ID，`set(node_ids)` 会把两个同 ID 节点合成一个，于是"引用完整"这条检查
可能通过，但数据库 upsert 之后只剩一个节点，另一个的内容静默消失。

自环禁令是给递归查询准备的：`WITH RECURSIVE` 遇到 `a → a` 会立刻形成无限循环。虽然 5.9 节的 SQL
里也有防环，但**在种子层就拒绝比在查询层挡住更好**——数据一开始就不该长成那样。

加载器只有 25 行，但也说明了一条选择：

```python
def load_knowledge_seed(path: Path) -> KnowledgeSeedBundle:
    if not path.is_file():
        raise FileNotFoundError(f"knowledge seed file does not exist: {path}")

    # JSON 保持跨语言标准格式，解释性内容放在实现指南，结构正确性由 Pydantic 集中保证。
    payload = json.loads(path.read_text(encoding="utf-8"))
    return KnowledgeSeedBundle.model_validate(payload)
```

用标准 JSON 而不是 YAML/JSON5，代价是不能写注释（说明全部搬进实现指南），收益是任何语言的
工具链都能直接读它。`seed_version: str = Field(pattern=r"^graph-seed:v[0-9]+$")` 让种子本身
带版本（`data/knowledge/cross_chain_graph.json` 当前是 `graph-seed:v12`），评测报告因此能标注
"这批数字跑在哪一版知识图上"。

## 5.4 Embedding：一个 Protocol，两个实现

### 5.4.1 为什么用 `Protocol` 而不是抽象基类

```python
class EmbeddingProvider(Protocol):
    @property
    def provider_id(self) -> str: ...
    @property
    def dimensions(self) -> int: ...
    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]: ...
```

Python 的 `Protocol`（PEP 544）是**结构化子类型**：任何类只要有这三个成员就满足协议，不需要
`class Foo(EmbeddingProvider)`。Java 读者可以理解成"编译期 duck typing"——比接口更松，因为
实现方不需要 import 协议定义。

好处在测试里最明显：`tests/unit/test_retrieval_service.py` 可以写一个十行的替身类喂固定向量，
不需要继承任何东西。而 `mypy`/IDE 仍然会检查签名匹配。

协议里两个属性的 docstring 各自写了一条硬纪律：

> 实现升级分词、模型权重或归一化规则时必须更换该 ID；调用方会据此排除旧向量，避免两个数学空间
> 即使维度相同也被 pgvector 直接比较。

**"维度相同 ≠ 空间相同"** 是向量检索最容易犯的错。两个 1024 维模型的向量可以做余弦计算、不会
报错，结果毫无意义。所以数据库里每行都存 `embedding_provider` 和 `embedding_dimensions`，
查询时两个条件都要匹配（见 5.6）。

`embed_texts` 的 docstring 规定了原子性：

> 批次中任一输入或 Provider 调用失败时应抛出异常，不返回无法与节点一一对应的部分结果；
> 该原子语义让知识写入可以在一个数据库事务中决定提交或回滚。

### 5.4.2 确定性 hash 基线：它存在的理由

```python
class DeterministicHashEmbeddingProvider:
    def __init__(self, *, dimensions: int = 128) -> None:
        if not 8 <= dimensions <= 4096:
            raise ValueError("embedding dimensions must be between 8 and 4096")
```

这个实现用 feature hashing（也叫 hashing trick）生成向量：提取特征 → SHA-256 散列到固定桶 →
有符号累加 → L2 归一化。

```python
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
```

三个细节：

- **为什么不用内置 `hash()`**：Python 3 默认对 `str` 启用哈希随机化（`PYTHONHASHSEED`），同一
  字符串在两次进程里 `hash()` 不同。用它建立的向量库重启后全部失效。这个 bug 一旦发生极难定位，
  因为"检索质量突然变差"没有任何异常。
- **符号位从摘要第 9 字节取**：`digest[8] & 1` 决定 +1 还是 -1。有符号累加让不同特征撞进同一桶时
  有一半概率互相抵消而不是叠加，降低碰撞造成的系统性偏差。
- **`norm == 0` 要显式拒绝**：理论上极罕见（所有特征刚好两两抵消），但一旦发生就会产生 5.3.2 那个
  零向量，所以在源头也挡一次。

特征提取兼顾中英文：

```python
def _extract_features(text: str) -> list[str]:
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
```

英文按空格/下划线切词，源码里的模式是：

```python
_TOKEN_PATTERN = re.compile(r"[a-z0-9_]+|[㐀-䶿一-鿿]+")
```

第二个分支用码位区间覆盖 CJK 扩展 A 与基本区，
中文没有分词边界所以取 1/2/3-gram。类型前缀（`word:` / `latin3:` / `cjk2:`）让不同特征族即使
字面相同也散列到不同桶。`NFKC` 归一化统一全角/半角，`casefold()` 比 `lower()` 更彻底。

**关键是模块 docstring 对这个实现的定位**：

> 它是工程基线而非神经语义模型。生产默认改用远程 `bge-m3:v1`，它提供真正的多语言语义空间；
> 两者通过版本化 provider_id 严格隔离，绝不混算。

这句话解决了一个作品集项目的真实难题：**没有 API key 的人也要能跑通全链路**。feature hashing
能真实执行 pgvector 查询、验证融合公式、跑消融测试，唯独不能理解同义词。诚实地说明这一点，
比号称"内置轻量语义模型"要好得多。

### 5.4.3 远程 Provider：三个必须做的校验

```python
        if len(response.data) != len(batch):
            raise EmbeddingProviderError("embedding provider returned a different batch size")
        # 兼容端点允许乱序返回，必须按 index 重排，否则节点会拿到别人的向量且无从察觉。
        ordered = sorted(response.data, key=lambda item: item.index)
        if [item.index for item in ordered] != list(range(len(batch))):
            raise EmbeddingProviderError("embedding provider returned non-contiguous indices")
```

"按 `index` 重排"这条是本章第一个**错配类缺陷**（后面 5.8 还有一个同类的）。OpenAI-compatible
规范允许服务端乱序返回，如果直接 `zip(texts, response.data)`，节点 A 会拿到节点 B 的向量。
表现是什么？检索结果看起来完全正常，只是"不太准"——没有异常、没有日志、指标也只是略低。
这类缺陷只能靠**在边界上断言契约**发现。

第三条检查是逐项校验维度与数值：

```python
        for item in ordered:
            vector = [float(value) for value in item.embedding]
            if len(vector) != self._dimensions:
                raise EmbeddingProviderError(
                    f"embedding provider returned {len(vector)} dimensions, "
                    f"expected {self._dimensions}"
                )
            if not all(isfinite(value) for value in vector):
                raise EmbeddingProviderError("embedding provider returned non-finite values")
            if not any(value != 0 for value in vector):
                raise EmbeddingProviderError("embedding provider returned an all-zero vector")
```

docstring 解释了为什么不等数据库拦：

> 成功后逐项校验维度、有限性和非零，确保数据库约束 `vector_dims(embedding) = embedding_dimensions`
> 不会在写入时才失败，也不会有全零向量污染语义排序。

数据库约束能拦维度，但**拦不住全零向量**（它维度是对的）。所以两层都要有。

构造器还有一个容易被忽略的决定：

```python
        self._client = client or AsyncOpenAI(
            api_key=api_key.get_secret_value(),
            base_url=base_url,
            timeout=timeout_seconds,
            max_retries=0,
            # 与 Planner/Auditor 共用出站身份：避免不同 Provider 在网关侧表现为不同客户端。
            default_headers=outbound_default_headers(),
        )
```

`max_retries=0` 关掉 SDK 隐式重试，docstring 说明了动机："使调用方看到的失败次数与真实请求次数
一致，便于评测统计成本。"SDK 默认会重试两次，于是"一次调用"实际可能是三次计费请求——评测报告
里的调用次数就成了假数字。第 2 章那条重试预算校验也建立在"重试由我们自己控制"这个前提上。

### 5.4.4 工厂：未知 ID 必须失败

```python
def create_embedding_provider(provider_id: str, *, dimensions: int, ...) -> EmbeddingProvider:
    if provider_id == DETERMINISTIC_HASH_PROVIDER_ID:
        return DeterministicHashEmbeddingProvider(dimensions=dimensions)
    if provider_id == BGE_M3_PROVIDER_ID:
        if api_key is None:
            raise ValueError(f"embedding provider {provider_id} requires an API key")
        if base_url is None:
            raise ValueError(f"embedding provider {provider_id} requires a base URL")
        return OpenAICompatibleEmbeddingProvider(...)
    raise ValueError(f"unsupported embedding provider: {provider_id}")
```

docstring："工厂只批准显式注册的两种实现，未知 ID 立即失败而不是静默回退，防止部署者以为正在
使用外部语义模型。"

**"静默回退到本地实现"是这里最坏的选项**：`DATAOPS_EMBEDDING_PROVIDER=bge-m3:v2`（拼错版本号）
如果回退成 hash 基线，部署者会以为语义检索已启用，而实际召回质量完全不同——并且数据库里已经
写满了另一个空间的向量。

### 5.4.5 入库：不修改输入，重新走一遍校验

```python
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
```

`model_copy(update=...)` 是 Pydantic 里更快的写法，但它**跳过校验器**。这里故意选慢的那条路：
第三方 Provider 返回的向量必须再过一遍 5.3.2 的四条检查。`zip(..., strict=True)` 是 Python 3.10+
的长度不等即报错，比事后 `assert len(a) == len(b)` 更贴近出错点。

文档库的嵌入函数还多做一件事：

```python
    # 按切片 ID 建索引再回填，避免依赖"文档顺序 × 切片顺序"这一隐式假设重新展开一次嵌套循环。
    vectors_by_chunk_id = {
        chunk.chunk_id: vector for (_, chunk), vector in zip(flattened, vectors, strict=True)
    }
```

先摊平成一维列表调用 Provider，再**按 ID 回填**而不是按顺序重新嵌套遍历。理由和 5.4.3 的按
`index` 重排完全一样：任何依赖"两次遍历顺序相同"的代码都是一个等待发生的错配。

## 5.5 通道 A：PostgreSQL 全文召回

很多人以为"有了向量就不需要关键词检索"。恰恰相反，排障场景里关键词极其重要：用户会直接把
`lts_daily_etl_0312` 这种任务 ID 或 `partition not ready` 这种日志片段贴进来，而 embedding 对
**精确标识符**的表现通常不如字面匹配。

```sql
WITH ranked AS (
    SELECT
        node_id, node_type, name, content, aliases, source_id, source_span,
        reliability, remediation_risk_level,
        ts_rank(
            to_tsvector(
                'simple',
                coalesce(name, '') || ' ' || coalesce(content, '') ||
                ' ' || coalesce(aliases::text, '')
            ),
            websearch_to_tsquery('simple', :query)
        ) AS text_rank,
        CASE
            WHEN lower(name) LIKE lower(:pattern) THEN 0.5
            WHEN lower(aliases::text) LIKE lower(:pattern) THEN 0.25
            ELSE 0
        END AS lexical_bonus
    FROM knowledge_nodes
)
SELECT *, greatest(text_rank + lexical_bonus, 0.001) AS lexical_score
FROM ranked
WHERE text_rank > 0 OR lexical_bonus > 0
ORDER BY lexical_score DESC, reliability DESC, node_id
LIMIT :limit
```

逐项说明：

- **`'simple'` 而不是 `'english'`**：`simple` 配置不做词干还原也不去停用词。方法 docstring 写了
  理由："`websearch_to_tsquery('simple')` 提供稳定英文标识符检索。"`english` 配置会把
  `lts_daily_etl` 这种标识符做词干处理，反而降低精确匹配率；而且我们的语料是中英混排，
  `english` 的停用词表对中文毫无帮助。
- **`websearch_to_tsquery` 而不是 `to_tsquery`**：后者要求输入是合法 tsquery 语法（`a & b`），
  用户随手输入的一句话会直接语法报错。`websearch_to_tsquery` 接受任意自由文本，像搜索引擎那样
  容错。**选它是因为输入是不可信的自然语言。**
- **`LIKE` bonus 补足全文的短板**：`ts_rank` 对很短的组件名（`lts`、`bds`）几乎给不出区分度，
  别名（`aliases::text` 把 JSONB 转成文本）也不在词典里。名称命中给 0.5、别名命中给 0.25——
  这两个数字是启发式的，但它们**只影响候选顺序，不影响最终结论**，而且会被后续重排重新评估。
- **`greatest(..., 0.001)`**：保证进入 `LexicalSeedMatch(lexical_score: Field(ge=0))` 的分数严格
  为正。零分候选和"没召回"在下游是两种不同语义。
- **三级排序 `lexical_score DESC, reliability DESC, node_id`**：最后一级用 ID 是为了**确定性**。
  没有它，同分候选的顺序由 PostgreSQL 执行计划决定，同一份数据两次查询可能给出不同 top-k，
  评测数字就不可复现了。这个手法在本项目出现了很多次（5.8、5.11 还会看到）。

方法前面有两条前置检查和一句注释：

```python
        if not query.strip():
            raise ValueError("query must not be blank")
        if not 1 <= limit <= 20:
            raise ValueError("limit must be between 1 and 20")
```

以及绑定参数的说明：

```python
        # 查询文本、LIKE 模式和 limit 都通过绑定参数传入，用户输入不会拼接进 SQL 结构。
        result = await self._session.execute(
            statement,
            {"query": query, "pattern": f"%{query}%", "limit": limit},
        )
```

这是本章唯一直接写 SQL 文本的地方（另一处是 5.9 的递归 CTE），所以注入防护要点明：三个值全部
走 `:name` 绑定参数。`f"%{query}%"` 里的 `%` 只是 LIKE 通配符，它出现在**参数值**里而不是 SQL
结构里，所以不构成注入面（用户输入的 `%` 会让匹配变宽，但不能改变语句）。

结果立刻转成领域模型：

```python
        # 每行立即转换成领域模型，在仓储边界拒绝数据库中的类型或约束漂移。
        return [
            LexicalSeedMatch(
                node=_node_from_mapping(row._mapping),
                lexical_score=float(row.lexical_score),
            )
            for row in result
        ]
```

`_node_from_mapping` 的 docstring 解释了 SQL 为什么不 `SELECT embedding`：

> 全文查询不需要 embedding，因此 SQL 不选择向量列，避免驱动把未声明类型的 vector 解码成文本，
> 也避免检索上下文重复携带大数组。

用 `text()` 原生 SQL 时，SQLAlchemy 不知道 `embedding` 列是 pgvector 类型，会把它当字符串返回——
一个 1024 维向量变成几千字符的文本，白白穿过整条链路。**不 SELECT 用不到的列**，在这里既是性能
问题也是正确性问题。

## 5.6 通道 B：pgvector 语义召回

```python
        if not query_embedding:
            raise ValueError("query_embedding must not be empty")
        if not all(isinstance(value, int | float) and isfinite(value) for value in query_embedding):
            raise ValueError("query_embedding values must be finite numbers")
        if not any(value != 0 for value in query_embedding):
            raise ValueError("query_embedding must not be an all-zero vector")
```

三条前置检查和 5.3.2 是同一套（有限、非零），因为查询向量和存储向量参与同一个余弦计算。

```python
        # cosine_distance 由 pgvector comparator 生成 `<=>`，数据库只对同一向量空间的行计算。
        distance = KnowledgeNodeRecord.embedding.cosine_distance(query_embedding)
        statement = (
            select(KnowledgeNodeRecord, distance.label("cosine_distance"))
            .where(
                KnowledgeNodeRecord.embedding.is_not(None),
                KnowledgeNodeRecord.embedding_provider == provider_id,
                KnowledgeNodeRecord.embedding_dimensions == len(query_embedding),
            )
            .order_by(
                distance,
                KnowledgeNodeRecord.reliability.desc(),
                KnowledgeNodeRecord.node_id,
            )
            .limit(limit)
        )
```

**三条 WHERE 缺一不可**：

| 条件 | 挡住什么 |
|---|---|
| `embedding.is_not(None)` | 尚未生成向量的节点参与排序（余弦对 NULL 无意义） |
| `embedding_provider == provider_id` | 不同模型空间混算（第 2 章那条启动校验的运行期对应物） |
| `embedding_dimensions == len(query_embedding)` | 同 Provider 但换过维度配置的历史残留 |

注意维度条件用的是 `len(query_embedding)` 而不是 Provider 声明的 `dimensions`——**用实际长度比
用声明值更严**，因为它同时验证了"Provider 说的和它给的一致"。

排序仍是三级，第三级又是 `node_id`。距离升序（越近越前），可靠性降序，ID 兜底。

```python
        for record, raw_distance in result:
            # cosine similarity 理论范围为 [-1, 1]；检索分数裁剪负相关项到零以保持评分契约。
            semantic_score = max(0.0, min(1.0, 1.0 - float(raw_distance)))
```

pgvector 的 `<=>` 返回 **cosine distance** = `1 - cosine_similarity`，范围 `[0, 2]`。转成
`[0, 1]` 的相似度需要 `1 - distance` 再裁剪负值。裁剪掉负相关（夹角大于 90°）是一个产品决定：
`HybridSeedMatch.semantic_score` 契约是 `ge=0, le=1`，而"负相关有多负"对排障没有意义——它们
本来也不会进 top-k。

**这一整段没有任何 Python 层的向量计算。** docstring 明确了这一点："不在 Python 中扫描全表。"
如果在应用层算余弦，就必须把全库向量拉出来，那不是检索而是全表扫描。

## 5.7 融合：五项加权，不做隐式归一化

两路候选在 `merge_seed_matches` 汇合。它是纯函数（不在类里），所以单测可以直接喂两个列表：

```python
    lexical_by_id = {match.node.node_id: match for match in lexical_matches}
    vector_by_id = {match.node.node_id: match for match in vector_matches}
    node_ids = lexical_by_id.keys() | vector_by_id.keys()
```

`keys() | keys()` 是**并集**（不是交集）——两路任一命中都算候选。这一点很关键：如果取交集，
纯语义命中的节点（用户描述症状，没提任何标识符）会被丢掉，向量通道就白做了。

```python
        if vector is not None:
            node = vector.node
        elif lexical is not None:
            node = lexical.node
        else:  # pragma: no cover - node_ids is the union of the two dictionaries.
            raise RuntimeError("merged seed ID is absent from both retrieval channels")
```

`else` 分支在逻辑上不可达（`node_ids` 是两个字典键的并集），但仍然写了 `raise` 而不是
`assert`——并且用 `# pragma: no cover` 明确告诉覆盖率工具"这行不该被测到"。这比留一个静默的
`node = None` 好：如果将来有人改了并集逻辑，这里会立刻炸而不是产生一个 `None` 节点。

评分本体：

```python
        # 可靠性来自人工知识节点；freshness 等案例时间字段进入模型后可在同一公式中补齐。
        reliability_score = node.reliability
        freshness_score = 0.0
        hybrid_score = (
            semantic_score * weights.semantic
            + lexical_score * weights.lexical
            + reliability_score * weights.reliability
            + freshness_score * weights.freshness
        )
```

注意**这里没有 `path` 分量**——种子阶段还没扩图。但权重对象用的仍是完整的五项权重，docstring
解释了为什么不重新归一化到四项：

> 种子阶段没有路径分量，案例新鲜度也尚未接入，因此 hybrid_score 只累加当前适用项，但仍使用完整
> 全局权重以便与路径分数衔接。

也就是说种子分数天然比路径分数低（少了 `path * 0.25` 这一项），这是**故意的**：有关系支撑的路径
证据应该排在孤立节点前面。如果种子阶段重新归一化，两种证据就不可比了。

权重本身在第 2 章见过一半，完整定义是：

```python
class HybridScoringWeights(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    semantic: float = Field(default=0.45, ge=0, le=1)
    lexical: float = Field(default=0.10, ge=0, le=1)
    path: float = Field(default=0.25, ge=0, le=1)
    reliability: float = Field(default=0.10, ge=0, le=1)
    freshness: float = Field(default=0.10, ge=0, le=1)

    @model_validator(mode="after")
    def validate_total_weight(self) -> HybridScoringWeights:
        total = self.semantic + self.lexical + self.path + self.reliability + self.freshness
        if abs(total - 1.0) > 1e-9:
            raise ValueError("hybrid scoring weights must sum to 1.0")
        return self
```

`1e-9` 容差只吸收十进制转二进制的误差（`0.45 + 0.10 + 0.25 + 0.10 + 0.10` 在浮点里不精确等于
`1.0`），不放过真实的配置错误。docstring 重申了第 2 章那条纪律："不自动归一化错误配置；显式失败
能让部署者看见评分契约变化，而不是让服务悄悄采用与文档不同的实际权重。"

最后的排序又是两级：

```python
    return sorted(
        merged,
        key=lambda match: (-match.hybrid_score, match.node.node_id),
    )[:limit]
```

`-score` 配 `node_id` 升序，等价于"分数降序、同分按 ID"。**Python 的 `sorted` 是稳定排序**，
但这里输入顺序来自 `set` 的迭代（`node_ids` 是集合），本身不确定，所以必须靠 `node_id` 兜底。

## 5.8 精排：cross-encoder 与"失败只降级为不重排"

### 5.8.1 为什么双塔 embedding 之后还需要 cross-encoder

`reranker.py` 的模块 docstring 把动机说完了：

> 双塔 embedding 为了可索引必须独立编码查询和文档，因此无法建模两者的交互；cross-encoder 把
> 查询与候选拼在一起联合打分，在小候选集上显著更准。

这是检索领域的基本事实。双塔（bi-encoder）把查询和文档分别编码成向量，好处是文档向量可以预先
建索引；代价是编码文档时**它还不知道查询是什么**。cross-encoder 把 `[查询, 文档]` 一起送进模型，
能建模词与词的交互，但因此**无法预计算**——必须每个候选跑一次。

所以标准做法是两阶段：一阶段召回 N×k 个候选（便宜、可索引），二阶段对这 N×k 个精排（贵、准）。
本项目 `rerank_candidate_multiplier` 默认 3、`seed_limit` 默认 5，即一阶段召回 15 条送精排，
最后留 5 条。

`_candidate_limit` 把这件事写成代码：

```python
    def _candidate_limit(self, seed_limit: int) -> int:
        if self._reranker is None:
            return seed_limit
        multiplied = seed_limit * self._rerank_candidate_multiplier
        return max(seed_limit, min(multiplied, MAX_SEED_CANDIDATES, MAX_RERANK_DOCUMENTS))
```

docstring 点明收益来源与双重上限的必要性：

> 放大是两阶段检索的收益来源——精排只能在候选集内部改名次，候选太少就无从提升。上限同时受
> 仓储 top-k 契约和重排端点单次文档数约束，避免配置一个大倍数后请求被远程截断却无人察觉。

"精排只能在候选集内部改名次"这句值得记住：**如果一阶段没召回，精排救不回来。** 倍数=1 时精排
只是重新排列同一批 5 条，几乎不可能改变最终进入上下文的集合。

`min(multiplied, MAX_SEED_CANDIDATES, MAX_RERANK_DOCUMENTS)` 里 `MAX_SEED_CANDIDATES = 20`
（仓储 `limit` 契约上限），`MAX_RERANK_DOCUMENTS = 64`（端点契约）。外面再套 `max(seed_limit, ...)`
保证不会因为上限把候选压到比种子数还少。

### 5.8.2 按 index 回填：本章第二个错配陷阱

```python
    async def rerank(self, query: str, documents: Sequence[str]) -> list[float]:
        if not query.strip():
            raise ValueError("rerank query must not be blank")
        if not documents:
            return []
        if len(documents) > self._max_documents:
            raise ValueError(f"rerank accepts at most {self._max_documents} documents")
```

`len(documents) > max` 时**直接拒绝而不是截断尾部**，docstring 解释："候选数超限直接拒绝而不是
静默丢弃尾部，避免调用方以为全部候选都被评估过。"这和第 3 章"所有门禁整批拒绝而不截断"是同一
条纪律。

请求体：

```python
        # 截断在客户端完成而不是依赖服务端策略，这样"单次调用最多送出多少字符"是本地可证明的成本
        # 上界；top_n 显式等于候选数，避免端点默认只返回前若干条后调用方拿到不完整的分数集合。
        payload = {
            "model": self._model,
            "query": query[:MAX_RERANK_QUERY_CHARS],
            "documents": [document[:MAX_RERANK_DOCUMENT_CHARS] for document in documents],
            "top_n": len(documents),
            "return_documents": False,
        }
```

`top_n=len(documents)` 是必须显式写的：Jina/Cohere 风格的 `/rerank` 端点默认可能只返回前若干条，
那样有些候选就没有分数。`return_documents=False` 省掉回传正文的带宽——我们只要分数，文档本来
就在本地。

然后是这一节的重点：

```python
def _scores_in_input_order(body: object, *, expected: int) -> list[float]:
    if not isinstance(body, dict):
        raise RerankerError("rerank response must be a JSON object")
    results = body.get("results")
    if not isinstance(results, list) or len(results) != expected:
        raise RerankerError("rerank response did not score every submitted document")

    # 预填 None 而不是 0.0：只有"未被赋值"与"分数为零"两种状态可区分，才能在最后检测出既没有
    # 重复索引也没有越界、却仍有候选未被打分的响应，而不是把缺失静默当成不相关。
    scores: list[float | None] = [None] * expected
    for item in results:
        if not isinstance(item, dict):
            raise RerankerError("rerank result entries must be JSON objects")
        index = item.get("index")
        score = item.get("relevance_score")
        if not isinstance(index, int) or not 0 <= index < expected:
            raise RerankerError("rerank result contained an out-of-range index")
        if scores[index] is not None:
            raise RerankerError("rerank result contained a duplicate index")
        if not isinstance(score, int | float):
            raise RerankerError("rerank result relevance_score must be numeric")
        scores[index] = max(0.0, min(1.0, float(score)))

    if any(score is None for score in scores):
        raise RerankerError("rerank response left at least one document unscored")
    return [score for score in scores if score is not None]
```

`rerank` 方法的 docstring 直接把这个陷阱命名了：

> 响应按 `index` 回填到原位置：该端点按分数降序返回，若直接按响应顺序读取会把最高分错配给第一个
> 候选，这是此类 API 最容易出现且最难发现的缺陷。

**`/rerank` 端点按分数降序返回结果，不是按输入顺序。** 如果写成
`zip(candidates, body["results"])`，那么"最相关的那一条的分数"会被赋给"输入里的第一条候选"。
结果是什么？排序完全错乱，但每个分数都在合法范围内，没有异常，看起来像是"重排模型效果不好"。

预填 `None` 而不是 `0.0` 是一个很细的选择，注释已经解释了：`0.0` 是合法分数，用它做哨兵就没法
区分"没打分"和"打了零分"。三条检查（越界、重复、最终仍有 `None`）合起来才覆盖所有错配形态：
越界和重复能抓住大部分，但"响应条数对、索引都合法、却漏了一个又重复了一个"这种情况需要重复检查；
而"条数对、索引合法、无重复、仍有空位"在数学上不可能——除非 `expected` 与实际不一致，
所以最后那条检查是纵深防御。

最后一行 `[score for score in scores if score is not None]` 从类型上把
`list[float | None]` 收窄成 `list[float]`，让 mypy 满意（等价于 `cast`，但不需要断言）。

### 5.8.3 重排失败必须降级，不能失败

```python
    async def _rerank_candidates(
        self, query: str, candidates: list[HybridSeedMatch]
    ) -> tuple[list[HybridSeedMatch], str | None]:
        if self._reranker is None or not candidates:
            return candidates, None

        documents = [knowledge_node_text(candidate.node) for candidate in candidates]
        try:
            scores = await self._reranker.rerank(query, documents)
        except RerankerError:
            return candidates, None
        if len(scores) != len(candidates):
            return candidates, None
```

docstring 的理由是本章最重要的一句可用性论述：

> `RerankerError` 一律降级为"保留一阶段排序"：重排是可选增强，把它变成可用性依赖会让一个计费
> 外部服务抖动直接击穿整条诊断链路。

三条降级路径（未配置、异常、长度不符）**全部返回 `(candidates, None)`**——第二个元素是模型名。
`None` 会一路传到 `GraphRetrievalResult.reranker_model`，于是报告里"是否精排过"永远是真实的。
`create_reranker` 的 docstring 把这条规则说得更明确：

> 返回 None 而不是一个"恒等重排"替身，是为了让检索结果里的 `reranker_model` 字段真实为空，
> 从而评测和报告不会把未重排的排序说成重排结果。

**"恒等替身"是很自然的设计（省掉一堆 `if`），但它会伪造事实。** 这是本项目反复出现的取舍：
宁可多几个 `None` 判断，也不要让数据说谎。

成功路径：

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
            sorted(reranked, key=lambda match: (-match.final_score, match.node.node_id)),
            self._reranker.model,
        )
```

这里用了 `model_copy(update=...)`（跳过校验），和 5.4.5 的选择相反。区别在于**数据来源**：那里
是第三方 Provider 返回的向量（不可信），这里两个字段都刚由本地代码算出并已 `bounded_score`
裁剪过。不过要注意 `rerank_score` 与 `final_score` 必须**同时**更新，否则会违反 5.10 那条不变量。

### 5.8.4 融合公式：为什么不直接用重排分覆盖

```python
def blend_scores(hybrid_score: float, rerank_score: float, *, blend: float) -> float:
    return bounded_score((1 - blend) * hybrid_score + blend * rerank_score)
```

默认 `rerank_blend_weight = 0.4`，即最终分 = 0.6×一阶段 + 0.4×精排。docstring：

> 选择线性融合而不是直接用重排分覆盖，是因为 cross-encoder 只看查询与文本的语义匹配，完全不知道
> 节点可靠性、图路径强度和检索通道；两者相加才能既吸收精排的判别力，又保留知识库自身的可信度
> 信息。权重进入检索结果契约，因此任何名次变化都能被归因到具体的一个数字而不是隐藏策略。

关键在于**两个分数看的是不同维度**。cross-encoder 不知道这个节点是人工审核过的 SOP（
`reliability=1.0`）还是一条推测；也不知道它有几条关系边。直接覆盖等于扔掉知识库自己的元信息。

`rerank_blend_weight` 进入 `GraphRetrievalResult` 契约这件事也有讲究：

```python
            rerank_blend_weight=self._rerank_blend_weight if reranker_model else 0,
```

没有实际重排时写 0，不写配置值。**记录"实际生效的值"而不是"配置的值"**——否则评测报告会显示
一次未重排的检索带着 `blend=0.4`，让人以为精排参与了。

## 5.9 图扩展：一条递归 CTE

这是全项目最"数据库"的一段代码，也是 GraphRAG 里 "Graph" 三个字母的全部实现。

```sql
WITH RECURSIVE graph_paths AS (
    SELECT
        ARRAY[e.from_node_id, e.to_node_id]::varchar[] AS node_ids,
        ARRAY[e.edge_id]::varchar[] AS edge_ids,
        1 AS depth,
        e.weight::double precision AS path_score
    FROM knowledge_edges e
    WHERE e.from_node_id = :seed_node_id
      AND e.relation_type = ANY(:relations)

    UNION ALL

    SELECT
        gp.node_ids || e.to_node_id,
        gp.edge_ids || e.edge_id,
        gp.depth + 1,
        (gp.path_score * e.weight)::double precision
    FROM graph_paths gp
    JOIN knowledge_edges e ON e.from_node_id = gp.node_ids[array_length(gp.node_ids, 1)]
    WHERE gp.depth < :max_hops
      AND e.relation_type = ANY(:relations)
      AND NOT e.to_node_id = ANY(gp.node_ids)
)
SELECT node_ids, edge_ids, depth, path_score
FROM graph_paths
ORDER BY depth, path_score DESC, edge_ids
```

`WITH RECURSIVE` 的结构是固定的两段：**基础项**（第一个 SELECT，从种子出发的一跳）和
**递归项**（`UNION ALL` 之后，引用 CTE 自身）。PostgreSQL 会反复执行递归项，直到某一轮产生零行。

四个设计点：

**1. 用数组累积路径，而不是只记末节点。** `node_ids` 和 `edge_ids` 是 varchar 数组，每跳用 `||`
追加。这解决了三件事：

- **防环**：`NOT e.to_node_id = ANY(gp.node_ids)` 拒绝已经出现在本路径上的目标节点。源码注释：
  "递归项只从当前路径末节点继续，并拒绝目标已在 node_ids 中，形成简单有向路径。"没有这一条，
  `a → b → a` 会无限展开（`depth < max_hops` 能兜住，但会产生大量重复路径）。
- **保序**：`node_ids[array_length(node_ids, 1)]` 取末元素继续扩展；数组顺序就是路径方向。
- **稳定引用**：`edge_ids` 的有序序列直接决定 `path_id`（见下）。

**2. 边权逐跳相乘。** `gp.path_score * e.weight`。因为 `weight: Field(gt=0, le=1)`，乘积必然落在
`(0, 1]` 且**随深度单调下降**——两跳路径天然弱于一跳路径。这正好符合排障直觉：间接关系比直接关系
可信度低。`GraphPath.score: Field(gt=0, le=1)` 的 `gt=0` 也因此成立（权重严格为正，乘积不会是 0）。

**3. 关系白名单用 `= ANY(:relations)` 绑定数组。**

```python
        ).bindparams(bindparam("relations", type_=ARRAY(String())))
        # ARRAY(String) 明确告诉 SQLAlchemy 如何绑定关系白名单，避免驱动猜测数组元素类型。
        result = await self._session.execute(
            statement,
            {
                "seed_node_id": seed_node_id,
                "max_hops": max_hops,
                "relations": sorted(relation.value for relation in relations),
            },
        )
```

必须显式声明 `type_=ARRAY(String())`，否则 asyncpg 拿到一个 Python `list[str]` 不知道该绑成什么
PostgreSQL 类型。`sorted(...)` 让参数本身也确定（对结果无影响，但让查询日志可比对）。

**4. `ORDER BY depth, path_score DESC, edge_ids`。** 又是确定性排序，第三级用 `edge_ids` 数组
（PostgreSQL 支持数组比较）。

### 5.9.1 空关系集合与 `None` 的语义区别

```python
        if max_hops not in {1, 2}:
            raise ValueError("max_hops must be 1 or 2")

        # None 表示采用完整批准枚举；显式空集合表示调用方禁止所有扩展，二者语义不同。
        relations = set(KnowledgeRelationType) if allowed_relations is None else allowed_relations
        if not relations:
            return []
```

`None` = "用默认全集"，`set()` = "什么都不许扩展"。这个区分让消融测试能干净地关掉图扩展而不用改
调用签名。**在 Python 里 `None` 和空集合是两个不同的信号，别用同一个 falsy 判断把它们合并。**

`max_hops not in {1, 2}` 的上限来自产品决定：两跳。三跳路径的可解释性急剧下降（"A 因为 B 因为 C
因为 D"很难向值班工程师交代），而组合爆炸的成本却是指数级的。`GraphPath.depth: Field(ge=1, le=2)`
在类型层再写一次同一个界。

### 5.9.2 两段式加载：先 ID，再实体

```python
        # 先物化轻量路径行；无边时立即返回，避免额外节点/边查询。
        rows = list(result)
        if not rows:
            return []

        # 批量加载所有唯一实体，避免为每条路径产生 N+1 查询。
        node_ids = {node_id for row in rows for node_id in row.node_ids}
        edge_ids = {edge_id for row in rows for edge_id in row.edge_ids}
        node_records = (
            await self._session.scalars(
                select(KnowledgeNodeRecord).where(KnowledgeNodeRecord.node_id.in_(node_ids))
            )
        ).all()
```

方法 docstring 解释了为什么分两步：

> 查询先返回轻量 ID 数组，再批量加载涉及的节点/边，避免递归行重复携带大文本。

递归 CTE 如果直接 SELECT 完整节点内容，同一个热点节点（比如 `lts` 组件节点）会在几十条路径里
各出现一次，每次都带 4000 字符正文。改成"CTE 只返回 ID，之后一次 `IN (...)` 批量加载"，
每个实体只传输一次。

这是经典的 **N+1 查询**问题的变体，解法也是经典的：批量加载 + 内存映射。

```python
        # 映射表用于按 SQL 路径数组原顺序重建领域对象，保留方向和逐跳关系。
        nodes_by_id = {record.node_id: _node_from_record(record) for record in node_records}
        edges_by_id = {record.edge_id: _edge_from_record(record) for record in edge_records}

        # GraphPath 再次执行 Pydantic 校验，确保数据库查询结果满足一至两跳领域契约。
        paths: list[GraphPath] = []
        for row in rows:
            path_nodes = [nodes_by_id[node_id] for node_id in row.node_ids]
            path_edges = [edges_by_id[edge_id] for edge_id in row.edge_ids]
            source_ids = sorted({item.source_id for item in [*path_nodes, *path_edges]})
            paths.append(
                GraphPath(
                    path_id=_path_id(row.edge_ids),
                    nodes=path_nodes,
                    edges=path_edges,
                    depth=int(row.depth),
                    score=float(row.path_score),
                    source_ids=source_ids,
                )
            )
```

重建时**按 SQL 数组的原顺序**索引映射表，所以方向信息不丢。`source_ids` 收集路径上所有节点和边的
来源并去重排序——这是 Auditor 逐条核对 `source_span` 的入口。

### 5.9.3 `path_id`：为什么不排序

```python
def _path_id(edge_ids: list[str]) -> str:
    """根据有序 edge_id 序列生成可重放的短 SHA-256 图路径引用。

    边顺序决定路径方向，因此不排序；相同有序路径跨查询得到相同 ID，删边或换序则改变引用。
    16 位摘要适合小型作品集规模，不用于安全签名或跨系统全局唯一标识。
    """

    digest = sha256("|".join(edge_ids).encode("utf-8")).hexdigest()[:16]
    return f"path_{digest}"
```

**不排序**这件事在第 4 章 `_stable_unique` 那里出现过一次，这里的理由不同但同样重要：
`a → b → c` 和 `c → b → a` 是两条**不同**的路径（关系有方向），如果 `path_id` 对边 ID 排序，
两者会得到同一个 ID。

反过来"删边或换序则改变引用"是消融测试的基础：第 14 章在事务里删掉一条边，路径消失或改变，
报告里的 `path_id` 引用随之失效——这就证明了结论真的依赖图结构。

`[:16]` 的取舍在 docstring 里写明了适用范围。16 个十六进制字符 = 64 位，本项目规模下碰撞概率
可忽略；这里明确声明"不用于安全签名"，避免有人将来把它当成防篡改标识。

`GraphPath.path_id: Field(pattern=r"^path_[a-f0-9]{16}$")` 在类型层锁定格式，
`BundledGraphPath.evidence_id` 用同一个正则——所以报告里的引用和检索结果的 ID 是同一个空间。

## 5.10 三层分数与一条不可绕过的不变量

`HybridSeedMatch` 和 `ScoredGraphPath` 都保存**三层分数**：

| 字段 | 含义 | 来源 |
|---|---|---|
| `hybrid_score` | 一阶段五项加权分 | `merge_seed_matches` / `score_graph_path` |
| `rerank_score` | 二阶段 cross-encoder 分 | `_rerank_candidates`，可为 `None` |
| `final_score` | 实际排序值 | `blend_scores`，或等于 `hybrid_score` |

`HybridSeedMatch` 的 docstring 说明了为什么要三个而不是一个：

> `hybrid_score` 是五项加权的一阶段分数，`rerank_score` 是二阶段 cross-encoder 分数，
> `final_score` 是两者的显式融合结果——三者分开保存，才能在评测里判断名次变化来自哪一阶段。

然后是把这个契约钉死的不变量：

```python
def validate_rerank_consistency(
    hybrid_score: float,
    rerank_score: float | None,
    final_score: float,
) -> None:
    if rerank_score is None and abs(final_score - hybrid_score) > 1e-9:
        raise ValueError("final_score must equal hybrid_score when no rerank score is present")
```

一句话：**最终分偏离一阶段分，必须有一个真实存在的二阶段分数来解释。** 它挡住的是"某个中间步骤
悄悄调了一下排序值"这类改动——那种改动会让评测里的名次变化无法归因。

`ScoredGraphPath` 的同名校验器 docstring 还多说了一层：

> 路径的最终排序决定哪些证据进入上下文预算，因此这条不变量比种子层更重要：任何"看起来更相关"
> 的重新排序都必须留下可核对的二阶段分数。

排序影响的不只是展示顺序——第 6 章会看到 `EvidenceBundleBudget` 按最终分从高到低选，选不下的进
`omitted_*`。**排序就是选择。**

配套的还有一个 `mode="before"` 校验器：

```python
def default_final_score(data: object) -> object:
    if isinstance(data, dict) and "final_score" not in data and "hybrid_score" in data:
        return {**data, "final_score": data["hybrid_score"]}
    return data
```

docstring 解释了两条克制：只处理 `dict`（其它输入原样交给 Pydantic，"避免这里替 Pydantic 猜测
未知输入形态"），`hybrid_score` 缺失时也不补齐（"让原本的 missing 错误照常报告在正确字段上"）。

**这个默认值让"省略即未重排"成为唯一自洽的写法**，`HybridSeedMatch.default_final_score` 的
docstring：如果要求每个调用点重复写一次相同数字，只会制造两者不一致的机会。

### 5.10.1 `scoring.py` 为什么要单独存在

四个函数、一个枚举、73 行，却是独立模块。模块 docstring 给了两个理由：

> 把这些规则放进独立的底层模块，而不是让 `documents.py` 反向导入 `models.py` 的私有函数，既消除了
> 循环导入，也保证两条通道不会因为各自复制一份实现而在容差、裁剪范围或融合公式上悄悄分叉——那种
> 分叉在评测里表现为无法归因的名次变化，是最难发现的一类问题。

依赖方向是 `scoring.py ← models.py`（图通道）和 `scoring.py ← documents.py`（文档通道）。
如果没有这个模块，两条通道要么互相导入（循环），要么各写一份 `blend_scores`——然后某天有人只改了
一边的 `blend` 公式。

最后一句划清了边界："本模块只做纯函数与枚举，不感知任何数据库、Provider 或 Pydantic 模型。"
这就是它能被任何层引用的原因。

## 5.11 `retrieve()`：把四段拼起来

现在可以完整读主流程了。

```python
        # Provider 使用批量接口以兼容远程模型；单查询必须严格返回一个固定维度向量。
        query_vectors = await self._embedding_provider.embed_texts([query])
        if len(query_vectors) != 1:
            raise ValueError("embedding provider must return exactly one query vector")
        query_embedding = query_vectors[0]
        if len(query_embedding) != self._embedding_provider.dimensions:
            raise ValueError("query embedding length does not match provider dimensions")
```

即使只有一个查询也走批量接口（协议只有批量方法），但要**验证 Provider 真的只返回了一个**，
并且长度和它自己声明的维度一致。第二条检查针对的是"Provider 声明 1024 维但返回 768 维"这种
配置漂移——不查的话，5.6 那个 `embedding_dimensions == len(query_embedding)` 条件会静默匹配零行，
语义通道变成"永远没召回"。

```python
        candidate_limit = self._candidate_limit(seed_limit)
        # vector-only/vector-graph 故意关闭全文通道，隔离图结构相对于纯向量检索的真实增益。
        lexical_matches: list[LexicalSeedMatch] = []
        if mode is RetrievalMode.HYBRID_GRAPH:
            lexical_matches = await self._repository.search_lexical_seeds(query, limit=candidate_limit)
        vector_matches = await self._repository.search_vector_seeds(
            query_embedding,
            provider_id=self._embedding_provider.provider_id,
            limit=candidate_limit,
        )
```

三种模式的差异全在这里和下面的 `if mode is not RetrievalMode.VECTOR_ONLY`：

| 模式 | 全文通道 | 向量通道 | 图扩展 | 用途 |
|---|---|---|---|---|
| `vector_only` | ✗ | ✓ | ✗ | 消融基线：纯向量 RAG |
| `vector_graph` | ✗ | ✓ | ✓ | 隔离"图扩展"的增益 |
| `hybrid_graph` | ✓ | ✓ | ✓ | 生产默认 |

枚举的 docstring 说明了为什么要做成显式模式："显式枚举防止评测通过隐藏布尔开关得到无法复现的
比较结果。"三种模式两两对比，正好隔离出两项增益：`vector_only → vector_graph` 是图结构的贡献，
`vector_graph → hybrid_graph` 是全文通道的贡献。**消融设计决定了枚举的取值**，不是反过来。

注意两次 SQL 是**顺序 await 而不是 `asyncio.gather`**。类 docstring 解释了：

> 每次调用先生成一个查询向量，再顺序使用同一 AsyncSession 执行两路 SQL，避免并发复用会话。

SQLAlchemy 的 `AsyncSession` **不是并发安全**的。这和第 3 章工具调用能并行形成对比：那里每个
`call_tool` 开独立 stdio 子进程，所以能 `gather`；这里两条 SQL 共享一个会话，必须串行。
**并行的前提是资源独立**，不是"看起来互不依赖"。

```python
        candidates = merge_seed_matches(lexical_matches, vector_matches, weights=..., limit=candidate_limit)
        seeds, reranker_model = await self._rerank_candidates(query, candidates)
        seeds = seeds[:seed_limit]
```

**截断在重排之后**，这是两阶段检索的关键顺序。先截断再重排就等于精排只能在最终 5 条里重排，
候选放大完全白费。

图扩展部分：

```python
        # SIMILAR_TO 只由已确认案例注册器写入；纳入白名单后，case 向量种子才能从任一方向扩展
        # 到相关先例，同时 pending/rejected 因没有图节点而无法借此进入上下文。
        allowed_relations = {
            KnowledgeRelationType.DEPENDS_ON,
            KnowledgeRelationType.CAUSED_BY,
            KnowledgeRelationType.MANIFESTS_AS,
            KnowledgeRelationType.RESOLVED_BY,
            KnowledgeRelationType.RUNS_ON,
            KnowledgeRelationType.PRODUCES,
            KnowledgeRelationType.CONSUMES,
            KnowledgeRelationType.SIMILAR_TO,
        }
```

八种关系全部列出（等价于 `set(KnowledgeRelationType)`），但**显式写出来**——将来新增第九种关系
时，它不会自动进入生产检索，必须有人在这里加一行并解释为什么。这是"显式优于隐式"的一个实例，
代价是一点重复。

那条注释还交代了案例记忆与图的连接方式：只有 `confirmed` 案例会被注册成图节点，所以
`pending`/`rejected` 案例**在结构上**没法通过 `SIMILAR_TO` 进入上下文。第 10 章会看到注册器。

```python
        paths_by_id: dict[str, ScoredGraphPath] = {}
        if mode is not RetrievalMode.VECTOR_ONLY:
            for seed in seeds:
                paths = await self._repository.expand_paths(
                    seed.node.node_id, max_hops=max_hops, allowed_relations=allowed_relations
                )
                for path in paths:
                    scored_path = score_graph_path(
                        path, seed=seed, weights=self._score_weights,
                        rerank_blend_weight=self._rerank_blend_weight,
                    )
                    current = paths_by_id.get(path.path_id)
                    if current is None or scored_path.final_score > current.final_score:
                        # 多种子命中同一路径时只保留解释分更强的一版，真实 edge 序列保持不变。
                        paths_by_id[path.path_id] = scored_path
```

多个种子可能扩展到同一条路径（比如两个症状节点都指向同一个根因）。去重按 `path_id`，保留
**最终分更高的那一版**。注释强调"真实 edge 序列保持不变"——去重只影响分数解释（`seed_node_id`、
各分量），不影响图结构本身，所以引用仍然有效。

`score_graph_path` 的评分：

```python
    hybrid_score = bounded_score(
        seed.semantic_score * weights.semantic
        + seed.lexical_score * weights.lexical
        + path.score * weights.path
        + seed.reliability_score * weights.reliability
        + seed.freshness_score * weights.freshness
    )
    final_score = hybrid_score
    if seed.rerank_score is not None:
        final_score = blend_scores(hybrid_score, seed.rerank_score, blend=rerank_blend_weight)
```

这才是**五项都用上**的完整公式（对比 5.7 种子阶段缺 `path`）。路径**继承种子的重排分**，
docstring 给了理由：

> 路径的相关性来源是"这个种子值得展开"，因此重复把拼接文本送进 cross-encoder 只会增加成本而不
> 增加信息。

这是一个成本决定，也是一个语义决定：cross-encoder 面对"节点 A 正文 + 节点 B 正文 + 边说明"这种
拼接文本给出的分数，含义相当模糊。

最后：

```python
            paths=sorted(
                paths_by_id.values(),
                key=lambda path: (-path.final_score, -path.depth, path.path_id),
            ),
```

三级排序，中间那级是 `-depth`：**同分时深度大的排前面**。两跳路径信息量更大（多一条关系边），
在分数相同的情况下更值得进上下文。第三级仍然是 ID 兜底。

## 5.12 写入侧：谁负责事务

`PostgresGraphRepository` 的构造器只收一个 `AsyncSession`，docstring 明确了所有权：

> 构造器不打开连接、不提交也不回滚；这种所有权边界允许种子写入原子提交，也允许集成测试
> 在事务中删边后回滚消融。

**仓储不管事务**这条纪律直接决定了消融测试能不能写。`tests/unit/test_graph_ablation.py` 和
`-m postgres` 集成测试的做法是：开事务 → 删一条边 → 跑检索 → 断言路径变化 → 回滚。如果仓储自己
`commit()`，这个测试就得真的破坏数据库再修回来。

`upsert_seed_bundle` 的两处注释解释了写入顺序：

```python
        # 节点先于边写入是外键依赖要求；逐条 upsert 保留教学可读性和精确失败位置。
        for node in bundle.nodes:
            ...
            # 冲突更新完整来源字段，避免旧版本种子在重复部署后残留过时内容。
            statement = statement.on_conflict_do_update(
                index_elements=[KnowledgeNodeRecord.node_id],
                set_={**values, "updated_at": func.now()},
            )
```

```python
        # 所有节点已进入同一事务后再写边，尚未 commit 的节点对该会话仍可满足外键检查。
```

第二条注释澄清了一个容易困惑的点：外键约束在**语句级**检查，同一事务内未提交的插入对本会话可见，
所以不需要先 commit 节点再写边。

`ON CONFLICT DO UPDATE` 让 `python -m app.persistence.seed` 幂等——Docker 每次启动都会跑它。
而"更新完整字段"而不是只更新变化字段，是为了让旧版本内容不残留（种子改了正文但 ID 不变时）。

还有一个专门服务启动校验的方法：

```python
    async def count_embedded_nodes(self, *, provider_id: str, dimensions: int) -> int:
        ...
        count = await self._session.scalar(
            select(func.count())
            .select_from(KnowledgeNodeRecord)
            .where(
                KnowledgeNodeRecord.embedding.is_not(None),
                KnowledgeNodeRecord.embedding_provider == provider_id,
                KnowledgeNodeRecord.embedding_dimensions == dimensions,
            )
        )
```

docstring 点明了它和 5.6 那个查询的关系：

> 过滤条件与 cosine 查询完全一致，因此数字不会把旧 Provider 或不同维度记录误报为当前空间可用数据。

**统计口径必须和查询口径完全一致**，否则第 2 章那条"全有或全无"校验就会失效：如果这里不带
provider 过滤，切换 embedding 模型后节点数依然对得上，服务照常启动，而语义通道实际上一条也召不回。

## 5.13 本章小结

| 设计选择 | 实现 | 拒绝的替代方案 |
|---|---|---|
| 检索输出证据不是答案 | `GraphRetrievalResult` 全结构化，无自然语言 | 检索层直接拼 Prompt 文本 |
| 关系有限且有语义 | 八类 `KnowledgeRelationType` 白名单 | 从文本抽任意三元组 |
| 风险等级只能由方案节点声明 | `validate_remediation_risk_declaration` 双向校验 | 报告层从动作文本嗅探关键词 |
| 向量空间严格隔离 | `provider_id` + `dimensions` 双条件过滤 | 只比维度 |
| 无 key 也能跑通全链路 | feature hashing 基线 + 版本化 provider_id | 号称"内置轻量语义模型" |
| 批量结果按 ID/index 回填 | embedding 按 `index` 重排、rerank 按 `index` 预填 `None` | 依赖响应顺序 `zip` |
| 精排是增强不是依赖 | `RerankerError` → 保留一阶段排序，模型名留空 | 恒等重排替身 |
| 排序值必须可归因 | `validate_rerank_consistency` 三层分数 | 只保存一个 final score |
| 权重错误必须暴露 | 总和 ≠ 1 直接失败 | 隐式归一化 |
| 图路径可引用、可消融 | 有序 `edge_ids` → `path_id`，不排序 | 只返回相似文本 |
| 递归受控 | 数组防环 + `depth ≤ 2` + 关系白名单 | 无界图遍历 |
| 事务归调用方 | 仓储不 commit / rollback | 仓储内部自动提交 |
| 排序处处确定 | 每个 `ORDER BY` / `sorted` 都以 ID 兜底 | 依赖执行计划顺序 |

两句话概括这一章：

1. **GraphRAG 的"图"不是修饰词。** 它带来的是纯向量检索拿不到的东西：有方向的关系、可引用的
   `path_id`、以及"删掉一条边结论就变"这种可验证性。代价是知识必须人工建模，不能只喂文档。
2. **两阶段检索的每一处顺序都有理由。** 先放大候选再精排、先精排再截断、先算一阶段分再融合、
   先返回 ID 再批量加载实体——顺序反了功能仍然"能跑"，只是收益消失或者数字失去意义。

下一章看文档 RAG：Runbook 这类静态文档怎么切片、怎么和知识图共享同一个引用空间、以及
`EvidenceBundleBudget` 如何在字节和条数两个维度上决定"哪些证据进得去 Prompt"。

