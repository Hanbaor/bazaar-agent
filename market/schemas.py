from decimal import Decimal
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PurchaseItem(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
    )

    item_id: str = Field(min_length=1)
    need: str = Field(min_length=1)
    qty: int = Field(gt=0, strict=True)


class Requirement(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
    )
    id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    must: bool = Field(strict=True)
    check: Literal["structured", "evidence"]


class ApprovalPolicy(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    over_budget: Literal["ask_user"] = "ask_user"
    max_over_ratio: Decimal = Field(
        default=Decimal("0.20"),
        ge=0,
        allow_inf_nan=False,
    )
    approval_ttl_min: int = Field(default=30, gt=0, strict=True)


class Mandate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(default=1, ge=1, strict=True)
    budget_total: int = Field(ge=0, strict=True)
    order_deadline: int = Field(ge=0, strict=True)
    delivery_deadline: int | None = Field(default=None, ge=0, strict=True)
    require_complete_set: bool = Field(default=True, strict=True)
    allow_partial_commit: bool = Field(default=True, strict=True)

    items: list[PurchaseItem] = Field(min_length=1)
    requirements: list[Requirement] = Field(default_factory=list)
    approval_policy: ApprovalPolicy = Field(default_factory=ApprovalPolicy)

    @model_validator(mode="after")
    def validate_contract(self) -> Self:
        item_ids = [item.item_id for item in self.items]
        if len(item_ids) != len(set(item_ids)):
            raise ValueError("采购清单中的 item_id 不能重复")

        requirement_ids = [requirement.id for requirement in self.requirements]
        if len(requirement_ids) != len(set(requirement_ids)):
            raise ValueError("用户要求中的 id 不能重复")

        if not self.require_complete_set or not self.allow_partial_commit:
            raise ValueError("首版要求买齐才算完成，并允许逐件下单")

        return self
