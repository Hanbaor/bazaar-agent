# 报价的数据结构。接受出价只生成报价，place_order 才成交和扣款。
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


# 类说明：买方提交的出价参数；单价单位为分，数量必须为正整数。
class OfferRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    listing_id: str = Field(min_length=1)
    unit_price: int = Field(ge=0, strict=True)  # 金额单位为整数分。
    qty: int = Field(gt=0, strict=True)


# 类说明：服务端生成的任务专属报价单，记录价格、数量、版本和有效期，不代表已经成交。
class Quote(BaseModel):
    model_config = ConfigDict(extra="forbid")
    quote_id: str
    task_id: str  # 报价只能被创建它的任务使用。
    listing_id: str
    listing_version: int = Field(ge=1, strict=True)
    unit_price: int = Field(ge=0, strict=True)
    qty: int = Field(gt=0, strict=True)
    created_at: int = Field(ge=0, strict=True)
    expires_at: int = Field(ge=0, strict=True)  # 绝对模拟时刻。
    used: bool = False


# 类说明：描述接受、还价或拒绝的报价业务数据结构；当前引擎实际对外返回统一字典结果。
class OfferResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["accepted", "counter_offer", "rejected"]
    quote: Quote | None = None
    counter_price: int | None = None
    error_code: str | None = None
