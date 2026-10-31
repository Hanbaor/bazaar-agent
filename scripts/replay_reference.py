# 将场景参考步骤展开成公开查询、取得报价和下单，核对实际成交与账本。
import argparse
import json
from pathlib import Path
from uuid import uuid4

from market.ledger.db import Database
from market.ledger.service import MarketService
from market.scenarios import load_scenario

ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = ("s01_basic", "s02_stockout", "s03_low_budget")


# 用途：创建场景任务，用同一服务执行参考采购步骤，再核对实际花费和独立验收结果；不调用模型。
def replay(service: MarketService, name: str) -> dict:
    scenario = load_scenario(ROOT / "evals/scenarios/dev" / f"{name}.json")
    created = service.create_task(scenario)
    task_id, token = created["task_id"], created["agent_token"]

    # 用途：为重放中的一个新工具动作生成编号，并带当前任务身份调用业务服务。
    def call(tool, arguments):
        return service.execute(task_id, token, str(uuid4()), tool, arguments)

    # 三个场景都先调查公开商品与政策，S03 据此验证停止理由。
    catalog = call("search_listings", {})["data"]
    evidence = []
    for seller_id in sorted({x["seller_id"] for x in catalog}):
        policy = call("get_policy", {"seller_id": seller_id})
        evidence.append(policy["data"]["evidence_id"])
    for step in scenario.reference_trace:
        listing = call("get_listing", {"listing_id": step.listing_id})["data"]
        offered = call(
            "make_offer",
            {"listing_id": step.listing_id, "unit_price": listing["list_price"], "qty": step.qty},
        )
        assert offered["status"] == "success" and offered["data"]["status"] == "accepted", offered
        result = call(
            "place_order",
            {
                "item_id": step.item_id,
                "quote_id": offered["data"]["quote"]["quote_id"],
                "qty": step.qty,
            },
        )
        actual = "success" if result["status"] == "success" else result["error"]["code"]
        assert actual == step.expected_result, (actual, step.expected_result)
    finished = call(
        "finish",
        {
            "outcome": "completed" if scenario.reference_trace else "stopped",
            "reason": "参考步骤重放完成"
            if scenario.reference_trace
            else "完整公开候选及固定价政策表明整套预算不足",
            "evidence_ids": evidence,
            "order_ids": [o["id"] for o in service.snapshot(task_id, token)["orders"]],
        },
    )
    state = service.snapshot(task_id, token)
    assert state["spent"] == scenario.expected.reference_spent, state
    assert finished["data"]["verdict"]["outcome"] == scenario.expected.outcome, finished
    assert finished["data"]["verdict"]["passed"], finished
    return {
        "scenario": name,
        "task_id": task_id,
        "spent": state["spent"],
        "verdict": state["verdict"],
    }


# 用途：读取命令行场景选项，连接数据库、执行一次参考重放并打印实际结果，最后关闭连接池。
def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", choices=SCENARIOS, default="s01_basic")
    arguments = parser.parse_args()
    database = Database()
    try:
        print(
            json.dumps(
                replay(MarketService(database), arguments.scenario), ensure_ascii=False, indent=2
            )
        )
    finally:
        database.close()


if __name__ == "__main__":
    main()
