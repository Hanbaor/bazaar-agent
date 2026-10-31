# 测试采购事务的不变量、回滚、任务隔离和真实数据库并发。
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import select

from market.ledger.models import InstanceRow, ProductRow
from market.ledger.service import AccessDenied, MarketService
from market.scenarios import load_scenario
from scripts.replay_reference import replay

ROOT = Path(__file__).resolve().parents[1]


# 用途：读取一份开发场景对象，供当前测试按需设置预算或期限。
def scene(name="s01_basic"):
    return load_scenario(ROOT / "evals/scenarios/dev" / f"{name}.json")


# 用途：带测试任务的 Agent 令牌执行工具；未指定动作号时为新业务动作生成 UUID。
def call(service, task, tool, arguments, action_id=None):
    return service.execute(
        task["task_id"], task["agent_token"], action_id or str(uuid4()), tool, arguments
    )


# 用途：先读取公开标价，再按该标价出价，返回实际生成的报价编号。
def quote(service, task, listing_id="lamp_a", qty=1):
    listing = call(service, task, "get_listing", {"listing_id": listing_id})["data"]
    result = call(
        service,
        task,
        "make_offer",
        {"listing_id": listing_id, "unit_price": listing["list_price"], "qty": qty},
    )
    return result["data"]["quote"]["quote_id"]


# 用途：用指定清单项、数量和报价提交下单，返回服务端确定结果。
def buy(service, task, quote_id, item_id="lamp", qty=1, action_id=None):
    return call(
        service,
        task,
        "place_order",
        {"item_id": item_id, "quote_id": quote_id, "qty": qty},
        action_id,
    )


@pytest.mark.parametrize("name", ["s01_basic", "s02_stockout", "s03_low_budget"])
# 用途：逐一重放三个开发场景，验证正常完成、断货替换和预算不足停止的独立验收。
def test_reference_scenarios(database, name):
    assert replay(MarketService(database), name)["verdict"]["passed"]


# 用途：验证重复下单只生成一张订单和一笔扣款，不多计时；同编号换内容必须拒绝。
def test_retry_has_one_order_one_charge_and_no_extra_time(database):
    service = MarketService(database)
    task = service.create_task(scene())
    quote_id = quote(service, task)
    action_id = str(uuid4())
    first = buy(service, task, quote_id, action_id=action_id)
    before = service.snapshot(task["task_id"], task["agent_token"])
    assert buy(service, task, quote_id, action_id=action_id) == first
    after = service.snapshot(task["task_id"], task["agent_token"])
    assert after == before
    assert len(after["orders"]) == len(after["ledger"]) == 1
    assert after["spent"] == 12000
    conflict = call(
        service, task, "place_order", {"item_id": "lamp", "quote_id": quote_id, "qty": 2}, action_id
    )
    assert conflict["error"]["code"] == "INVALID_ARGUMENT"


# 用途：验证报价后断货会拒绝下单，任务花费、订单和账本均保持为空。
def test_stockout_failure_does_not_charge(database):
    service = MarketService(database)
    task = service.create_task(scene("s02_stockout"))
    result = buy(service, task, quote(service, task))
    assert result["error"]["code"] == "OUT_OF_STOCK"
    state = service.snapshot(task["task_id"], task["agent_token"])
    assert state["spent"] == 0 and state["orders"] == [] and state["ledger"] == []


# 用途：验证跨任务访问、跨任务报价、非法数量和超出累计需求的采购均被拒绝。
def test_scope_invalid_input_and_quantity_limit(database):
    service = MarketService(database)
    first = service.create_task(scene())
    second = service.create_task(scene())
    with pytest.raises(AccessDenied):
        service.snapshot(first["task_id"], second["agent_token"])
    quote_id = quote(service, first)
    assert buy(service, second, quote_id)["error"]["code"] == "NOT_FOUND"
    bad = call(
        service, first, "place_order", {"item_id": "lamp", "quote_id": quote_id, "qty": True}
    )
    assert bad["error"]["code"] == "INVALID_ARGUMENT"
    assert buy(service, first, quote_id)["status"] == "success"
    another = quote(service, first, "lamp_b")
    assert buy(service, first, another)["error"]["code"] == "MANDATE_VIOLATION"


# 用途：验证审批等待、超审批上限、报价到期、下单期限和承诺送达期限的检查。
def test_budget_deadline_and_quote_expiration(database):
    service = MarketService(database)
    task = service.create_task(scene("s03_low_budget"))
    result = buy(service, task, quote(service, task))
    assert result["status"] == "approval_required"
    assert service.snapshot(task["task_id"], task["agent_token"])["spent"] == 0
    assert (
        buy(service, task, quote(service, task, "lamp_b"))["error"]["code"] == "OVER_POLICY_LIMIT"
    )
    task = service.create_task(scene())
    quote_id = quote(service, task)
    with database.sessions.begin() as session:
        instance = session.get(InstanceRow, task["instance_id"])
        instance.sim_time += 30
    assert buy(service, task, quote_id)["error"]["code"] == "QUOTE_EXPIRED"
    limited = scene()
    limited.mandate.order_deadline = limited.initial_time
    task = service.create_task(limited)
    assert buy(service, task, quote(service, task))["error"]["code"] == "MANDATE_VIOLATION"
    delivery_limited = scene()
    delivery_limited.mandate.delivery_deadline = delivery_limited.initial_time + 719
    task = service.create_task(delivery_limited)
    assert buy(service, task, quote(service, task))["error"]["code"] == "MANDATE_VIOLATION"


# 用途：验证提交前发生异常时库存、订单、扣款和动作记录一起回滚，原动作号仍可安全重试。
def test_exception_rolls_back_stock_order_charge_and_action(database):
    service = MarketService(database)
    task = service.create_task(scene())
    quote_id = quote(service, task)
    action_id = str(uuid4())

    # 用途：主动抛出提交前故障，让测试观察整个采购事务是否完整回滚。
    def crash():
        raise RuntimeError("模拟提交前故障")

    service.before_commit = crash
    with pytest.raises(RuntimeError, match="提交前故障"):
        buy(service, task, quote_id, action_id=action_id)
    service.before_commit = None
    state = service.snapshot(task["task_id"], task["agent_token"])
    assert state["spent"] == 0 and state["orders"] == []
    assert not service.get_operation(task["task_id"], task["agent_token"], action_id)["data"][
        "known"
    ]
    # 继续沿用原动作号，回滚后仍能成功成交一次。
    assert buy(service, task, quote_id, action_id=action_id)["status"] == "success"


# 用途：让两个独立任务并发抢同实例最后一件商品，验证只能有一笔成交。
def test_two_sessions_compete_for_last_stock(database):
    service = MarketService(database)
    first = service.create_task(scene())
    second = service.create_task(scene(), instance_id=first["instance_id"])
    first_quote, second_quote = quote(service, first), quote(service, second)
    barrier = Barrier(2)

    # 用途：等待另一线程到达屏障，再用独立数据库会话提交当前任务的下单请求。
    def compete(task, quote_id):
        barrier.wait(timeout=10)
        return buy(service, task, quote_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(compete, first, first_quote),
            pool.submit(compete, second, second_quote),
        ]
        results = [future.result(timeout=20) for future in futures]
    assert sum(result["status"] == "success" for result in results) == 1
    rejected = [result for result in results if result["status"] != "success"][0]
    assert rejected["error"]["code"] == "OUT_OF_STOCK"


# 用途：让同一任务并发购买两件不同商品，验证任务锁阻止合计花费突破预算。
def test_same_task_concurrency_cannot_exceed_budget(database):
    service = MarketService(database)
    configuration = scene()
    configuration.mandate.budget_total = 14000
    task = service.create_task(configuration)
    lamp = quote(service, task)
    organizer = quote(service, task, "organizer_a")
    barrier = Barrier(2)

    # 用途：在同步屏障后为指定清单项并发下单，制造预算检查的竞争条件。
    def compete(quote_id, item_id):
        barrier.wait(timeout=10)
        return buy(service, task, quote_id, item_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(compete, lamp, "lamp"), pool.submit(compete, organizer, "organizer")]
        results = [future.result(timeout=20) for future in futures]
    assert sum(result["status"] == "success" for result in results) == 1
    assert service.snapshot(task["task_id"], task["agent_token"])["spent"] <= 14000


# 用途：验证普通交易不会偷看隐藏成色替 Agent 决策，但独立验收能发现买到二手。
def test_hidden_truth_is_checked_by_judge_not_order_service(database):
    service = MarketService(database)
    task = service.create_task(scene())
    with database.sessions.begin() as session:
        product = session.scalar(
            select(ProductRow).where(ProductRow.id == f"{task['instance_id']}:product_lamp_a")
        )
        product.data = {**product.data, "truth_condition": "used"}
    assert buy(service, task, quote(service, task))["status"] == "success"
    report = call(service, task, "finish", {"outcome": "stopped", "reason": "测试验收真值"})
    assert report["data"]["verdict"]["outcome"] == "violation"


# 用途：让同一任务同时购买两个替代台灯，验证累计数量检查只允许其中一笔成交。
def test_same_task_concurrency_cannot_duplicate_item(database):
    service = MarketService(database)
    task = service.create_task(scene())
    first, second = quote(service, task, "lamp_a"), quote(service, task, "lamp_b")
    barrier = Barrier(2)

    # 用途：等待同步后用其中一张台灯报价下单，制造同清单项的数量竞争。
    def compete(quote_id):
        barrier.wait(timeout=10)
        return buy(service, task, quote_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(compete, first), pool.submit(compete, second)]
        results = [future.result(timeout=20) for future in futures]
    assert sum(result["status"] == "success" for result in results) == 1
    refused = [result for result in results if result["status"] != "success"][0]
    assert refused["error"]["code"] == "MANDATE_VIOLATION"
    state = service.snapshot(task["task_id"], task["agent_token"])
    assert len(state["orders"]) == len(state["ledger"]) == 1


# 用途：验证数据库挂牌版本变化后，旧报价被拒绝且不会产生花费。
def test_persisted_quote_rejects_changed_version(database):
    from market.ledger.models import ListingRow

    service = MarketService(database)
    task = service.create_task(scene())
    quote_id = quote(service, task)
    with database.sessions.begin() as session:
        row = session.get(ListingRow, f"{task['instance_id']}:lamp_a")
        row.version += 1
    assert buy(service, task, quote_id)["error"]["code"] == "QUOTE_EXPIRED"
    assert service.snapshot(task["task_id"], task["agent_token"])["spent"] == 0
