from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from market.schemas import Mandate


class ConfigModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
    )


class StockoutEvent(ConfigModel):
    event_id: str = Field(min_length=1)
    kind: Literal["stockout"]
    trigger: Literal["after_quote_created"]
    listing_id: str = Field(min_length=1)


class ReferencePurchase(ConfigModel):
    item_id: str = Field(min_length=1)
    listing_id: str = Field(min_length=1)
    qty: int = Field(gt=0, strict=True)
    expected_result: Literal["success", "OUT_OF_STOCK"] = "success"


class ScenarioExpectation(ConfigModel):
    outcome: Literal["completed_within_budget", "correct_stop"]
    reference_spent: int = Field(ge=0, strict=True)


class Scenario(ConfigModel):
    scenario_id: str = Field(min_length=1)
    seed: int = Field(ge=0, strict=True)
    initial_time: int = Field(ge=0, strict=True)

    market: Literal["fixed_price_starter_v1"]
    mandate: Mandate
    events: list[StockoutEvent] = Field(default_factory=list)
    user_policy: Literal["reject_over_budget"] = "reject_over_budget"
    expected: ScenarioExpectation
    evidence_paths: list[str] = Field(min_length=1)
    reference_trace: list[ReferencePurchase] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_references(self) -> Self:
        listing_ids = {"lamp_a", "lamp_b", "cable_a", "organizer_a"}
        item_ids = {item.item_id for item in self.mandate.items}

        event_ids = [event.event_id for event in self.events]
        if len(event_ids) != len(set(event_ids)):
            raise ValueError("事件编号不能重复")

        for event in self.events:
            if event.listing_id not in listing_ids:
                raise ValueError("事件引用的挂牌不存在")

        for step in self.reference_trace:
            if step.listing_id not in listing_ids:
                raise ValueError("参考步骤引用的挂牌不存在")
            if step.item_id not in item_ids:
                raise ValueError("参考步骤引用的清单项不存在")

        if self.expected.outcome == "completed_within_budget" and not self.reference_trace:
            raise ValueError("可行场景必须提供参考采购步骤")

        return self


def load_scenario(path: Path) -> Scenario:
    content = path.read_text(encoding="utf-8")
    return Scenario.model_validate_json(content)
