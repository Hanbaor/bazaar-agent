from copy import deepcopy
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from market.scenarios import Scenario
from market.sim.quotes import OfferRequest, Quote
from market.tools import TOOL_MODELS, rejected, success

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
# 每个新动作先消耗模拟分钟；重复动作不再次计时。
ACTION_COST = {
    "search_listings": 1,
    "get_listing": 1,
    "get_policy": 1,
    "ask_seller": 5,
    "make_offer": 5,
    "place_order": 1,
    "request_approval": 1,
}


# 类说明：单条完整挂牌，包含公开商品信息和服务端内部策略；再次赋值也执行校验。
class Listing(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    listing_id: str = Field(min_length=1)
    seller_id: str = Field(min_length=1)
    product_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    category: Literal["lamp", "cable", "organizer"]
    description: str = Field(min_length=1)
    specs: dict[str, str | int | bool]
    list_price: int = Field(ge=0, strict=True)  # 整数分。
    stock: int = Field(ge=0, strict=True)
    ship_minutes: int = Field(default=720, ge=0, strict=True)
    version: int = Field(default=1, ge=1, strict=True)
    reserve_price: int = Field(ge=0, strict=True)  # 隐藏卖家底价。
    truth_condition: Literal["new", "used", "refurbished"]  # 只供验收使用。
    concession_step: int = Field(default=0, ge=0, strict=True)


# 类说明：一份市场初始配置，描述卖家、挂牌和报价策略；创建实例时复制其数据。
class MarketTemplate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    template_id: str
    seller_names: dict[str, str] = Field(min_length=1)
    listings: list[Listing] = Field(min_length=1)
    pricing_mode: Literal["fixed", "concession"] = "fixed"
    quote_ttl_min: int = Field(default=30, gt=0, strict=True)


# 用途：按服务端指定的模板名读取 JSON 并校验，返回包含卖家和完整挂牌的模板对象。
def load_template(name: str) -> MarketTemplate:
    # 模板路径由服务端场景决定，接口不接受任意文件路径。
    path = Path(__file__).parent / "templates" / f"{name}.json"
    return MarketTemplate.model_validate_json(path.read_text(encoding="utf-8"))


# 类说明：一局市场的内存运行对象，负责确定性查询、议价、计时和事件。
class MarketSimulator:
    # 用途：初始化当前这一局市场；self 指当前对象，挂牌深复制后与模板及其他实例隔离。
    # 保存模拟时刻、报价、议价轮次和事件状态；当前确定性策略只保存种子，不使用随机价格。
    def __init__(self, scenario: Scenario, template: MarketTemplate) -> None:
        self.scenario_id = scenario.scenario_id
        self.seed = scenario.seed  # 当前策略无随机数，保留种子供后续扩展。
        self.sim_time = scenario.initial_time
        self.seller_names = template.seller_names.copy()
        self.pricing_mode = template.pricing_mode
        self.quote_ttl_min = template.quote_ttl_min
        self._listings: dict[str, Listing] = {}
        for listing in template.listings:
            if listing.listing_id in self._listings:
                raise ValueError("挂牌编号不能重复")
            if listing.seller_id not in self.seller_names:
                raise ValueError("挂牌引用的卖家不存在")
            if listing.reserve_price > listing.list_price:
                raise ValueError("底价不能超过标价")
            if self.pricing_mode == "fixed" and (
                listing.reserve_price != listing.list_price or listing.concession_step != 0
            ):
                raise ValueError("固定价模板必须保持固定价格")
            self._listings[listing.listing_id] = listing.model_copy(deep=True)
        self._quotes: dict[str, Quote] = {}
        self._quote_seq = 0
        # 嵌套字典保存各任务各挂牌的议价轮次，可以直接序列化成 JSON。
        self._rounds: dict[str, dict[str, int]] = {}
        self._events = [event.model_dump() for event in scenario.events]
        self._triggered: set[str] = set()
        self.pending_events: list[dict] = []
        self._actions: dict[tuple[str, str], tuple[dict, dict]] = {}

    # 用途：按挂牌编号返回内部原始对象，供业务修改使用；找不到时抛出 NOT_FOUND。
    def _find_listing(self, listing_id: str) -> Listing:
        if listing_id not in self._listings:
            raise KeyError("NOT_FOUND")
        return self._listings[listing_id]

    # 用途：按编号导出一份公开挂牌快照，隐藏底价和真值，不向调用者暴露可修改的原对象。
    def get_listing(self, listing_id: str) -> dict:
        return self._find_listing(listing_id).model_dump(include=PUBLIC_LISTING_FIELDS)

    # 用途：按关键词、类别和最高单价筛选公开挂牌；最高单价单位为分，None 表示不限。
    def search_listings(
        self, keyword: str = "", category: str | None = None, max_price: int | None = None
    ) -> list[dict]:
        keyword = keyword.strip().lower()
        results = []
        for listing in self._listings.values():
            if keyword and keyword not in f"{listing.title} {listing.description}".lower():
                continue
            if category is not None and listing.category != category:
                continue
            if max_price is not None and listing.list_price > max_price:
                continue
            results.append(listing.model_dump(include=PUBLIC_LISTING_FIELDS))
        return results

    # 用途：设置一件挂牌的库存，通过赋值校验拒绝负数；库存变化不递增价格和条款版本。
    def set_stock(self, listing_id: str, stock: int) -> None:
        # 库存变化不影响价格/条款版本，下单时仍需重新检查库存。
        self._find_listing(listing_id).stock = stock

    # 用途：校验并修改标价（分）与发货时长（模拟分钟）；条款实际变化才递增版本，使旧报价失效。
    def change_terms(self, listing_id: str, price: int, ship_minutes: int) -> None:
        listing = self._find_listing(listing_id)
        # 先校验新对象，失败时不留下只改了一半的状态。
        values = listing.model_dump()
        values.update(list_price=price, ship_minutes=ship_minutes)
        if self.pricing_mode == "fixed":
            values["reserve_price"] = price
        candidate = Listing.model_validate(values)
        if candidate.list_price < candidate.reserve_price:
            raise ValueError("价格不能低于底价")
        if price != listing.list_price or ship_minutes != listing.ship_minutes:
            candidate.version += 1
        self._listings[listing_id] = candidate

    # 用途：查询卖家公开定价政策和证据编号；它说明交易规则，不会公开隐藏底价。
    def get_policy(self, seller_id: str, topic: str = "pricing") -> dict:
        if seller_id not in self.seller_names or topic != "pricing":
            raise KeyError("NOT_FOUND")
        text = "固定价格，不议价" if self.pricing_mode == "fixed" else "可多轮出价，以卖家回复为准"
        return {
            "seller_id": seller_id,
            "topic": topic,
            "text": text,
            "evidence_id": f"get_policy:{seller_id}:pricing",
        }

    # 用途：根据卖家的公开商品描述回答问题并附证据编号；回复属于卖家说法。
    def ask_seller(self, seller_id: str, question: str) -> dict:
        if seller_id not in self.seller_names:
            raise KeyError("NOT_FOUND")
        # 回复来自公开描述，不读取隐藏成色或卖家底价。
        descriptions = [x.description for x in self._listings.values() if x.seller_id == seller_id]
        return {
            "seller_id": seller_id,
            "question": question,
            "reply": "；".join(descriptions) or "当前没有在售商品。",
            "evidence_id": f"ask_seller:{seller_id}",
        }

    # 用途：处理某任务的一次出价：查库存、计算本轮要价、返回还价或创建报价单。
    # 金额单位为分；接受报价不扣款、不扣库存，但可能立即触发场景声明的一次断货。
    def make_offer(self, task_id: str, request: OfferRequest) -> dict:
        listing = self._find_listing(request.listing_id)
        if request.qty > listing.stock:
            return rejected("OUT_OF_STOCK", "库存不足")
        rounds = self._rounds.setdefault(task_id, {})
        number = rounds.get(listing.listing_id, 0) + 1
        rounds[listing.listing_id] = number
        ask = max(listing.reserve_price, listing.list_price - listing.concession_step * number)
        if request.unit_price < ask:
            return success({"status": "counter_offer", "counter_price": ask})
        self._quote_seq += 1
        quote = Quote(
            quote_id=f"q_{self._quote_seq}",
            task_id=task_id,
            listing_id=listing.listing_id,
            listing_version=listing.version,
            unit_price=ask,
            qty=request.qty,
            created_at=self.sim_time,
            expires_at=self.sim_time + self.quote_ttl_min,
        )
        self._quotes[quote.quote_id] = quote
        # 创建报价后才触发断货；下单因此得到库存错误而非版本错误。
        for event in self._events:
            if (
                event["event_id"] not in self._triggered
                and event["listing_id"] == listing.listing_id
            ):
                self.set_stock(listing.listing_id, 0)
                self._triggered.add(event["event_id"])
                self.pending_events.append({"type": "stockout", "listing_id": listing.listing_id})
        return success({"status": "accepted", "quote": quote.model_dump()})

    # 用途：读取属于指定任务的报价副本；报价不存在或属于其他任务时统一返回 NOT_FOUND。
    def get_quote(self, task_id: str, quote_id: str) -> Quote:
        quote = self._quotes.get(quote_id)
        if quote is None or quote.task_id != task_id:
            raise KeyError("NOT_FOUND")
        return quote.model_copy(deep=True)

    # 用途：检查报价归属、使用状态、有效期、挂牌版本和数量，成功返回报价副本。
    # 此处尚未检查预算及实时库存；这些条件由下单事务再次检查。
    def validate_quote(self, task_id: str, quote_id: str, qty: int) -> Quote:
        quote = self.get_quote(task_id, quote_id)
        listing = self._find_listing(quote.listing_id)
        if (
            quote.used
            or self.sim_time >= quote.expires_at
            or quote.listing_version != listing.version
        ):
            raise ValueError("QUOTE_EXPIRED")
        if quote.qty != qty:
            raise ValueError("MANDATE_VIOLATION")
        return quote

    # 用途：根据动作名称累加模拟分钟；恢复查询没有配置耗时，因此不推进时钟。
    def advance(self, name: str) -> None:
        self.sim_time += ACTION_COST.get(name, 0)

    # 用途：校验一个新动作的参数，推进一次时钟，再分派到查询或报价业务并包装结果。
    def perform(self, task_id: str, name: str, arguments: dict) -> dict:
        # 参数校验先于计时，task_id 已由受信任调用方确定。
        params = TOOL_MODELS[name].model_validate(arguments).model_dump()
        self.advance(name)
        try:
            if name == "make_offer":
                return self.make_offer(task_id, OfferRequest.model_validate(params))
            method = {
                "search_listings": self.search_listings,
                "get_listing": self.get_listing,
                "get_policy": self.get_policy,
                "ask_seller": self.ask_seller,
            }[name]
            return success(method(**params))
        except KeyError:
            return rejected("NOT_FOUND", "请求对象不存在或不可见")

    # 用途：执行内存版动作去重：相同任务、动作号和参数复用原结果，内容冲突则拒绝。
    # 保存与返回均复制结果，避免外部修改缓存；数据库服务改用持久化动作表去重。
    def run_action(self, task_id: str, action_id: str, name: str, arguments: dict) -> dict:
        # 内存版去重；持久化服务使用数据库动作表实现同样规则。
        request = {
            "tool": name,
            "arguments": TOOL_MODELS[name].model_validate(arguments).model_dump(),
        }
        key = (task_id, action_id)
        if key in self._actions:
            previous, result = self._actions[key]
            return (
                deepcopy(result)
                if previous == request
                else rejected("INVALID_ARGUMENT", "动作号内容冲突")
            )
        result = self.perform(task_id, name, request["arguments"])
        self._actions[key] = (deepcopy(request), deepcopy(result))
        return deepcopy(result)

    # 用途：导出可保存为 JSON 的轮次、报价序号和已触发事件；库存与报价记录另存。
    def runtime_dump(self) -> dict:
        # 库存和报价另有数据库记录，不在这里保存第二份事实来源。
        return {
            "quote_seq": self._quote_seq,
            "rounds": deepcopy(self._rounds),
            "triggered": sorted(self._triggered),
        }

    # 用途：用持久化的运行状态、报价记录和模拟时刻恢复对象，继续已有市场而非重置一局。
    def restore_runtime(self, runtime: dict, quotes: list[dict], sim_time: int) -> None:
        self.sim_time = sim_time
        self._quote_seq = runtime.get("quote_seq", 0)
        self._rounds = deepcopy(runtime.get("rounds", {}))
        self._triggered = set(runtime.get("triggered", []))
        self._quotes = {q["quote_id"]: Quote.model_validate(q) for q in quotes}


# 用途：根据场景指定的模板创建一局新的独立市场，供初始化和纯模拟器测试使用。
def create_market(scenario: Scenario) -> MarketSimulator:
    return MarketSimulator(scenario, load_template(scenario.market))
