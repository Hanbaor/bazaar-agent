# 完整委托测试：先构造一份正常样例，再逐项破坏它，确认对应规则会拦截。
# 这些测试检查委托格式，不判断这笔采购在真实市场中是否可行。

import pytest
from pydantic import ValidationError

from market.schemas import Mandate


def make_mandate_data() -> dict[str, object]:
    # 每次调用都创建新字典，避免某个测试修改数据后影响其他测试。
    # 预算 50000 分；1080 是 D1 18:00；2160 是 D2 12:00。
    return {
        "budget_total": 50000,
        "order_deadline": 1080,
        "delivery_deadline": 2160,
        "items": [
            {"item_id": "lamp", "need": "可调光 LED 台灯", "qty": 1},
            {"item_id": "cable", "need": "1 米 60W USB-C 数据线", "qty": 2},
            {"item_id": "organizer", "need": "桌面收纳盒", "qty": 1},
        ],
        "requirements": [
            {
                "id": "no_used",
                "text": "不要二手或翻新",
                "must": True,
                "check": "evidence",
            }
        ],
    }


def test_valid_mandate_is_accepted() -> None:
    # model_validate 解析字典，并把嵌套清单转换为 PurchaseItem 对象。
    mandate = Mandate.model_validate(make_mandate_data())

    # 三类商品共四件；数据线这一清单项的需求数量是两根。
    assert len(mandate.items) == 3
    assert mandate.items[1].qty == 2


# 参数名 field 和 value 会由 pytest 注入下面的测试函数。
# 每组参数只替换正常样例中的一个字段，便于定位是哪条规则没有拦截。
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("budget_total", -1),
        ("budget_total", 500.0),
        ("order_deadline", -1),
        ("items", []),
        ("require_complete_set", False),
        ("allow_partial_commit", False),
        ("approval_policy", {"max_over_ratio": "-0.1"}),
        ("approval_policy", {"approval_ttl_min": 0}),
    ],
)
def test_invalid_mandate_is_rejected(field: str, value: object) -> None:
    data = make_mandate_data()
    data[field] = value

    # 预期错误必须出现，测试才通过；普通运行中，这类异常由接口层处理。
    with pytest.raises(ValidationError):
        Mandate.model_validate(data)


def test_duplicate_item_ids_are_rejected() -> None:
    # 即使两份需求内容不同，也不能用同一个 item_id，否则订单无法准确关联。
    data = make_mandate_data()
    data["items"] = [
        {"item_id": "cable", "need": "USB-C 数据线", "qty": 1},
        {"item_id": "cable", "need": "USB-C 数据线", "qty": 2},
    ]

    with pytest.raises(ValidationError):
        Mandate.model_validate(data)


def test_duplicate_requirement_ids_are_rejected() -> None:
    # 防止同一个要求编号同时指向两条要求，影响证据和验收结果的对应关系。
    data = make_mandate_data()
    data["requirements"] = [
        {"id": "condition", "text": "不要二手", "must": True, "check": "evidence"},
        {"id": "condition", "text": "不要翻新", "must": True, "check": "evidence"},
    ]

    with pytest.raises(ValidationError):
        Mandate.model_validate(data)
