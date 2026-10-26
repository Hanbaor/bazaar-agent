# 清单项与用户要求的输入校验测试。
# pytest 按 test 前缀发现这类测试函数；参数化的一项输入算一个测试用例。

import pytest
from pydantic import ValidationError

from market.schemas import PurchaseItem, Requirement


# 同一个测试依次接收四种非法数量：零、负数、字符串和布尔值。
# 尤其 True 在 Python 中与整数有关联，strict=True 会拒绝这种输入。
@pytest.mark.parametrize("qty", [0, -1, "2", True])
def test_invaild_quantity_is_rejected(qty: object) -> None:
    # 预期缩进块抛出 ValidationError；若错误输入被放行，这个测试反而失败。
    with pytest.raises(ValidationError):
        PurchaseItem.model_validate(
            {
                "item_id": "cable",
                "need": "1米60W USB-C 数据线",
                "qty": qty,
            }
        )


def test_mandatory_requirement_can_need_evidence() -> None:
    # 固定项目的一条语义：“必须满足”和“需要证据判断”可以同时成立。
    requirement = Requirement(
        id="no_used",
        text="不要二手或翻新",
        must=True,
        check="evidence",
    )

    assert requirement.must is True
    assert requirement.check == "evidence"


def test_unknown_field_is_rejected() -> None:
    # 字段拼错或多传时应明确拒绝，不能悄悄忽略调用者以为生效的参数。
    with pytest.raises(ValidationError):
        PurchaseItem.model_validate(
            {
                "item_id": "cable",
                "need": "1 米 60W USB-C 数据线",
                "qty": 2,
                "unexpected_field": "多传的内容",
            }
        )
