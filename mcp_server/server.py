# 本模块属于 MCP 适配层，由 MCP 客户端通过 stdio 启动并调用已注册的工具。
# 每个工具只转交 MarketClient，不持有数据库连接，也不另写成交规则。
# 工具返回 dict[str, object]，让已安装的 SDK 2.3 同时提供文本与 structured_content。
# object 表示结果值可以是字符串、数字、列表或嵌套字典，不是把内部数据库对象直接暴露出去。
import os
from functools import lru_cache
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

from mcp.server import MCPServer
from mcp.server.mcpserver import Context

from market.api.client import MarketClient
from market.sim.quotes import OfferRequest
from market.tools import (
    ApprovalParams,
    FinishParams,
    ListingParams,
    OperationParams,
    OrderParams,
    PolicyParams,
    SearchParams,
    SellerParams,
)

ROOT = Path(__file__).resolve().parents[1]
mcp = MCPServer(
    "Bazaar procurement", instructions=(ROOT / "rules/purchasing.md").read_text(encoding="utf-8")
)
SESSION = str(uuid4())


@lru_cache(maxsize=1)
# 用途：首次调用时从进程环境读取当前任务身份并创建 HTTP 客户端，后续复用同一个对象。
def client() -> MarketClient:
    # 使用独立任务的 Agent 令牌，绝不把 owner_token 或模型 API key 当业务参数。
    return MarketClient(
        os.environ.get("BAZAAR_API_URL", "http://127.0.0.1:8000"),
        os.environ["BAZAAR_TASK_ID"],
        os.environ["BAZAAR_TASK_TOKEN"],
    )


# 用途：把 SDK 注入的请求上下文映射为当前会话内的稳定动作号，再转发同一市场 HTTP 接口。
def forward(name: str, params, ctx: Context) -> dict[str, object]:
    # 同一 stdio 进程中的同一请求映射到同一动作号；HTTP 重发也沿用这个号。
    # M2 才验证跨进程恢复，不能把此处的会话内映射宣称为持久化恢复。
    action_id = str(uuid5(NAMESPACE_URL, f"{SESSION}:{ctx.request_id}"))
    return client().execute(action_id, name, params.model_dump(mode="json"))


@mcp.tool()
# 用途：将 MCP 搜索参数转交市场服务，返回公开候选列表。
def search_listings(params: SearchParams, ctx: Context) -> dict[str, object]:
    """搜索公开挂牌。params={} 表示不限制条件。"""
    return forward("search_listings", params, ctx)


@mcp.tool()
# 用途：将 MCP 挂牌查询转交市场服务，返回允许公开的商品信息。
def get_listing(params: ListingParams, ctx: Context) -> dict[str, object]:
    """查看公开商品信息、库存和价格。"""
    return forward("get_listing", params, ctx)


@mcp.tool()
# 用途：将 MCP 政策查询转交市场服务，返回政策文本及证据编号。
def get_policy(params: PolicyParams, ctx: Context) -> dict[str, object]:
    """查看卖家定价政策和证据编号。"""
    return forward("get_policy", params, ctx)


@mcp.tool()
# 用途：将 MCP 卖家询问转交市场服务，返回公开说法与证据。
def ask_seller(params: SellerParams, ctx: Context) -> dict[str, object]:
    """询问卖家，回复只代表卖家公开说法。"""
    return forward("ask_seller", params, ctx)


@mcp.tool()
# 用途：将 MCP 出价转交市场服务，取得还价或报价单，不在适配层成交。
def make_offer(params: OfferRequest, ctx: Context) -> dict[str, object]:
    """按整数分报价，接受后取得报价单但尚未成交。"""
    return forward("make_offer", params, ctx)


@mcp.tool()
# 用途：将 MCP 下单转交统一事务服务，沿用同一预算、库存和数量检查。
def place_order(params: OrderParams, ctx: Context) -> dict[str, object]:
    """用当前任务报价单下单，预算、数量和库存由服务端检查。"""
    return forward("place_order", params, ctx)


@mcp.tool()
# 用途：转发逐笔审批申请，不能在 MCP 层自行扩大预算或批准交易。
def request_approval(params: ApprovalParams, ctx: Context) -> dict[str, object]:
    """申请逐笔超预算授权，M1 不会自动批准。"""
    return forward("request_approval", params, ctx)


@mcp.tool()
# 用途：通过 MCP 查证原动作是否已提交，不推进模拟时间。
def get_operation(params: OperationParams, ctx: Context) -> dict[str, object]:
    """查证原动作的提交结果，不推进时间。"""
    return forward("get_operation", params, ctx)


@mcp.tool()
# 用途：通过 MCP 读取当前任务已经成交的订单和累计花费。
def list_task_orders(ctx: Context) -> dict[str, object]:
    """查看当前任务已经成交的订单和累计花费。"""
    from market.tools import Params

    return forward("list_task_orders", Params(), ctx)


@mcp.tool()
# 用途：通过 MCP 提交结构化采购报告，最终结果仍由服务端独立验收。
def finish(params: FinishParams, ctx: Context) -> dict[str, object]:
    """提交报告，由服务端独立验收。"""
    return forward("finish", params, ctx)
