import json
import shutil
from pathlib import Path

import pytest

from app.core.fixture_registry import FixtureRegistry, load_golden_cases
from app.domain.tooling import ToolErrorCode

FIXTURE_DIRECTORY = Path("data/fixtures/scenarios")
GOLDEN_CASE_FILE = Path("data/fixtures/golden_cases.json")


def test_all_scenarios_load_and_match_golden_cases() -> None:
    registry = FixtureRegistry.from_directory(FIXTURE_DIRECTORY)
    golden_cases = load_golden_cases(GOLDEN_CASE_FILE)

    assert len(registry) == 5
    assert len(golden_cases) == 5
    assert {case.scenario_id for case in golden_cases} == set(registry.scenario_ids)


def test_main_scenario_exercises_all_nine_tool_contracts() -> None:
    scenario = FixtureRegistry.from_directory(FIXTURE_DIRECTORY).get("cross_chain_pk_conflict")
    assert len({result.tool_name for result in scenario.tool_results}) == 9


def test_failure_scenarios_cover_required_error_classes() -> None:
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
    source = FIXTURE_DIRECTORY / "lts_empty_result.json"
    shutil.copyfile(source, tmp_path / "first.json")
    shutil.copyfile(source, tmp_path / "second.json")

    with pytest.raises(ValueError, match="duplicate scenario_id"):
        FixtureRegistry.from_directory(tmp_path)


def test_fixture_scenario_id_must_match_tool_request(tmp_path: Path) -> None:
    source = FIXTURE_DIRECTORY / "lts_empty_result.json"
    payload = json.loads(source.read_text(encoding="utf-8"))
    payload["tool_results"][0]["request"]["scenario_id"] = "different_scenario"
    target = tmp_path / "invalid.json"
    target.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="scenario_id must match"):
        FixtureRegistry.from_directory(tmp_path)
