import pytest
from pydantic import ValidationError

from market.schemas import Mandate


def make_mandate_data() -> dict[str, object]:
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
    mandate = Mandate.model_validate(make_mandate_data())

    assert len(mandate.items) == 3
    assert mandate.items[1].qty == 2


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

    with pytest.raises(ValidationError):
        Mandate.model_validate(data)


def test_duplicate_item_ids_are_rejected() -> None:
    data = make_mandate_data()
    data["items"] = [
        {"item_id": "cable", "need": "USB-C 数据线", "qty": 1},
        {"item_id": "cable", "need": "USB-C 数据线", "qty": 2},
    ]

    with pytest.raises(ValidationError):
        Mandate.model_validate(data)


def test_duplicate_requirement_ids_are_rejected() -> None:
    data = make_mandate_data()
    data["requirements"] = [
        {"id": "condition", "text": "不要二手", "must": True, "check": "evidence"},
        {"id": "condition", "text": "不要翻新", "must": True, "check": "evidence"},
    ]

    with pytest.raises(ValidationError):
        Mandate.model_validate(data)
