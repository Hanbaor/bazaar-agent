# 这个模块把场景与模板变成一局运行中的市场，提供内部查询和库存修改方法。
# 当前状态保存在 Python 对象中；数据库事务和持久化写入将在 M1.3 接入。
# 当前查询方法本身不推进模拟时间，后续统一动作执行入口负责计时与去重。

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from market.scenarios import Scenario

# 查询结果采用字段白名单：只返回明确允许公开的内容。
# 底价 reserve_price 和真实成色 truth_condition 留在服务端，不能交给 Agent。
PUBLIC_LISTING_FIELDS = {
    "listing_id",
    "seller_id",
    "product_id",
    "title",
    "category",
    "description",
    "specs",
    "list_price",
    "stock",
    "ship_minutes",
    "version",
}


# 某卖家出售某商品的运行数据，同时包含公开信息和服务端内部信息。
class Listing(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        # 对象创建后再次赋值也做校验，例如 listing.stock = -1 会报错。
        validate_assignment=True,
    )

    listing_id: str = Field(min_length=1)
    seller_id: str = Field(min_length=1)
    product_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    category: Literal["lamp", "cable", "organizer"]
    description: str = Field(min_length=1)
    # 规格可包含文本、数字和布尔值，例如功率 60、接口 USB-C、可调光 True。
    specs: dict[str, str | int | bool]
    # 金额为整数分；库存为非负整数，True 和字符串数字都不能冒充整数。
    list_price: int = Field(ge=0, strict=True)
    stock: int = Field(ge=0, strict=True)
    # 发货时长为模拟分钟；720 即 12 小时。
    ship_minutes: int = Field(default=720, ge=0, strict=True)
    # 价格或条款变化时将递增版本，使旧报价失效；当前库存修改不改变它。
    version: int = Field(default=1, ge=1, strict=True)

    # 以下两项用于卖家策略与独立验收，公开查询不会导出它们。
    reserve_price: int = Field(ge=0, strict=True)

    truth_condition: Literal["new", "used", "refurbished"]


# 一份初始市场配置：模板名称、卖家编号到店名的映射、初始挂牌列表。
class MarketTemplate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    template_id: Literal["fixed_price_starter_v1"]
    seller_names: dict[str, str] = Field(min_length=1)
    listings: list[Listing] = Field(min_length=1)


class MarketSimulator:
    # self 表示当前这一局市场；不同对象分别保存自己的库存等运行状态。
    def __init__(self, scenario: Scenario, template: MarketTemplate) -> None:
        # 场景编号不是运行实例的全局唯一编号，同一场景可以创建多局市场。
        self.scenario_id = scenario.scenario_id
        # 固定价模板暂不引入随机价格，这里先保留种子和初始模拟时刻。
        self.seed = scenario.seed
        self.sim_time = scenario.initial_time
        # 映射中的值是字符串，普通 copy 即可让两局拥有独立的字典。
        self.seller_names = template.seller_names.copy()
        # 下划线表示“内部使用”的命名约定；按编号建字典，方便直接查找。
        self._listings: dict[str, Listing] = {}

        for listing in template.listings:
            # 以下是普通 Python 的关联检查，失败时直接抛出 ValueError。
            if listing.listing_id in self._listings:
                raise ValueError("挂牌编号不能重复")
            if listing.seller_id not in self.seller_names:
                raise ValueError("挂牌引用的卖家不存在")
            # 当前仅支持固定价模板。接入议价策略时需要按策略调整这项规则。
            if listing.reserve_price != listing.list_price:
                raise ValueError("固定价模板的底价必须等于标价")
            # 深复制也复制 specs 等嵌套数据，防止市场修改影响模板或其他实例。
            self._listings[listing.listing_id] = listing.model_copy(deep=True)

    def _find_listing(self, listing_id: str) -> Listing:
        # 内部查找返回当前市场持有的原始对象，set_stock 用它修改真实状态。
        if listing_id not in self._listings:
            raise KeyError("NOT_FOUND")
        return self._listings[listing_id]

    def get_listing(self, listing_id: str) -> dict[str, object]:
        listing = self._find_listing(listing_id)
        # 转成新的公开字典，避免把原始对象和内部字段直接交给调用者。
        return listing.model_dump(include=PUBLIC_LISTING_FIELDS)

    def search_listings(
        self,
        keyword: str = "",
        category: str | None = None,
        max_price: int | None = None,
    ) -> list[dict[str, object]]:
        # 去掉两端空白并忽略英文大小写；本轮是子串匹配，没有语义向量检索。
        keyword = keyword.strip().lower()
        results = []

        # values() 依次取出挂牌对象。continue 表示跳过当前挂牌，检查下一项。
        for listing in self._listings.values():
            # 当前只搜索标题和描述；结构化 specs 没有加入这段关键词匹配。
            searchable_text = f"{listing.title} {listing.description}".lower()
            if keyword and keyword not in searchable_text:
                continue
            # None 表示不限制类别；非 None 时要求类别相同。
            if category is not None and listing.category != category:
                continue
            # 这里限制单价。用 is not None，才能正确处理 max_price=0。
            if max_price is not None and listing.list_price > max_price:
                continue
            # 通过所有条件的候选才加入结果，导出时仍然遵守公开字段白名单。
            results.append(listing.model_dump(include=PUBLIC_LISTING_FIELDS))

        return results

    def set_stock(self, listing_id: str, stock: int) -> None:
        # 这是模拟器内部的状态修改入口，后续由服务端事件等逻辑调用。
        # Agent 的工具接口不会直接开放任意改库存的能力。
        listing = self._find_listing(listing_id)
        # Listing 开启了 validate_assignment，因此这里仍会拒绝负库存。
        listing.stock = stock


def create_market(scenario: Scenario) -> MarketSimulator:
    # 工厂函数：按场景中的模板名加载数据，再创建一局独立市场。
    # __file__ 是当前模块路径，使用它定位模板，可避免依赖终端当前目录。
    template_path = Path(__file__).parent / "templates" / f"{scenario.market}.json"
    # 模板包含完整内部数据，只在服务端解析；嵌套挂牌也会被 Pydantic 校验。
    template = MarketTemplate.model_validate_json(template_path.read_text(encoding="utf-8"))
    # 构造对象时 __init__ 会复制数据，因此每次调用得到独立的市场状态。
    return MarketSimulator(scenario, template)
