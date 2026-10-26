import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from market.scenarios import Scenario, load_scenario

SCENARIO_DIR = Path(__file__).resolve().parents[1] / "evals" / "scenarios" / "dev"
BASIC_PATH = SCENARIO_DIR / "s01_basic.json"


@pytest.mark.parametrize(
    "filename",
    [
        "s01_basic.json",
        "s02_stockout.json",
        "s03_low_budget.json",
    ],
)
def test_scenario_file_is_valid(filename: str) -> None:
    scenario = load_scenario(SCENARIO_DIR / filename)

    assert scenario.scenario_id == Path(filename).stem


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        (
            {
                "events": [
                    {
                        "event_id": "bad_event",
                        "kind": "stockout",
                        "trigger": "after_quote_created",
                        "listing_id": "missing",
                    }
                ]
            },
            "事件引用的挂牌不存在",
        ),
        (
            {
                "reference_trace": [
                    {
                        "item_id": "missing",
                        "listing_id": "lamp_a",
                        "qty": 1,
                    }
                ]
            },
            "参考步骤引用的清单项不存在",
        ),
        (
            {"reference_trace": []},
            "可行场景必须提供参考采购步骤",
        ),
    ],
)
def test_invalid_scenario_is_rejected(
    changes: dict[str, object],
    message: str,
) -> None:
    data = json.loads(BASIC_PATH.read_text(encoding="utf-8"))
    data.update(changes)

    with pytest.raises(ValidationError, match=message):
        Scenario.model_validate(data)
