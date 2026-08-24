"""把测试进程与开发者本地 `.env` 隔离，保证配置断言检查的是仓库默认值。

`Settings` 通过 pydantic-settings 读取 `.env`，而真实部署需要在该文件里写入 SiliconFlow 凭据、
远程 provider ID 和 1024 维配置。若不隔离，`.env` 一存在，单元测试断言的默认 provider、维度和
重排开关就会随开发者机器变化，出现"本地红、CI 绿"或反之的不可复现失败。因此这里在整个测试
会话期间清空 `DATAOPS_*` 环境变量并禁用 env_file，让配置类测试永远面对确定输入；需要真实
凭据的集成测试仍可通过 `DATAOPS_TEST_DATABASE_URL` 等专用变量显式选择加入。
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from app.core.settings import Settings, get_settings

# 测试专用变量不属于 Settings 的 DATAOPS_ 配置面，必须保留，否则 Postgres 集成测试会被跳过。
_PRESERVED_ENVIRONMENT_VARIABLES = frozenset(
    {
        "DATAOPS_TEST_DATABASE_URL",
    }
)


@pytest.fixture(autouse=True, scope="session")
def isolate_settings_environment() -> Iterator[None]:
    """在会话开始时移除 `DATAOPS_*` 覆盖并禁用 `.env`，结束时精确恢复原值。

    autouse + session 作用域让隔离对每个测试文件自动生效，无需逐个测试记得声明；直接改
    `Settings.model_config` 而不是猴补丁读取逻辑，是因为 pydantic-settings 在实例化时才解析
    env_file，改配置足以让所有后续构造都跳过磁盘读取。`get_settings` 的进程级缓存在前后各清一次，
    避免隔离前构造的实例泄漏进测试，或测试期构造的实例泄漏给其它会话工具。
    """

    removed = {
        name: value
        for name, value in os.environ.items()
        if name.startswith("DATAOPS_") and name not in _PRESERVED_ENVIRONMENT_VARIABLES
    }
    for name in removed:
        del os.environ[name]
    original_env_file = Settings.model_config.get("env_file")
    Settings.model_config["env_file"] = None
    get_settings.cache_clear()
    try:
        yield
    finally:
        Settings.model_config["env_file"] = original_env_file
        os.environ.update(removed)
        get_settings.cache_clear()
