# 本模块验证 HTTP 接口与 MarketClient 的配合，复用 conftest 中隔离的 PostgreSQL schema。
# FastAPI TestClient 执行真实路由；仅传输故障使用替身，不调用模型或修改个人开发表。
import json
from collections.abc import Iterator
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from market.api.app import create_app
from market.api.client import MarketClient
from market.ledger.service import MarketService


# 用途：为一个测试启动应用生命周期并注入隔离的业务服务，退出时关闭测试客户端。
@pytest.fixture
def http(service: MarketService) -> Iterator[TestClient]:
    with TestClient(create_app(service)) as transport:
        yield transport


# 用途：经 HTTP 创建指定场景的任务，返回任务身份和绑定该任务的客户端；金额单位为分。
def create_client(http: TestClient, scenario: str = "s01_basic") -> tuple[dict, MarketClient]:
    response = http.post("/tasks", json={"scenario": scenario})
    assert response.status_code == 200
    task = response.json()
    client = MarketClient("http://testserver", task["task_id"], task["agent_token"], http)
    return task, client


# 用途：通过 HTTP 报价并下单，再重发原动作，验证只成交一次且不重复计时或扣款。
def test_http_purchase_retry_and_operation_lookup(http: TestClient) -> None:
    task, client = create_client(http)
    offered = client.execute(
        str(uuid4()), "make_offer", {"listing_id": "lamp_a", "unit_price": 12000, "qty": 1}
    )
    assert offered["status"] == "success"
    quote_id = offered["data"]["quote"]["quote_id"]
    action_id = str(uuid4())
    arguments = {"item_id": "lamp", "quote_id": quote_id, "qty": 1}
    bought = client.execute(action_id, "place_order", arguments)
    assert bought["status"] == "success"
    before = client.snapshot()

    # 同一任务的同一动作重发，必须采用原结果；模拟分钟和累计花费都不能再增加。
    assert client.execute(action_id, "place_order", arguments) == bought
    assert client.operation(action_id) == bought
    assert client.snapshot() == before
    assert len(before["orders"]) == len(before["ledger"]) == 1
    assert before["spent"] == 12000
    headers = {"Authorization": f"Bearer {task['agent_token']}"}
    orders = http.get(f"/tasks/{task['task_id']}/orders", headers=headers)
    assert orders.status_code == 200
    assert orders.json()["orders"] == before["orders"]
    assert orders.json()["ledger"] == before["ledger"]


# 用途：验证任务令牌隔离、人工干预的 owner 权限，以及公开挂牌不会泄露隐藏字段。
def test_http_task_scope_and_owner_intervention(http: TestClient) -> None:
    task, client = create_client(http)
    other, _ = create_client(http)
    path = f"/tasks/{task['task_id']}"
    assert http.get(path).status_code == 404
    assert (
        http.get(path, headers={"Authorization": f"Bearer {other['agent_token']}"}).status_code
        == 404
    )

    body = {"action_id": str(uuid4()), "listing_id": "lamp_a"}
    intervention = f"{path}/interventions/stockout"
    assert (
        http.post(
            intervention, json=body, headers={"Authorization": f"Bearer {task['agent_token']}"}
        ).status_code
        == 404
    )
    response = http.post(
        intervention, json=body, headers={"Authorization": f"Bearer {task['owner_token']}"}
    )
    assert response.status_code == 200
    listing = client.execute(str(uuid4()), "get_listing", {"listing_id": "lamp_a"})["data"]
    assert listing["stock"] == 0
    assert "reserve_price" not in listing
    assert "truth_condition" not in listing


# 用途：验证 HTTP 外层拒绝越权字段，工具内层拒绝布尔数量，非法输入不产生订单。
def test_http_request_validation(http: TestClient) -> None:
    # 场景只能从服务端白名单选择，不能通过场景字段读取任意本地路径。
    assert http.post("/tasks", json={"scenario": "unknown_scenario"}).status_code == 422
    task, client = create_client(http)
    response = http.post(
        f"/tasks/{task['task_id']}/actions",
        headers={"Authorization": f"Bearer {task['agent_token']}"},
        json={
            "action_id": str(uuid4()),
            "tool": "search_listings",
            "arguments": {},
            "owner": True,
        },
    )
    assert response.status_code == 422
    rejected = client.execute(
        str(uuid4()), "place_order", {"item_id": "lamp", "quote_id": "absent", "qty": True}
    )
    assert rejected["error"]["code"] == "INVALID_ARGUMENT"
    assert client.snapshot()["orders"] == []


# 用途：让一次真实下单先提交，再丢弃响应，验证客户端通过原动作号查证而不会重复购买。
def test_http_client_recovers_lost_order_response(http: TestClient) -> None:
    task, normal = create_client(http)
    offered = normal.execute(
        str(uuid4()), "make_offer", {"listing_id": "lamp_a", "unit_price": 12000, "qty": 1}
    )
    quote_id = offered["data"]["quote"]["quote_id"]

    # 类说明：包装真实测试传输，只模拟一次“数据库已成交、客户端没收到响应”。
    class LoseOneResponse:
        # 用途：记录下单请求次数，后面检查客户端是否多发了一笔采购。
        def __init__(self) -> None:
            self.order_requests = 0

        # 用途：先经真实路由执行请求，再对第一次下单抛出超时；查证 GET 正常转交。
        def request(self, method: str, path: str, **kwargs):
            response = http.request(method, path, **kwargs)
            if method == "POST" and kwargs.get("json", {}).get("tool") == "place_order":
                self.order_requests += 1
                if self.order_requests == 1:
                    raise httpx.ReadTimeout("模拟成交后响应丢失")
            return response

    transport = LoseOneResponse()
    client = MarketClient("http://testserver", task["task_id"], task["agent_token"], transport)
    result = client.execute(
        str(uuid4()), "place_order", {"item_id": "lamp", "quote_id": quote_id, "qty": 1}
    )
    assert result["status"] == "success"
    assert transport.order_requests == 1
    state = normal.snapshot()
    assert len(state["orders"]) == len(state["ledger"]) == 1
    assert state["spent"] == 12000


# 用途：模拟写请求和查证持续超时，验证有限重发沿用同一个动作号，并明确返回 unknown。
def test_http_client_preserves_action_id_when_result_is_unknown() -> None:
    action_ids = []
    lookups = []

    # 用途：记录每次发送的动作号和查证路径，然后模拟没有可用响应的传输故障。
    def timeout(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            action_ids.append(json.loads(request.content)["action_id"])
        else:
            lookups.append(request.url.path)
        raise httpx.ReadTimeout("模拟网络持续超时", request=request)

    action_id = str(uuid4())
    with httpx.Client(base_url="http://testserver", transport=httpx.MockTransport(timeout)) as http:
        client = MarketClient("http://testserver", "test_task", "test_token", http)
        result = client.execute(
            action_id, "place_order", {"item_id": "lamp", "quote_id": "q_1", "qty": 1}
        )
    assert result["status"] == "unknown"
    assert result["data"]["action_id"] == action_id
    assert action_ids == [action_id] * 3
    assert lookups == [f"/tasks/test_task/operations/{action_id}"] * 3


# 用途：验证已结束任务的 SSE 按序发送公开事件、支持 after 补发，并发送 done 后结束连接。
def test_http_sse_replays_events_and_closes_after_finish(http: TestClient) -> None:
    task, client = create_client(http)
    # 没有采购就报告停止会验收失败，但任务仍已结束，可以检查事件流正常收尾。
    finished = client.execute(
        str(uuid4()), "finish", {"outcome": "stopped", "reason": "测试事件流"}
    )
    assert finished["status"] == "success"
    headers = {"Authorization": f"Bearer {task['agent_token']}"}
    path = f"/tasks/{task['task_id']}/events"
    response = http.get(path, headers=headers)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    sequences = [
        int(line.removeprefix("id: "))
        for line in response.text.splitlines()
        if line.startswith("id: ")
    ]
    assert sequences and sequences == sorted(set(sequences))
    assert "event: done" in response.text
    resumed = http.get(path, params={"after": sequences[-1]}, headers=headers)
    assert "id: " not in resumed.text
    assert "event: done" in resumed.text
