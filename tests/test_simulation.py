# 测试市场状态实际变化，而不是仅检查数据模型能否创建。
from pathlib import Path

import pytest

from market.scenarios import load_scenario
from market.sim.engine import MarketSimulator, MarketTemplate, create_market, load_template

ROOT = Path(__file__).resolve().parents[1]


# 用途：读取指定的开发场景，为每个测试提供初始配置；默认使用正常采购场景。
def scenario(name="s01_basic"):
    return load_scenario(ROOT / "evals" / "scenarios" / "dev" / f"{name}.json")


# 用途：用指定任务和动作号提交一次台灯出价，返回模拟器的实际动作结果。
def offer(market, task="task_a", action="offer_a", price=12000):
    return market.run_action(
        task, action, "make_offer", {"listing_id": "lamp_a", "unit_price": price, "qty": 1}
    )


# 用途：验证固定价不会因买方多出钱而涨价，且创建报价只计时、不扣库存。
def test_fixed_price_accepts_without_charging_or_deducting_stock():
    market = create_market(scenario())
    result = offer(market, price=13000)
    assert result["data"]["quote"]["unit_price"] == 12000
    assert market.get_listing("lamp_a")["stock"] == 1
    assert market.sim_time == 545


# 用途：验证同动作重试不重复计时或创建报价，返回快照不污染缓存，换参数则拒绝。
def test_retry_does_not_repeat_time_or_quote_creation():
    market = create_market(scenario())
    first = offer(market)
    assert offer(market) == first
    assert market.sim_time == 545
    assert len(market._quotes) == 1
    # 修改返回快照也不能污染缓存。
    first["data"]["quote"]["unit_price"] = 1
    assert offer(market)["data"]["quote"]["unit_price"] == 12000
    conflict = offer(market, price=10000)
    assert conflict["error"]["code"] == "INVALID_ARGUMENT"


# 用途：验证 S02 在报价后断货一次，恢复库存后不会被同一个事件再次清空。
def test_stockout_happens_once_after_quote():
    market = create_market(scenario("s02_stockout"))
    quote = offer(market)["data"]["quote"]
    assert market.get_listing("lamp_a")["stock"] == 0
    assert market.get_quote("task_a", quote["quote_id"]).listing_version == 1
    market.set_stock("lamp_a", 1)
    offer(market, action="new_offer")
    assert market.get_listing("lamp_a")["stock"] == 1
    assert len(market.pending_events) == 1


# 用途：验证报价的任务归属、副本隔离、到期边界，以及条款变更导致的失效。
def test_quote_scope_copy_expiration_and_version():
    market = create_market(scenario())
    quote_id = offer(market)["data"]["quote"]["quote_id"]
    with pytest.raises(KeyError, match="NOT_FOUND"):
        market.get_quote("another_task", quote_id)
    returned = market.get_quote("task_a", quote_id)
    returned.unit_price = 1
    assert market.get_quote("task_a", quote_id).unit_price == 12000
    market.sim_time = returned.expires_at
    with pytest.raises(ValueError, match="QUOTE_EXPIRED"):
        market.validate_quote("task_a", quote_id, 1)
    market.sim_time = returned.created_at
    market.change_terms("lamp_a", 12500, 720)
    with pytest.raises(ValueError, match="QUOTE_EXPIRED"):
        market.validate_quote("task_a", quote_id, 1)


# 用途：验证相同配置和动作得到相同还价、让步不低于底价，并且不同任务分别计轮次。
def test_concession_is_deterministic_and_task_scoped():
    data = load_template("fixed_price_starter_v1").model_dump()
    data["pricing_mode"] = "concession"
    data["listings"][0].update(list_price=15000, reserve_price=12000, concession_step=1000)
    template = MarketTemplate.model_validate(data)
    first = MarketSimulator(scenario(), template)
    second = MarketSimulator(scenario(), template)
    results = []
    for number in range(1, 5):
        result = offer(first, action=f"round_{number}", price=0)
        assert result == offer(second, action=f"round_{number}", price=0)
        results.append(result["data"]["counter_price"])
    assert results == [14000, 13000, 12000, 12000]
    assert offer(first, task="new_task", price=0)["data"]["counter_price"] == 14000
