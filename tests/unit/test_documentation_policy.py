"""强制执行学习/求职项目的注释与实现原理文档约束。

本测试扫描所有人工编写的 Python 模块，要求存在足够说明职责和设计意图的模块 docstring，
并检查实现指南覆盖当前核心技术。这样新增文件无法在缺少学习型说明时通过测试。
"""

import ast
import io
import tokenize
from pathlib import Path
from zipfile import ZipFile

PYTHON_ROOTS = (Path("app"), Path("mcp_server"), Path("tests"))
MIN_MODULE_DOCSTRING_LENGTH = 60
MIN_CALLABLE_DOCSTRING_LENGTH = 80
CRITICAL_INLINE_COMMENT_FILES = (
    Path("app/api/main.py"),
    Path("app/mcp/client.py"),
    Path("app/mcp/executor.py"),
    Path("app/mcp/observation.py"),
    Path("app/persistence/database.py"),
    Path("app/persistence/migrations/env.py"),
    Path("app/persistence/migrations/versions/20260710_0001_knowledge_graph.py"),
    Path("app/persistence/seed.py"),
    Path("app/retrieval/repository.py"),
    Path("app/retrieval/embeddings.py"),
    Path("app/retrieval/budget.py"),
    Path("app/retrieval/ablation.py"),
    Path("app/retrieval/service.py"),
    Path("mcp_server/repository.py"),
)
REQUIRED_GUIDE_SECTIONS = (
    "Pydantic 契约",
    "Fixture 与 Golden Case",
    "MCP 真实协议边界",
    "FastAPI lifespan",
    "PostgreSQL、SQLAlchemy、Alembic 与 pgvector",
    "显式 GraphRAG 路径",
    "测试分层",
)


def test_every_python_module_has_a_learning_oriented_docstring() -> None:
    """验证每个 Python 模块都先解释文件职责、技术边界和学习入口。

    AST 读取真正的模块 docstring，而不是把任意散落字符串误判为说明；长度下限只承担
    防止空壳文档的机械门禁，具体内容质量仍由代码评审检查。
    """

    missing_or_short: list[str] = []
    for root in PYTHON_ROOTS:
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            docstring = ast.get_docstring(tree, clean=True) or ""
            if len(docstring) < MIN_MODULE_DOCSTRING_LENGTH:
                missing_or_short.append(f"{path} ({len(docstring)} chars)")

    assert missing_or_short == [], (
        "Every Python file needs a module docstring explaining responsibility, "
        f"principle, and boundary: {missing_or_short}"
    )


def test_every_python_callable_has_a_detailed_docstring() -> None:
    """验证所有类、函数、异步函数、方法与测试函数都有独立解释性说明。

    递归 AST 遍历会覆盖类方法和嵌套函数，因而文件头不能替代 callable 说明；失败消息
    精确给出文件、行号、类型和名称，方便开发者直接定位需要补充的学习型文档。
    """

    missing_or_short: list[str] = []
    callable_nodes = (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)

    for root in PYTHON_ROOTS:
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, callable_nodes):
                    continue
                docstring = ast.get_docstring(node, clean=True) or ""
                if len(docstring) < MIN_CALLABLE_DOCSTRING_LENGTH:
                    kind = type(node).__name__
                    missing_or_short.append(
                        f"{path}:{node.lineno} {kind} {node.name} ({len(docstring)} chars)"
                    )

    assert missing_or_short == [], (
        "Every class/function/method/test needs a callable-level docstring explaining "
        f"inputs, outputs, principle, boundaries, and failure behavior: {missing_or_short}"
    )


def test_complex_boundary_modules_contain_critical_step_comments() -> None:
    """验证高风险边界函数体中存在解释关键步骤的内联注释。

    tokenize 能区分真实注释与字符串中的井号。这里要求每个协议、数据库或生命周期模块
    至少有两条非 shebang 注释，确保实现者说明校验顺序、重试、事务或资源释放原理。
    """

    insufficient: list[str] = []
    for path in CRITICAL_INLINE_COMMENT_FILES:
        source = path.read_text(encoding="utf-8")
        comments = [
            token.string
            for token in tokenize.generate_tokens(io.StringIO(source).readline)
            if token.type == tokenize.COMMENT and not token.string.startswith("#!")
        ]
        if len(comments) < 2:
            insufficient.append(f"{path} ({len(comments)} inline comments)")

    assert insufficient == [], (
        "Complex boundary modules need critical-step comments explaining control flow: "
        f"{insufficient}"
    )


def test_implementation_guide_covers_current_technology_boundaries() -> None:
    """确认面试型实现指南覆盖当前已经落地的全部技术边界。

    该测试防止代码引入协议或基础设施后只留下局部注释；若新增核心技术，开发者必须把
    原理、调用链、限制和验证方式同步到可连续阅读的实现指南。
    """

    guide = Path("docs/implementation-guide.md").read_text(encoding="utf-8")

    for section in REQUIRED_GUIDE_SECTIONS:
        assert section in guide
    assert "原始人工知识 JSON 的 embedding 仍为 `null`" in guide
    assert "可替换 Embedding Provider" in guide
    assert "五项混合评分" in guide
    assert "Evidence Bundle 上下文预算" in guide
    assert "Vector-only / Vector+Graph 消融" in guide
    assert "尚未完成" in guide
    assert "代码注释的强制粒度" in guide
    assert "callable 级 docstring" in guide


def test_prompt_contract_versions_budgeted_retrieval_inputs() -> None:
    """确认 Prompt 契约区分完整检索 v2 与预算化 Evidence Bundle v1 输入。

    Planner Prompt 文本未改变，但两个占位符的数据语义已经版本化；测试防止后续代码升级 contract ID
    后文档仍声称旧结构，或遗漏路径原子选择和 truncated 的解释。
    """

    prompt_contract = Path("docs/prompt-contracts.md").read_text(encoding="utf-8")

    assert "graphrag-retrieval:v2" in prompt_contract
    assert "graphrag-evidence-bundle:v1" in prompt_contract
    assert "路径只有在其全部节点、边和来源能一起进入预算" in prompt_contract
    assert "truncated=true" in prompt_contract


def test_ablation_report_labels_measured_values_and_honest_zero_gain() -> None:
    """确认消融报告明确标注实测条件，并保留根因命中没有提升的真实结果。

    该门禁防止作品集只展示链路正增益却删除根因差值为零的事实，也要求报告记录 Provider、预算、
    path_id 和适用范围，避免把单个合成案例外推为通用准确率。
    """

    report = Path("docs/graphrag-ablation-results.md").read_text(encoding="utf-8")

    assert "实测值" in report
    assert "deterministic-hash:v1" in report
    assert "根因节点命中 | 1 | 1 | 0" in report
    assert "必要有序链路完整率 | 0.0 | 1.0 | +1.0" in report
    assert "path_4f6638ec28f7073d" in report
    assert "不能将本次结果外推" in report


def test_agents_policy_marks_documentation_as_definition_of_done() -> None:
    """确认仓库规则与开发 Skill 都把细粒度注释列入完成定义。

    同时检查两处规则是为了让人工开发和 Codex 辅助开发使用同一标准，避免只更新其中
    一个入口后，后续切片再次生成只有文件头说明的实现。
    """

    policy = Path("AGENTS.md").read_text(encoding="utf-8")
    skill = Path(".agents/skills/build-dataops-troubleshooter/SKILL.md").read_text(encoding="utf-8")

    assert "学习和求职展示" in policy
    assert "docs/implementation-guide.md" in policy
    assert "学习型说明是完成定义的一部分" in policy
    assert "每个类、函数、异步函数、方法和测试函数" in policy
    assert "文件开头的统一说明不能替代" in policy
    assert "保证学习与求职可解释性" in skill
    assert "将学习型说明纳入完成定义" in skill
    assert "callable 级 docstring" in skill
    assert "AST 扫描" in skill


def test_learning_constraint_is_synchronized_to_formal_docx() -> None:
    """确认正式 DOCX 阅读版包含与 Markdown 基线一致的学习型约束。

    DOCX 是面试展示用正式文档，无法通过普通文本 diff 可靠审查，因此测试直接读取压缩包
    内的 WordprocessingML，至少验证关键章节和实现指南引用没有在生成时丢失。
    """

    docx_path = next(Path("docs").glob("*.docx"))
    with ZipFile(docx_path) as package:
        document_xml = package.read("word/document.xml").decode("utf-8")

    assert "学习与求职可解释性" in document_xml
    assert "docs/implementation-guide.md" in document_xml
    assert "callable 级 docstring" in document_xml
    assert "文件开头的统一说明不能替代函数级说明" in document_xml
    assert "面向学习和面试讲解的实现指南" in document_xml
