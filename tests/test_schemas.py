import pytest
from pydantic import ValidationError

from market.schemas import PurchaseItem, Requirement


@pytest.mark.parametrize("qty", [0, -1, "2", True])
def test_invaild_quantity_is_rejected(qty: object) -> None:
    with pytest.raises(ValidationError):
        PurchaseItem.model_validate(
            {
                "item_id": "cable",
                "need": "1米60W USB-C 数据线",
                "qty": qty,
            }
        )


def test_mandatory_requirement_can_need_evidence() -> None:
    requirement = Requirement(
        id="no_used",
        text="不要二手或翻新",
        must=True,
        check="evidence",
    )

    assert requirement.must is True
    assert requirement.check == "evidence"


def test_unknown_field_is_rejected() -> None:
    with pytest.raises(ValidationError):
        PurchaseItem.model_validate(
            {
                "item_id": "cable",
                "need": "1 米 60W USB-C 数据线",
                "qty": 2,
                "unexpected_field": "多传的内容",
            }
        )
