# HTTP 校验、Agent 工具描述和 MCP 转接共同使用这些模型。
# 任务身份、动作编号和授权凭据不属于模型能决定的业务参数。
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from market.sim.quotes import OfferRequest


# 类说明：工具参数的共同基类，禁止传入模型未声明的额外字段。
class Params(BaseModel):
    model_config = ConfigDict(extra="forbid")


# 类说明：搜索条件：关键词、类别和以分计的最高单价。
class SearchParams(Params):
    keyword: str = ""
    category: Literal["lamp", "cable", "organizer"] | None = None
    max_price: int | None = Field(default=None, ge=0, strict=True)


# 类说明：只携带目标挂牌编号的查询或内部干预参数。
class ListingParams(Params):
    listing_id: str = Field(min_length=1)


# 类说明：指定卖家和当前支持的定价政策主题。
class PolicyParams(Params):
    seller_id: str = Field(min_length=1)
    topic: Literal["pricing"] = "pricing"


# 类说明：指定卖家与待询问的问题，不携带身份或授权字段。
class SellerParams(Params):
    seller_id: str = Field(min_length=1)
    question: str = Field(min_length=1, max_length=1000)


# 类说明：指定采购清单项、服务端报价单和本次数量。
class OrderParams(Params):
    item_id: str = Field(min_length=1)
    quote_id: str = Field(min_length=1)
    qty: int = Field(gt=0, strict=True)


# 类说明：在下单业务内容之外补充审批理由，不包含模型自称的审批结果。
class ApprovalParams(OrderParams):
    reason: str = Field(min_length=1)


# 类说明：指定需要查证的原动作编号，用于恢复查询。
class OperationParams(Params):
    action_id: str = Field(min_length=1)


# 类说明：采购结束报告，包含声明的结局、理由、证据和实际订单编号。
class FinishParams(Params):
    outcome: Literal["completed", "stopped"]
    reason: str = Field(min_length=1)
    evidence_ids: list[str] = Field(default_factory=list)
    order_ids: list[str] = Field(default_factory=list)


# 明确的白名单防止客户端借工具名执行任意 Python 函数。
TOOL_MODELS: dict[str, type[BaseModel]] = {
    "search_listings": SearchParams,
    "get_listing": ListingParams,
    "get_policy": PolicyParams,
    "ask_seller": SellerParams,
    "make_offer": OfferRequest,
    "place_order": OrderParams,
    "request_approval": ApprovalParams,
    "get_operation": OperationParams,
    "list_task_orders": Params,
    "finish": FinishParams,
}

TOOL_DESCRIPTIONS = {
    "search_listings": "搜索公开挂牌。空条件返回全部候选；价格单位为分。",
    "get_listing": "查看商品公开规格、描述、库存和价格。",
    "get_policy": "查询卖家公开定价政策，取得证据编号。",
    "ask_seller": "询问卖家，取得公开回复和证据编号。",
    "make_offer": "按整数分出价。接受后得到报价单，尚未扣款。",
    "place_order": "用本任务报价单采购指定清单项。服务端核对预算和库存。",
    "request_approval": "申请单笔超预算审批；M1 不会自动批准。",
    "get_operation": "查证原动作结果，不消耗模拟时间。",
    "list_task_orders": "查询实际订单和已花金额，不消耗模拟时间。",
    "finish": "提交完成或停止报告，最终结果由独立验收确定。",
}


# 把确定执行成功的数据包装成统一结果，供 HTTP、MCP 和 Agent 使用。
def success(data: object) -> dict:
    # 外层表示动作确定执行；data 内的还价也是确定的业务结果。
    return {"status": "success", "data": data, "error": None}


# 用错误码和说明构造确定的业务拒绝；拒绝本身不会扣库存或产生订单。
def rejected(code: str, message: str = "") -> dict:
    return {"status": "rejected", "data": None, "error": {"code": code, "message": message}}
