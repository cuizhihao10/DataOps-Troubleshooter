"""强制执行学习/求职项目的注释与实现原理文档约束。

本测试扫描所有人工编写的 Python 模块，要求存在足够说明职责和设计意图的模块 docstring，
并检查实现指南覆盖当前核心技术。这样新增文件无法在缺少学习型说明时通过测试。
"""

import ast
from pathlib import Path
from zipfile import ZipFile

PYTHON_ROOTS = (Path("app"), Path("mcp_server"), Path("tests"))
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
    missing_or_short: list[str] = []
    for root in PYTHON_ROOTS:
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            docstring = ast.get_docstring(tree, clean=True) or ""
            if len(docstring) < 60:
                missing_or_short.append(f"{path} ({len(docstring)} chars)")

    assert missing_or_short == [], (
        "Every Python file needs a module docstring explaining responsibility, "
        f"principle, and boundary: {missing_or_short}"
    )


def test_implementation_guide_covers_current_technology_boundaries() -> None:
    guide = Path("docs/implementation-guide.md").read_text(encoding="utf-8")

    for section in REQUIRED_GUIDE_SECTIONS:
        assert section in guide
    assert "embedding 仍为 `null`" in guide
    assert "尚未完成" in guide


def test_agents_policy_marks_documentation_as_definition_of_done() -> None:
    policy = Path("AGENTS.md").read_text(encoding="utf-8")
    skill = Path(".agents/skills/build-dataops-troubleshooter/SKILL.md").read_text(encoding="utf-8")

    assert "学习和求职展示" in policy
    assert "docs/implementation-guide.md" in policy
    assert "学习型说明是完成定义的一部分" in policy
    assert "保证学习与求职可解释性" in skill
    assert "将学习型说明纳入完成定义" in skill


def test_learning_constraint_is_synchronized_to_formal_docx() -> None:
    docx_path = next(Path("docs").glob("*.docx"))
    with ZipFile(docx_path) as package:
        document_xml = package.read("word/document.xml").decode("utf-8")

    assert "学习与求职可解释性" in document_xml
    assert "docs/implementation-guide.md" in document_xml
    assert "面向学习和面试讲解的实现指南" in document_xml
