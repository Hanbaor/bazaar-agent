# 场景是一局采购的配置：初始条件、用户委托、未来事件和参考结果。
# 本文件负责读取和校验配置。执行市场变化、下单和独立验收由后续模块完成。
# 完整场景包含测试答案和未来事件，只供服务端与测试使用，不能整体交给模型。

from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from market.schemas import Mandate


# 供下面几个配置模型继承的共同设置，减少重复配置。
class ConfigModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
    )


# 一次断货事件的声明。创建这个对象只是记录配置，还不会修改市场库存。
class StockoutEvent(ConfigModel):
    # 事件编号用于后续去重，使同一事件在一局中只触发一次。
    event_id: str = Field(min_length=1)
    kind: Literal["stockout"]
    trigger: Literal["after_quote_created"]
    listing_id: str = Field(min_length=1)


# 人工设计的参考采购尝试：后续重放器会执行查看、报价、下单等公开操作。
# 同一清单项可以先失败、再选择替代挂牌，不能把尝试次数直接当作成交数量。
class ReferencePurchase(ConfigModel):
    item_id: str = Field(min_length=1)
    listing_id: str = Field(min_length=1)
    qty: int = Field(gt=0, strict=True)
    expected_result: Literal["success", "OUT_OF_STOCK"] = "success"


# 这是场景作者填写的预期，不是程序已经验证出的实际成绩。
class ScenarioExpectation(ConfigModel):
    outcome: Literal["completed_within_budget", "correct_stop"]
    # 参考步骤的成交花费，单位为分；不要求 Agent 选择完全相同的合法方案。
    reference_spent: int = Field(ge=0, strict=True)


# 一整局配置。Pydantic 会把 JSON 中嵌套的字典转换成对应模型对象。
class Scenario(ConfigModel):
    scenario_id: str = Field(min_length=1)
    # 先保存种子，后续模拟器可用它控制本局的随机行为。
    seed: int = Field(ge=0, strict=True)
    # 540 表示第一天 09:00；这是模拟时刻，不是电脑当前时间。
    initial_time: int = Field(ge=0, strict=True)

    # 这里是市场模板的名称字符串，不是运行中的 MarketSimulator 对象。
    market: Literal["fixed_price_starter_v1"]
    mandate: Mandate
    # default_factory=list 为每个场景生成独立空列表，避免共享可变默认数据。
    events: list[StockoutEvent] = Field(default_factory=list)
    # 模拟用户的规则声明；真正的审批处理会在后续模块接上。
    user_policy: Literal["reject_over_budget"] = "reject_over_budget"
    expected: ScenarioExpectation
    # 当前只检查列表非空。来源是否可查询、内容是否充分，还需要运行验证。
    evidence_paths: list[str] = Field(min_length=1)
    reference_trace: list[ReferencePurchase] = Field(default_factory=list)

    # after 表示字段已通过校验，此时可以使用 self.mandate.items 等对象属性。
    @model_validator(mode="after")
    def validate_references(self) -> Self:
        # 这是固定开发模板的已知编号集合，存在性目前按这个集合判断。
        # 后续扩展模板时，需要让这里与实际目录保持一致。
        listing_ids = {"lamp_a", "lamp_b", "cable_a", "organizer_a"}
        # 集合推导式：依次取出委托中每个清单项的 item_id。
        item_ids = {item.item_id for item in self.mandate.items}

        # 列表保留重复项，set 会去重，因此可用长度差异发现重复事件编号。
        event_ids = [event.event_id for event in self.events]
        if len(event_ids) != len(set(event_ids)):
            raise ValueError("事件编号不能重复")

        # 字符串类型合法，不代表引用目标存在；这里检查事件指向的挂牌。
        for event in self.events:
            if event.listing_id not in listing_ids:
                raise ValueError("事件引用的挂牌不存在")

        # 参考步骤必须同时指向本模板的挂牌和本次委托的清单项。
        for step in self.reference_trace:
            if step.listing_id not in listing_ids:
                raise ValueError("参考步骤引用的挂牌不存在")
            if step.item_id not in item_ids:
                raise ValueError("参考步骤引用的清单项不存在")

        # 空列表在条件判断中为假。预期能完成的场景必须提供参考步骤。
        # 这只是检查作者是否提供方案，方案能否执行成功还要由重放验证。
        if self.expected.outcome == "completed_within_budget" and not self.reference_trace:
            raise ValueError("可行场景必须提供参考采购步骤")

        return self


def load_scenario(path: Path) -> Scenario:
    # Path 表示文件路径，read_text 读取 UTF-8 文本，支持场景里的中文。
    content = path.read_text(encoding="utf-8")
    # 解析 JSON、校验字段和关联，成功后返回具有类型信息的 Scenario 对象。
    return Scenario.model_validate_json(content)
