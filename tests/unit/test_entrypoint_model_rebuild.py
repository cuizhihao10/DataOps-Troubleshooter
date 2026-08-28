"""守护 `python -m` 入口模块里的 Pydantic 模型在导入期就完成 schema 构建。

runpy 会把 `-m` 目标装进一个名为 `__main__` 的临时模块，配合 `from __future__ import annotations`，
Pydantic 建类时解析不到该模块命名空间里的 `Literal` 等符号，于是把 core schema 推迟成 mock，直到
第一次实例化才补建；而评测报告恰好是整轮跑完才构造的对象，补建失败会让一次全量付费评测的结果
全部作废（实测过一次：28 条案例跑完后抛 PydanticUserError，报告未落盘）。本测试按入口模块逐个复现
那套命名空间条件，断言模型此时已经完成构建，从而把这类失败挡在任何模型调用之前。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from pydantic import BaseModel

APP_ROOT = Path(__file__).resolve().parents[2] / "app"


def _entrypoint_modules() -> list[Path]:
    """收集 `app/` 下所有带 `if __name__ == "__main__":` 守卫的模块路径。

    动态发现而不是维护一张清单：新增一个 `-m` 入口时本测试自动覆盖它，否则这条约束会退化成
    "只保护当年写下的两个文件"，而入口模块恰恰是最容易在几个月后被复制出第三个的地方。
    """

    modules = [
        path
        for path in sorted(APP_ROOT.rglob("*.py"))
        if any(_is_main_guard(node) for node in ast.parse(path.read_text(encoding="utf-8")).body)
    ]
    assert modules, "no __main__ entrypoint modules were discovered under app/"
    return modules


def _is_main_guard(node: ast.stmt) -> bool:
    """判断一个顶层语句是否为 `if __name__ == "__main__":` 守卫。

    只匹配这一种确切形态（左侧 Name 为 `__name__`、右侧常量为 `__main__`），因此不会把普通条件
    分支误判成入口守卫，也不会依赖源码里的空白或注释格式。
    """

    if not isinstance(node, ast.If) or not isinstance(node.test, ast.Compare):
        return False
    test = node.test
    if not isinstance(test.left, ast.Name) or test.left.id != "__name__":
        return False
    if len(test.comparators) != 1 or not isinstance(test.comparators[0], ast.Constant):
        return False
    return test.comparators[0].value == "__main__"


def _execute_as_main(path: Path) -> dict[str, object]:
    """在 `__name__ == "__main__"` 且 `sys.modules["__main__"]` 指向别处的命名空间里执行模块源码。

    这正是 `runpy.run_module(..., run_name="__main__")` 的实测形态：模块代码跑在一份 `__main__`
    命名空间里，但 `sys.modules["__main__"]` 并不是它，于是 Pydantic 按 `cls.__module__` 回查
    命名空间时找不到 `Literal` 等符号，只能把 schema 推迟成 mock。入口守卫被剔除，因为守卫里通常是
    `SystemExit(main())`，执行它会真的去连数据库或调模型；本测试只关心建类阶段。
    """

    tree = ast.parse(path.read_text(encoding="utf-8"))
    tree.body = [node for node in tree.body if not _is_main_guard(node)]
    namespace: dict[str, object] = {"__name__": "__main__", "__file__": str(path)}
    exec(compile(tree, str(path), "exec"), namespace)  # noqa: S102
    return namespace


@pytest.mark.parametrize("path", _entrypoint_modules(), ids=lambda path: path.name)
def test_entrypoint_models_are_built_at_import_time(path: Path) -> None:
    """每个入口模块在 `__main__` 命名空间下定义的 Pydantic 模型都必须已完成 schema 构建。

    未完成构建不会立刻报错，只会把失败推迟到第一次实例化——对评测入口来说那一刻在所有付费调用
    之后，因此这里要求模块自己在导入期调用 `model_rebuild()`，把代价从"整轮结果作废"降到"进程
    起不来"。模型为零的入口模块自然通过，不需要为它们添加任何样板。
    """

    namespace = _execute_as_main(path)
    models = [
        value
        for value in namespace.values()
        if isinstance(value, type)
        and issubclass(value, BaseModel)
        and value.__module__ == "__main__"
    ]
    incomplete = sorted(
        model.__name__ for model in models if not getattr(model, "__pydantic_complete__", False)
    )
    assert not incomplete, (
        f"{path.name} defines Pydantic models that are not fully defined when the module runs as "
        f"__main__: {incomplete}; call model_rebuild() at module level"
    )
