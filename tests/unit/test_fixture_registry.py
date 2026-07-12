"""验证场景注册、Golden Case 引用、证据冲突标注和失败 Fixture 覆盖。

测试确保八个场景可重复加载、九工具主场景完整、错误类别齐全，并拒绝重复 scenario_id
和工具请求引用其他场景等会破坏可复现性的输入。
"""

import json
import shutil
from pathlib import Path

import pytest

from app.core.fixture_registry import FixtureRegistry, load_golden_cases
from app.domain.scenarios import GoldenCaseCategory
from app.domain.tooling import ToolErrorCode, ToolName

FIXTURE_DIRECTORY = Path("data/fixtures/scenarios")
GOLDEN_CASE_FILE = Path("data/fixtures/golden_cases.json")


def test_all_scenarios_load_and_match_golden_cases() -> None:
    """验证全部合成场景和 Golden Case 可加载，且案例引用集合没有悬空项。

    固定数量断言捕获文件意外遗漏或重复，集合相等则保证每个评测案例都指向真实场景且当前场景
    均被案例覆盖；加载过程同时执行 JSON、Pydantic 和跨元素唯一性校验。
    """

    registry = FixtureRegistry.from_directory(FIXTURE_DIRECTORY)
    golden_cases = load_golden_cases(GOLDEN_CASE_FILE)

    assert len(registry) == 8
    assert len(golden_cases) == 18
    assert {case.scenario_id for case in golden_cases} == set(registry.scenario_ids)
    assert {case.contract_id for case in golden_cases} == {"golden-case:v7"}
    category_counts = {
        category: sum(case.case_category is category for case in golden_cases)
        for category in GoldenCaseCategory
    }
    assert category_counts == {
        GoldenCaseCategory.SINGLE_COMPONENT: 4,
        GoldenCaseCategory.CROSS_COMPONENT: 4,
        GoldenCaseCategory.AMBIGUOUS_OR_INSUFFICIENT: 4,
        GoldenCaseCategory.TOOL_ANOMALY_OR_CONFLICT: 3,
        GoldenCaseCategory.MEMORY_RECALL: 3,
    }
    cross_chain = next(
        case for case in golden_cases if case.case_id == "golden_cross_chain_pk_conflict"
    )
    assert [path.path_label for path in cross_chain.required_fault_paths] == [
        "component_dependency_chain",
        "sync_backlog_causal_chain",
    ]
    conflict_case = next(
        case
        for case in golden_cases
        if case.case_id == "golden_bds_conflicting_partition_evidence"
    )
    assert conflict_case.evidence_conflict_expectation is not None
    assert len(conflict_case.evidence_conflict_expectation.conflicting_evidence_sources) == 3
    lts_bds_case = next(
        case
        for case in golden_cases
        if case.case_id == "golden_cross_lts_blocked_by_bds_partition"
    )
    assert [path.path_label for path in lts_bds_case.required_fault_paths] == [
        "lts_task_depends_on_bds_task",
        "bds_task_consumes_delayed_dataset",
    ]
    bds_flashsync_case = next(
        case
        for case in golden_cases
        if case.case_id == "golden_cross_bds_blocked_by_flashsync_conflict"
    )
    assert [path.path_label for path in bds_flashsync_case.required_fault_paths] == [
        "bds_task_depends_on_flashsync_task",
        "flashsync_task_produces_bds_dataset",
        "flashsync_backlog_conflict_solution_chain",
    ]
    resource_case = next(
        case
        for case in golden_cases
        if case.case_id == "golden_cross_lts_blocked_by_bds_resource_exhaustion"
    )
    assert [path.path_label for path in resource_case.required_fault_paths] == [
        "lts_component_depends_on_bds_component"
    ]
    missing_context_case = next(
        case
        for case in golden_cases
        if case.case_id == "golden_ambiguous_bds_missing_resource_context"
    )
    assert missing_context_case.required_tools == []
    assert missing_context_case.allowed_root_causes == []
    missing_log_case = next(
        case
        for case in golden_cases
        if case.case_id == "golden_ambiguous_flashsync_missing_causal_log"
    )
    assert len(missing_log_case.required_tools) == 3
    assert missing_log_case.allowed_root_causes == []
    unavailable_case = next(
        case
        for case in golden_cases
        if case.case_id == "golden_ambiguous_lts_all_observations_unavailable"
    )
    assert unavailable_case.required_tools == [
        ToolName.LTS_GET_TASK_STATUS,
        ToolName.LTS_GET_TASK_LOG,
        ToolName.LTS_GET_DEPENDENCY_TOPOLOGY,
    ]
    assert unavailable_case.required_evidence_sources == []
    assert unavailable_case.allowed_root_causes == []


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


def test_golden_fault_path_requires_one_relation_per_adjacent_node(tmp_path: Path) -> None:
    """验证 Golden v2 路径不能用三个节点只标一条关系来伪造完整链路。

    测试仅删除主案例第二条关系，其他字段保持合法；加载必须在评测执行前失败，避免评分器猜测
    节点之间的未标注边类型或错误地把半条路径算作完整。
    """

    payload = json.loads(GOLDEN_CASE_FILE.read_text(encoding="utf-8"))
    payload[0]["required_fault_paths"][0]["required_relation_types"] = ["DEPENDS_ON"]
    target = tmp_path / "invalid_golden_cases.json"
    target.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="relations must connect every adjacent node"):
        load_golden_cases(target)


def test_golden_case_rejects_duplicate_path_labels(tmp_path: Path) -> None:
    """验证同一案例的路径标签不能重复，防止失败明细和宏观分母歧义。

    节点与关系仍合法，只把第二条路径标签改成第一条；Pydantic 跨字段校验应拒绝输入，而不是让
    评测报告出现两个无法区分的 requirement。
    """

    payload = json.loads(GOLDEN_CASE_FILE.read_text(encoding="utf-8"))
    payload[0]["required_fault_paths"][1]["path_label"] = payload[0]["required_fault_paths"][0][
        "path_label"
    ]
    target = tmp_path / "duplicate_path_labels.json"
    target.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="fault path labels must be unique"):
        load_golden_cases(target)


def test_memory_category_requires_history_expectation(tmp_path: Path) -> None:
    """验证 memory_recall 类别不能缺少必要/禁止历史案例标注。

    测试删除首条记忆案例的 history_expectation；加载必须失败，避免类别配额看似完成但评测器没有
    可执行的召回、投影和实时优先验收条件。
    """

    payload = json.loads(GOLDEN_CASE_FILE.read_text(encoding="utf-8"))
    memory_case = next(case for case in payload if case["case_category"] == "memory_recall")
    memory_case.pop("history_expectation")
    target = tmp_path / "missing_history_expectation.json"
    target.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="must be present together"):
        load_golden_cases(target)


def test_history_conflict_flag_must_match_current_allowed_roots(tmp_path: Path) -> None:
    """验证历史根因与本次允许根因冲突时必须显式标记 conflict。

    测试把 BDS 旧数据倾斜案例的冲突标记改为 false；Schema 应拒绝，防止实时优先评测因漏标而
    把历史覆盖当前 Observation 的风险排除在分母之外。
    """

    payload = json.loads(GOLDEN_CASE_FILE.read_text(encoding="utf-8"))
    conflict_case = next(
        case for case in payload if case["case_id"] == "golden_memory_bds_conflict_guard"
    )
    conflict_case["history_expectation"]["required_memories"][0]["expect_root_conflict"] = False
    target = tmp_path / "invalid_history_conflict.json"
    target.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="conflict flag must match"):
        load_golden_cases(target)


def test_evidence_conflict_sources_must_also_be_required_evidence(tmp_path: Path) -> None:
    """验证冲突来源不能绕过普通 Evidence source 覆盖义务单独存在。

    测试从冲突案例的 required_evidence_sources 删除一项，但保留冲突标注；加载必须在 runner 执行
    前失败。否则评分器可能一边宣称观察到完整冲突，一边让通用证据覆盖率忽略同一来源。
    """

    payload = json.loads(GOLDEN_CASE_FILE.read_text(encoding="utf-8"))
    conflict_case = next(
        case
        for case in payload
        if case["case_id"] == "golden_bds_conflicting_partition_evidence"
    )
    conflict_case["required_evidence_sources"].pop()
    target = tmp_path / "invalid_conflict_sources.json"
    target.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="must be required evidence sources"):
        load_golden_cases(target)


def test_evidence_conflict_expectation_requires_conflict_category(tmp_path: Path) -> None:
    """验证证据冲突专用标注不能挂到普通单组件类别上污染配额。

    测试只把冲突案例类别改为 single_component，保留三个成功响应和全部期望字段；Schema 应在加载
    时拒绝。否则类别统计会显示普通故障增加，而专用冲突指标仍悄悄参与聚合，破坏 8/10/4/3/3 配额。
    """

    payload = json.loads(GOLDEN_CASE_FILE.read_text(encoding="utf-8"))
    conflict_case = next(
        case
        for case in payload
        if case["case_id"] == "golden_bds_conflicting_partition_evidence"
    )
    conflict_case["case_category"] = "single_component"
    target = tmp_path / "invalid_conflict_category.json"
    target.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="requires tool anomaly/conflict category"):
        load_golden_cases(target)


def test_no_root_conflict_expectation_rejects_allowed_root_causes(tmp_path: Path) -> None:
    """验证要求“不下根因”的冲突案例不能同时声明可接受根因。

    测试向当前空 allowed_root_causes 注入一个单侧结论；Schema 必须拒绝互相矛盾的验收口径，避免
    Top-1 指标鼓励输出根因，而冲突安全指标又要求报告保持空根因。
    """

    payload = json.loads(GOLDEN_CASE_FILE.read_text(encoding="utf-8"))
    conflict_case = next(
        case
        for case in payload
        if case["case_id"] == "golden_bds_conflicting_partition_evidence"
    )
    conflict_case["allowed_root_causes"] = ["任一单侧结论"]
    target = tmp_path / "invalid_conflict_allowed_root.json"
    target.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="requires empty allowed root causes"):
        load_golden_cases(target)


def test_cross_component_category_requires_multiple_tool_components_and_path(
    tmp_path: Path,
) -> None:
    """验证跨组件配额必须同时由多组件 Action 和显式故障路径支撑。

    第一份负向数据删除所有 BDS 工具，只保留 LTS Action；第二份保留多组件工具但清空路径。两者均
    必须在加载阶段失败，防止仅修改 category 标签或堆叠互不相连的工具调用来虚增 10 条跨组件配额。
    """

    payload = json.loads(GOLDEN_CASE_FILE.read_text(encoding="utf-8"))
    cross_case = next(
        case
        for case in payload
        if case["case_id"] == "golden_cross_lts_blocked_by_bds_partition"
    )
    cross_case["required_tools"] = [
        tool for tool in cross_case["required_tools"] if tool.startswith("lts.")
    ]
    single_component_target = tmp_path / "invalid_cross_single_component.json"
    single_component_target.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="tools from at least two components"):
        load_golden_cases(single_component_target)

    payload = json.loads(GOLDEN_CASE_FILE.read_text(encoding="utf-8"))
    cross_case = next(
        case
        for case in payload
        if case["case_id"] == "golden_cross_lts_blocked_by_bds_partition"
    )
    cross_case["required_fault_paths"] = []
    missing_path_target = tmp_path / "invalid_cross_missing_path.json"
    missing_path_target.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="requires at least one fault path"):
        load_golden_cases(missing_path_target)


def test_zero_tool_case_requires_ambiguous_category_and_empty_observation_contract(
    tmp_path: Path,
) -> None:
    """验证零 MCP Action 只能表达补参场景，且不能同时要求路径或 Evidence。

    第一份数据把零工具案例改为普通单组件类别；第二份保留模糊类别却注入必要 Evidence source。两者
    都必须在加载阶段失败，避免用空 Action 绕过普通诊断义务，或声明永远无法由工具产生的证据分母。
    """

    payload = json.loads(GOLDEN_CASE_FILE.read_text(encoding="utf-8"))
    target_case = next(
        case
        for case in payload
        if case["case_id"] == "golden_ambiguous_bds_missing_resource_context"
    )
    target_case["case_category"] = "single_component"
    wrong_category = tmp_path / "invalid_zero_tool_category.json"
    wrong_category.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="requires ambiguous/insufficient category"):
        load_golden_cases(wrong_category)

    payload = json.loads(GOLDEN_CASE_FILE.read_text(encoding="utf-8"))
    target_case = next(
        case
        for case in payload
        if case["case_id"] == "golden_ambiguous_bds_missing_resource_context"
    )
    target_case["required_evidence_sources"] = ["impossible_without_action"]
    impossible_evidence = tmp_path / "invalid_zero_tool_evidence.json"
    impossible_evidence.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="cannot require paths or evidence sources"):
        load_golden_cases(impossible_evidence)


def test_ambiguous_case_rejects_root_causes_and_unsafe_stop_reason(tmp_path: Path) -> None:
    """验证模糊/证据不足类别不能预先允许根因，也必须声明安全停止语义。

    测试分别注入猜测根因和无关停止原因；Schema 应阻止这两种互相强化的错误标注，确保缺少资源
    上下文时评测鼓励请求用户补参，而不是为了 Top-1 分数编造结论或伪装为证据充分。
    """

    payload = json.loads(GOLDEN_CASE_FILE.read_text(encoding="utf-8"))
    target_case = next(
        case
        for case in payload
        if case["case_id"] == "golden_ambiguous_bds_missing_resource_context"
    )
    target_case["allowed_root_causes"] = ["无输入依据的猜测根因"]
    guessed_root = tmp_path / "invalid_ambiguous_root.json"
    guessed_root.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="cannot allow root causes"):
        load_golden_cases(guessed_root)

    payload = json.loads(GOLDEN_CASE_FILE.read_text(encoding="utf-8"))
    target_case = next(
        case
        for case in payload
        if case["case_id"] == "golden_ambiguous_bds_missing_resource_context"
    )
    target_case["expected_stop_reasons"] = ["evidence_sufficient"]
    unsafe_stop = tmp_path / "invalid_ambiguous_stop.json"
    unsafe_stop.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="requires a safe stop reason"):
        load_golden_cases(unsafe_stop)
