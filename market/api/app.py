# 本模块属于 HTTP 接口层：Uvicorn 加载 app，各路由再调用统一的 MarketService。
# 接口只处理请求、身份和响应；预算、库存、动作去重及订单事务继续由 Service 负责。
import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from market.api.schemas import (
    ActionRequest,
    CreateTask,
    InterventionRequest,
    LLMCallRequest,
    ReportRequest,
)
from market.ledger.db import Database
from market.ledger.service import AccessDenied, MarketService
from market.scenarios import load_scenario

ROOT = Path(__file__).resolve().parents[2]


# 用途：从 Authorization 请求头提取 Bearer 令牌；缺失或格式错误时拒绝访问。
def bearer(authorization: str | None = Header(default=None)) -> str:
    if authorization is None or not authorization.startswith("Bearer "):
        raise AccessDenied()
    return authorization.removeprefix("Bearer ")


# 用途：组装 HTTP 路由、鉴权、SSE 和静态页面；测试可注入隔离业务服务。
def create_app(service: MarketService | None = None) -> FastAPI:
    # 测试可以注入自己的服务和临时数据库；正常启动才读取 DATABASE_URL。
    @asynccontextmanager
    # 用途：在应用启动时准备业务服务，应用关闭时释放本应用创建的数据库连接池。
    async def lifespan(application):
        database = None
        if service is None:
            database = Database()
            application.state.market_service = MarketService(database)
        else:
            application.state.market_service = service
        try:
            yield
        finally:
            if database is not None:
                database.close()

    application = FastAPI(title="Bazaar Agent M1", lifespan=lifespan)

    @application.exception_handler(AccessDenied)
    # 用途：把不可见任务和无效令牌统一转换为 404 响应，避免泄露其他任务是否存在。
    async def denied(request, exception):
        return JSONResponse(status_code=404, content={"detail": "任务不存在或不可见"})

    # 用途：从当前应用状态获取唯一的业务服务，供各个路由通过依赖注入复用。
    def backend(request: Request) -> MarketService:
        return request.app.state.market_service

    @application.post("/tasks")
    # 用途：加载允许的开发场景，创建任务和独立实例，返回新任务的编号与本地令牌。
    def create(body: CreateTask, market: MarketService = Depends(backend)):
        scenario = load_scenario(ROOT / "evals/scenarios/dev" / f"{body.scenario}.json")
        return market.create_task(scenario)

    @application.get("/tasks/{task_id}")
    # 用途：鉴权后返回本任务公开状态、委托、订单、账本、用量和验收结果。
    def task_state(
        task_id: str, token: str = Depends(bearer), market: MarketService = Depends(backend)
    ):
        return market.snapshot(task_id, token)

    @application.post("/tasks/{task_id}/actions")
    # 用途：将 HTTP 中的动作编号、工具名和参数交给统一服务，不在路由中另写成交规则。
    def action(
        task_id: str,
        body: ActionRequest,
        token: str = Depends(bearer),
        market: MarketService = Depends(backend),
    ):
        return market.execute(task_id, token, body.action_id, body.tool, body.arguments)

    @application.get("/tasks/{task_id}/operations/{action_id}")
    # 用途：查询原动作是否已有提交结果，用于超时查证；此查询不推进市场时钟。
    def operation(
        task_id: str,
        action_id: str,
        token: str = Depends(bearer),
        market: MarketService = Depends(backend),
    ):
        return market.get_operation(task_id, token, action_id)

    @application.get("/tasks/{task_id}/orders")
    # 用途：返回当前任务的实际订单、账本和总花费，供页面或恢复流程核对。
    def orders(
        task_id: str, token: str = Depends(bearer), market: MarketService = Depends(backend)
    ):
        state = market.snapshot(task_id, token)
        return {"orders": state["orders"], "ledger": state["ledger"], "spent": state["spent"]}

    @application.post("/tasks/{task_id}/finish")
    # 用途：将结构化采购报告提交给业务服务，并由独立验收器确定实际结果。
    def finish(
        task_id: str,
        body: ReportRequest,
        token: str = Depends(bearer),
        market: MarketService = Depends(backend),
    ):
        return market.execute(task_id, token, body.action_id, "finish", body.report.model_dump())

    @application.post("/tasks/{task_id}/interventions/stockout")
    # 用途：转交人工断货请求，服务层会要求 owner 令牌并修改真实数据库库存。
    def stockout(
        task_id: str,
        body: InterventionRequest,
        token: str = Depends(bearer),
        market: MarketService = Depends(backend),
    ):
        return market.intervene(task_id, token, body.action_id, body.listing_id)

    @application.post("/tasks/{task_id}/llm-calls")
    # 用途：保存执行器上报的模型调用观测，记录用量和费用估算，不改变业务预算。
    def llm_call(
        task_id: str,
        body: LLMCallRequest,
        token: str = Depends(bearer),
        market: MarketService = Depends(backend),
    ):
        return market.record_llm_call(
            task_id, token, body.call_id, body.model_dump(exclude={"call_id"})
        )

    @application.get("/tasks/{task_id}/events")
    # 用途：先核对任务身份，再建立从 after 序号开始的公开 SSE 事件流。
    async def stream(
        request: Request,
        task_id: str,
        after: int = Query(default=0, ge=0),
        token: str = Depends(bearer),
        market: MarketService = Depends(backend),
    ):
        # 在开始流式响应前验证身份，否则已经发出 200 后难以正确返回鉴权错误。
        await run_in_threadpool(market.snapshot, task_id, token)

        # 用途：循环读取新事件并生成 SSE 消息帧，空闲时发心跳，任务结束后发送结束通知。
        async def frames():
            cursor = after
            while not await request.is_disconnected():
                batch = await run_in_threadpool(market.events, task_id, token, cursor)
                for event in batch:
                    cursor = event["seq"]
                    yield f"id: {cursor}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                state = await run_in_threadpool(market.snapshot, task_id, token)
                if not batch and state["status"] != "running":
                    yield "event: done\ndata: {}\n\n"
                    return
                if not batch:
                    yield ": heartbeat\n\n"
                await asyncio.sleep(0.5)

        return StreamingResponse(
            frames(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"}
        )

    @application.get("/")
    # 用途：页面文件已创建时返回首页，否则显示 API 就绪提示；不读取任意客户端文件路径。
    def index():
        page = ROOT / "web/index.html"
        return (
            FileResponse(page)
            if page.exists()
            else {"message": "API 已启动；写完 E 批页面后这里显示界面。"}
        )

    application.mount(
        "/static", StaticFiles(directory=ROOT / "web", check_dir=False), name="static"
    )
    return application


app = create_app()
