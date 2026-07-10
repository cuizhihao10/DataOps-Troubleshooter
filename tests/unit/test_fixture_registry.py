"""验证场景注册、Golden Case 引用和失败 Fixture 覆盖。

测试确保五个场景可重复加载、九工具主场景完整、错误类别齐全，并拒绝重复 scenario_id
和工具请求引用其他场景等会破坏可复现性的输入。
"""

import json
import shutil
from pathlib import Path

import pytest

from app.core.fixture_registry import FixtureRegistry, load_golden_cases
from app.domain.tooling import ToolErrorCode

FIXTURE_DIRECTORY = Path("data/fixtures/scenarios")
GOLDEN_CASE_FILE = Path("data/fixtures/golden_cases.json")


def test_all_scenarios_load_and_match_golden_cases() -> None:
    """验证全部合成场景和 Golden Case 可加载，且案例引用集合没有悬空项。

    固定数量断言捕获文件意外遗漏或重复，集合相等则保证每个评测案例都指向真实场景且当前场景
    均被案例覆盖；加载过程同时执行 JSON、Pydantic 和跨元素唯一性校验。
    """

    registry = FixtureRegistry.from_directory(FIXTURE_DIRECTORY)
    golden_cases = load_golden_cases(GOLDEN_CASE_FILE)

    assert len(registry) == 5
    assert len(golden_cases) == 5
    assert {case.scenario_id for case in golden_cases} == set(registry.scenario_ids)


def test_main_scenario_exercises_all_nine_tool_contracts() -> None:
    """验证跨组件主演示场景包含产品基线规定的全部九个工具名称。

    取集合而不是只数记录可发现重复工具冒充完整覆盖；该断言保护演示场景能够贯穿 LTS、BDS 与
    FlashSync 的状态、日志、拓扑/表/一致性观察，而不是只展示部分协议能力。
    """

    scenario = FixtureRegistry.from_directory(FIXTURE_DIRECTORY).get("cross_chain_pk_conflict")
    assert len({result.tool_name for result in scenario.tool_results}) == 9


def test_failure_scenarios_cover_required_error_classes() -> None:
    """验证 Fixture 集合覆盖空结果、超时、权限拒绝和服务不可用四类关键失败。

    测试遍历所有失败响应收集标准错误码，要求集合精确相等；这样既防止删除降级场景，也能发现
    未经设计的新错误分类悄然进入评测基线，确保重试和非重试分支都有可复现数据。
    """

    registry = FixtureRegistry.from_directory(FIXTURE_DIRECTORY)
    error_codes = {
        result.response.error_code
        for scenario in registry
        for result in scenario.tool_results
        if not result.response.ok
    }
    assert error_codes == {
        ToolErrorCode.EMPTY_RESULT,
        ToolErrorCode.TIMEOUT,
        ToolErrorCode.PERMISSION_DENIED,
        ToolErrorCode.SERVICE_UNAVAILABLE,
    }


def test_duplicate_scenario_id_is_rejected(tmp_path: Path) -> None:
    """验证两个不同文件声明同一 scenario_id 时注册表在启动阶段拒绝加载。

    测试复制同一合法 Fixture，隔离掉字段错误，只触发跨文件唯一性不变量；若未拒绝，后加载文件
    会静默覆盖前者并破坏同一输入的可重放性，因此必须得到明确 ValueError。
    """

    source = FIXTURE_DIRECTORY / "lts_empty_result.json"
    shutil.copyfile(source, tmp_path / "first.json")
    shutil.copyfile(source, tmp_path / "second.json")

    with pytest.raises(ValueError, match="duplicate scenario_id"):
        FixtureRegistry.from_directory(tmp_path)


def test_fixture_scenario_id_must_match_tool_request(tmp_path: Path) -> None:
    """验证 Fixture 内嵌工具请求不能引用与外层场景不同的 scenario_id。

    测试只篡改首个请求并通过临时目录重新加载，期望 Pydantic Bundle 校验失败；该约束防止复制
    场景时遗留引用，导致 MCP 以一个场景名返回另一个场景的证据。
    """

    source = FIXTURE_DIRECTORY / "lts_empty_result.json"
    payload = json.loads(source.read_text(encoding="utf-8"))
    payload["tool_results"][0]["request"]["scenario_id"] = "different_scenario"
    target = tmp_path / "invalid.json"
    target.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="scenario_id must match"):
        FixtureRegistry.from_directory(tmp_path)
