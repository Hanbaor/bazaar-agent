# 本模块验证 MCP 协议层：发现十个工具、隐藏身份参数、公开查询、真实下单和预算拒绝。
# 交易通过 FastAPI TestClient 进入隔离的 PostgreSQL schema；stdio 测试另启动受控子进程。
# 子进程仅在测试的 async with 期间运行，退出后由 SDK 清理，不留下后台服务或调用模型。
import asyncio
import json
import socket
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import Thread
from typing import Literal

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient
from mcp import Client, StdioServerParameters
from mcp.server import MCPServer

from market.api.app import create_app
from market.api.client import MarketClient
from market.ledger.service import MarketService
from market.tools import TOOL_MODELS

ROOT = Path(__file__).resolve().parents[1]


# 用途：仅在用例期间运行一个真实 HTTP 服务，绑定本机随机端口，退出时停止并回收线程。
# 注入的 Service 使用隔离测试 schema；不另建开发实例，也不留下持续运行的后台服务。
@contextmanager
def tcp_market(service: MarketService) -> Iterator[str]:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(create_app(service), log_level="error"))
        thread = Thread(target=server.run, kwargs={"sockets": [listener]})
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started:
                if not thread.is_alive() or time.monotonic() >= deadline:
                    raise RuntimeError("测试 HTTP 服务未能启动")
                time.sleep(0.01)
            yield f"http://127.0.0.1:{port}"
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            assert not thread.is_alive(), "测试 HTTP 服务没有按时退出"


# 用途：为 MCP 用例创建指定场景，通过替换客户端工厂，把转发请求送入隔离的 HTTP 服务。
@contextmanager
def mcp_market(
    service: MarketService, monkeypatch: pytest.MonkeyPatch, scenario: str = "s01_basic"
) -> Iterator[tuple[MCPServer, MarketClient]]:
    from mcp_server import server

    with TestClient(create_app(service)) as http:
        task = http.post("/tasks", json={"scenario": scenario}).json()
        backend = MarketClient("http://testserver", task["task_id"], task["agent_token"], http)

        # 用途：供每个 MCP 工具取得当前测试任务的 HTTP 客户端，不读取本机任务令牌。
        def current_client() -> MarketClient:
            return backend

        monkeypatch.setattr(server, "client", current_client)
        yield server.mcp, backend


# 用途：通过实际 MCP 客户端发现工具，检查十个工具均已注册且身份与 Context 不可由模型填写。
def test_mcp_discovers_tools_without_identity_parameters() -> None:
    from mcp_server import server

    # 用途：建立协议连接并读取工具清单，校验 SDK 生成的公开输入 schema。
    async def discover() -> None:
        async with Client(server.mcp, raise_exceptions=True) as client:
            listed = await client.list_tools()
            assert {tool.name for tool in listed.tools} == set(TOOL_MODELS)
            for tool in listed.tools:
                # 每个工具都应发布结构化输出契约，防止返回注解退回裸 dict 后只剩文本。
                assert tool.output_schema is not None
                assert tool.output_schema["type"] == "object"
                properties = tool.input_schema.get("properties", {})
                assert (
                    not {"ctx", "token", "task_id", "owner_token", "exec_epoch"} & properties.keys()
                )
            listing_tool = next(tool for tool in listed.tools if tool.name == "get_listing")
            assert set(listing_tool.input_schema["properties"]) == {"params"}

    asyncio.run(discover())


# 用途：经 MCP 查询、报价、下单和查订单，验证真实成交落库且公开结果不包含内部真值。
def test_mcp_purchase_uses_http_and_database(service, monkeypatch) -> None:
    with mcp_market(service, monkeypatch) as (server, backend):
        # 用途：完整走工具协议调用，金额 12000 为整数分，qty=1 为一件台灯。
        async def purchase() -> None:
            async with Client(server, raise_exceptions=True) as client:
                listing = await client.call_tool(
                    "get_listing", {"params": {"listing_id": "lamp_a"}}
                )
                assert not listing.is_error, listing
                assert listing.structured_content is not None, listing
                public = json.dumps(listing.structured_content, ensure_ascii=False)
                assert "lamp_a" in public
                assert "reserve_price" not in public and "truth_condition" not in public
                offered = await client.call_tool(
                    "make_offer",
                    {"params": {"listing_id": "lamp_a", "unit_price": 12000, "qty": 1}},
                )
                assert not offered.is_error
                assert offered.structured_content is not None
                quote_id = offered.structured_content["data"]["quote"]["quote_id"]
                bought = await client.call_tool(
                    "place_order", {"params": {"item_id": "lamp", "quote_id": quote_id, "qty": 1}}
                )
                assert bought.structured_content is not None
                assert bought.structured_content["status"] == "success"
                orders = await client.call_tool("list_task_orders", {})
                assert orders.structured_content is not None
                assert len(orders.structured_content["data"]["orders"]) == 1

        asyncio.run(purchase())
        state = backend.snapshot()
        assert len(state["orders"]) == len(state["ledger"]) == 1
        assert state["spent"] == 12000


# 用途：使用预算不足场景验证 MCP 不能绕过服务端授权，等待批准时订单和账本仍为空。
def test_mcp_reads_public_fields_and_cannot_bypass_budget(service, monkeypatch) -> None:
    with mcp_market(service, monkeypatch, "s03_low_budget") as (server, backend):
        # 用途：申请一份超过当前预算、仍在可申请上限内的报价，并尝试实际下单。
        async def request_over_budget() -> None:
            async with Client(server, raise_exceptions=True) as client:
                offered = await client.call_tool(
                    "make_offer",
                    {"params": {"listing_id": "lamp_a", "unit_price": 12000, "qty": 1}},
                )
                assert offered.structured_content is not None
                quote_id = offered.structured_content["data"]["quote"]["quote_id"]
                result = await client.call_tool(
                    "place_order", {"params": {"item_id": "lamp", "quote_id": quote_id, "qty": 1}}
                )
                assert result.structured_content is not None
                assert result.structured_content["status"] == "approval_required"

        asyncio.run(request_over_budget())
        state = backend.snapshot()
        assert state["spent"] == 0 and state["orders"] == [] and state["ledger"] == []


# 用途：真实启动 stdio 子进程，分别验证新版协议发现和兼容旧版 initialize 握手后列工具。
@pytest.mark.parametrize("mode", ["auto", "legacy"])
def test_mcp_stdio_connection_and_tool_discovery(mode: Literal["auto", "legacy"]) -> None:
    parameters = StdioServerParameters(
        command=sys.executable,
        # SDK 提供 mcp 控制台入口，没有 mcp.__main__；显式调用同一个 CLI 应用。
        args=[
            "-c",
            "from mcp.cli import app; app()",
            "run",
            str(ROOT / "mcp_server/server.py"),
            "--transport",
            "stdio",
        ],
        cwd=str(ROOT),
        env={"PYTHONPATH": str(ROOT)},
    )

    # 用途：在有时限的连接中发现工具；只列工具无需创建业务任务，也不会访问模型。
    async def discover() -> None:
        async with Client(parameters, mode=mode, read_timeout_seconds=20) as client:
            listed = await client.list_tools()
            assert {tool.name for tool in listed.tools} == set(TOOL_MODELS)

    asyncio.run(asyncio.wait_for(discover(), timeout=40))


# 用途：贯通真实 stdio 子进程、真实 HTTP 端口与 PostgreSQL，验证工具调用最终只产生一笔成交。
def test_mcp_stdio_purchase_over_real_http(service: MarketService) -> None:
    with tcp_market(service) as url, httpx.Client(base_url=url, timeout=20) as http:
        task = http.post("/tasks", json={"scenario": "s01_basic"}).json()
        parameters = StdioServerParameters(
            command=sys.executable,
            args=[
                "-c",
                "from mcp.cli import app; app()",
                "run",
                str(ROOT / "mcp_server/server.py"),
                "--transport",
                "stdio",
            ],
            cwd=str(ROOT),
            # 只向 MCP 子进程传递本次测试任务的身份，不传模型密钥或数据库连接配置。
            env={
                "PYTHONPATH": str(ROOT),
                "BAZAAR_API_URL": url,
                "BAZAAR_TASK_ID": task["task_id"],
                "BAZAAR_TASK_TOKEN": task["agent_token"],
            },
        )

        # 用途：跨真实进程提交报价与下单，再从同一任务查询订单；金额均为整数分。
        async def purchase() -> None:
            async with Client(parameters, read_timeout_seconds=20) as client:
                offered = await client.call_tool(
                    "make_offer",
                    {"params": {"listing_id": "lamp_a", "unit_price": 12000, "qty": 1}},
                )
                assert not offered.is_error, offered
                assert offered.structured_content is not None
                quote_id = offered.structured_content["data"]["quote"]["quote_id"]
                bought = await client.call_tool(
                    "place_order", {"params": {"item_id": "lamp", "quote_id": quote_id, "qty": 1}}
                )
                assert bought.structured_content is not None
                assert bought.structured_content["status"] == "success"
                orders = await client.call_tool("list_task_orders", {})
                assert orders.structured_content is not None
                assert orders.structured_content["data"]["spent"] == 12000

        asyncio.run(asyncio.wait_for(purchase(), timeout=40))
        headers = {"Authorization": f"Bearer {task['agent_token']}"}
        state = http.get(f"/tasks/{task['task_id']}", headers=headers).json()
        assert len(state["orders"]) == len(state["ledger"]) == 1
        assert state["spent"] == 12000
