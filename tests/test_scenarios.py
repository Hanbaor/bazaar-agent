# 场景文件测试：确认 JSON 可以加载，并检查错误的跨对象引用会被拒绝。
# 这里验证配置，不会执行参考采购步骤，也不会发送模型请求。

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from market.scenarios import Scenario, load_scenario

# 从当前测试文件定位项目根目录，让本地和 GitHub CI 使用同一套相对位置。
SCENARIO_DIR = Path(__file__).resolve().parents[1] / "evals" / "scenarios" / "dev"
BASIC_PATH = SCENARIO_DIR / "s01_basic.json"


# 显式列出三个文件；少保存任何一个文件，都应让测试失败。
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

    # stem 去掉 .json，防止复制文件后忘记修改内部 scenario_id。
    assert scenario.scenario_id == Path(filename).stem


# changes 是替换内容，message 是预期的拒绝原因。
# 分别测试不存在的挂牌、委托外的清单项、缺少参考步骤的“预期完成”场景。
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
    # 每次重新读取正常样例，得到一份独立字典，再替换本次要测试的字段。
    data = json.loads(BASIC_PATH.read_text(encoding="utf-8"))
    data.update(changes)

    # match 除了要求异常类型正确，还要求异常文本包含对应的拒绝原因。
    with pytest.raises(ValidationError, match=message):
        Scenario.model_validate(data)
