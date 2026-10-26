# 这个文件定义“采购委托应该长什么样”，供场景、接口和后续 Agent 共用。
# 创建这些对象会校验输入数据；是否有货、能否买齐，由市场和验收逻辑判断。
# 金额使用整数分，时刻使用模拟分钟，避免单位不一致。

# Decimal 是十进制数类型；本文件用它表示审批比例，例如 Decimal("0.20")。
# Literal 限定可选值；Self 表示方法返回当前这种类型的对象。
from decimal import Decimal
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


# 一条采购需求。例如：item_id="cable"，need="1 米数据线"，qty=2。
# 清单项编号标识用户的需求，与市场上具体的挂牌编号是两个概念。
class PurchaseItem(BaseModel):
    model_config = ConfigDict(
        # 多传或拼错字段时立即报错，帮助尽早发现配置问题。
        extra="forbid",
        # " cable " 会先整理为 "cable"，再做字段校验。
        str_strip_whitespace=True,
    )

    item_id: str = Field(min_length=1)
    need: str = Field(min_length=1)
    # gt=0 要求正数；strict=True 拒绝字符串 "2"、布尔值 True 等输入。
    qty: int = Field(gt=0, strict=True)


# 一条采购要求：must 表示强度，check 表示核实方式，这两个维度独立。
# 例如“不要二手”仍然必须满足，但需要依据商品描述和卖家回答判断。
class Requirement(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
    )
    id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    # 这里记录用户要求，不会自动检查真实商品是否满足这条要求。
    must: bool = Field(strict=True)
    check: Literal["structured", "evidence"]


# 审批策略只规定“可以申请什么”，不是已经获得的花费许可。
# 真正的批准需要在服务端绑定某一笔确定的下单操作。
class ApprovalPolicy(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    over_budget: Literal["ask_user"] = "ask_user"
    # 默认可申请的累计超预算比例上限为 20%；使用十进制数避免浮点误差。
    max_over_ratio: Decimal = Field(
        default=Decimal("0.20"),
        ge=0,
        allow_inf_nan=False,  # 不接受无穷大、非数值等特殊数字。
    )
    # 这是审批申请的有效时长，单位为模拟分钟，与报价单有效期分开管理。
    approval_ttl_min: int = Field(default=30, gt=0, strict=True)


# 完整委托：把预算、时间、采购需求、要求和审批策略组合起来。
# 这是数据模型；后续持久化委托版本和检查用户权限由服务端负责。
class Mandate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # 版本将用于识别委托修改，使绑定旧版本的审批失效。
    version: int = Field(default=1, ge=1, strict=True)
    # 例如 500 元写为 50000；允许预算为 0，能否采购另由市场判断。
    budget_total: int = Field(ge=0, strict=True)
    # 绝对模拟时刻：D1 18:00 为 1080，而不是“从现在起还有 1080 分钟”。
    order_deadline: int = Field(ge=0, strict=True)
    # None 表示没有额外限制承诺送达的最迟时刻。
    delivery_deadline: int | None = Field(default=None, ge=0, strict=True)
    # 首版要求买齐才算完成，但允许逐件成交，可能留下部分已买的结果。
    require_complete_set: bool = Field(default=True, strict=True)
    allow_partial_commit: bool = Field(default=True, strict=True)

    # 嵌套模型会递归校验：其中某项 qty 不合法，整份委托也会校验失败。
    items: list[PurchaseItem] = Field(min_length=1)
    # 每次创建委托时，default_factory 都会生成一份新的默认数据。
    requirements: list[Requirement] = Field(default_factory=list)
    approval_policy: ApprovalPolicy = Field(default_factory=ApprovalPolicy)

    # Pydantic 在字段校验成功后自动调用这个方法，继续检查字段之间的关系。
    # 在校验器里抛出的 ValueError 会由 Pydantic 汇总成 ValidationError。
    @model_validator(mode="after")
    def validate_contract(self) -> Self:
        # 先取出全部编号，比较去重前后的数量；列表比集合长说明编号重复。
        item_ids = [item.item_id for item in self.items]
        if len(item_ids) != len(set(item_ids)):
            raise ValueError("采购清单中的 item_id 不能重复")

        # 要求编号同样需要唯一，后续证据和验收才能准确关联到某条要求。
        requirement_ids = [requirement.id for requirement in self.requirements]
        if len(requirement_ids) != len(set(requirement_ids)):
            raise ValueError("用户要求中的 id 不能重复")

        # 当前只支持这组采购语义，避免接受尚未实现的整篮原子采购等行为。
        if not self.require_complete_set or not self.allow_partial_commit:
            raise ValueError("首版要求买齐才算完成，并允许逐件下单")

        # 返回当前这份校验通过的委托对象，而不是返回 True。
        return self
