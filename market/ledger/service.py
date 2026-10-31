# 本模块属于市场业务服务层：接口把动作交给 MarketService，由它协调模拟器和 PostgreSQL。
# 身份校验、加锁、动作去重、订单与账本写入集中在这里，Agent 不能直接修改交易事实。
import hashlib
import hmac
import json
import secrets
from copy import deepcopy
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from market.ledger.db import Database
from market.ledger.models import (
    ActionRow,
    EventRow,
    InstanceRow,
    LedgerRow,
    ListingRow,
    LLMCallRow,
    MandateRow,
    OrderRow,
    ProductRow,
    QuoteRow,
    SellerRow,
    TaskRow,
)
from market.scenarios import Scenario
from market.schemas import Mandate
from market.sim.engine import Listing, MarketSimulator, MarketTemplate, load_template
from market.tools import TOOL_MODELS, ListingParams, rejected, success


# 类说明：任务不存在或对当前令牌不可见时抛出的业务访问异常。
class AccessDenied(Exception):
    """不存在和不可见统一处理，避免向别的任务泄露记录是否存在。"""


# 用途：把任务令牌转为 SHA-256 摘要，数据库只保存摘要，不保存原始令牌。
def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# 用途：对工具名和规范化业务参数计算稳定摘要，供相同动作号的请求内容比对。
def request_digest(name: str, arguments: dict) -> str:
    # 只散列稳定业务内容，不包括令牌、执行代次或将来的审批凭据。
    raw = json.dumps({"tool": name, "arguments": arguments}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


# 类说明：唯一市场业务入口，负责鉴权、事务、动作去重、真实订单和账本。
class MarketService:
    # 用途：保存数据库入口和可选的提交前故障钩子；钩子只供回滚测试，不向接口开放。
    def __init__(self, database: Database, before_commit=None) -> None:
        self.db = database
        # 仅供事务回滚测试注入故障，不通过 HTTP 暴露。
        self.before_commit = before_commit

    # 用途：核对令牌是否属于目标任务；人工干预只接受 owner 令牌，普通工具也接受 Agent 令牌。
    # 输入允许数据库查询返回 None；失败抛出异常，成功返回确定存在的 TaskRow。
    # 调用方接收返回值后，Pylance 就能确认它可以安全访问任务属性。
    def _authorize(self, task: TaskRow | None, token: str, owner: bool = False) -> TaskRow:
        if task is None:
            raise AccessDenied()
        digest = token_hash(token)
        valid = hmac.compare_digest(digest, task.owner_hash)
        if not owner:
            valid = valid or hmac.compare_digest(digest, task.agent_hash)
        if not valid:
            raise AccessDenied()
        return task

    # 用途：在事务内初始化独立市场、卖家、商品和挂牌，创建任务与委托并返回两种任务令牌。
    # 只有内部并发测试可指定共享实例；HTTP 创建接口不让客户端任意选择其他实例。
    def create_task(self, scenario: Scenario, instance_id: str | None = None) -> dict:
        # 共享实例参数仅供内部并发测试，HTTP 不接受客户端指定它。
        owner_token, agent_token = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        task_id = str(uuid4())
        with self.db.sessions.begin() as session:
            if instance_id is None:
                template = load_template(scenario.market)
                market = MarketSimulator(scenario, template)
                instance_id = str(uuid4())
                instance = InstanceRow(
                    id=instance_id,
                    sim_time=market.sim_time,
                    next_seq=1,
                    config={
                        "scenario": scenario.model_dump(mode="json"),
                        "template_id": template.template_id,
                        "pricing_mode": template.pricing_mode,
                        "quote_ttl_min": template.quote_ttl_min,
                    },
                    runtime=market.runtime_dump(),
                )
                session.add(instance)
                session.flush()  # 先写父记录，再写带外键的卖家和商品。
                for seller_id, name in template.seller_names.items():
                    session.add(
                        SellerRow(
                            id=f"{instance_id}:{seller_id}",
                            instance_id=instance_id,
                            local_id=seller_id,
                            name=name,
                        )
                    )
                products = {listing.product_id: listing for listing in template.listings}
                for product_id, listing in products.items():
                    session.add(
                        ProductRow(
                            id=f"{instance_id}:{product_id}",
                            instance_id=instance_id,
                            local_id=product_id,
                            data=listing.model_dump(
                                include={
                                    "title",
                                    "category",
                                    "description",
                                    "specs",
                                    "truth_condition",
                                }
                            ),
                        )
                    )
                session.flush()
                for listing in template.listings:
                    session.add(
                        ListingRow(
                            id=f"{instance_id}:{listing.listing_id}",
                            instance_id=instance_id,
                            local_id=listing.listing_id,
                            seller_key=f"{instance_id}:{listing.seller_id}",
                            product_key=f"{instance_id}:{listing.product_id}",
                            stock=listing.stock,
                            version=listing.version,
                            list_price=listing.list_price,
                            reserve_price=listing.reserve_price,
                            concession_step=listing.concession_step,
                            ship_minutes=listing.ship_minutes,
                        )
                    )
                session.flush()
            else:
                instance = session.scalar(
                    select(InstanceRow).where(InstanceRow.id == instance_id).with_for_update()
                )
                if instance is None:
                    raise ValueError("共享实例不存在")
            task = TaskRow(
                id=task_id,
                instance_id=instance_id,
                owner_hash=token_hash(owner_token),
                agent_hash=token_hash(agent_token),
                mandate_version=scenario.mandate.version,
                spent=0,
                status="running",
            )
            session.add(task)
            session.flush()
            session.add(
                MandateRow(
                    id=f"{task_id}:{task.mandate_version}",
                    task_id=task_id,
                    version=task.mandate_version,
                    data=scenario.mandate.model_dump(mode="json"),
                )
            )
            self._emit(session, instance, task, "task_created", {"task_id": task_id})
        return {
            "task_id": task_id,
            "instance_id": instance_id,
            "owner_token": owner_token,
            "agent_token": agent_token,
        }

    # 用途：先检查任务归属，再按固定顺序锁实例、任务、挂牌和报价，返回供当前事务使用的记录。
    # 实例锁保护时钟，任务锁保护预算和累计数量，挂牌锁保护库存。
    # task_id 属于当前采购任务；返回的实例和任务均已检查存在，并持有锁到事务结束。
    def _locked(
        self, session: Session, task_id: str, token: str, owner: bool = False
    ) -> tuple[InstanceRow, TaskRow, list[ListingRow], list[QuoteRow]]:
        initial = self._authorize(session.get(TaskRow, task_id), token, owner)
        # 所有写入口遵循同一个锁顺序。实例锁同时保护时钟和事件序号。
        instance = session.scalar(
            select(InstanceRow).where(InstanceRow.id == initial.instance_id).with_for_update()
        )
        # 外键通常保证实例存在；仍显式处理查不到的情况，避免后续访问 None.id。
        if instance is None:
            raise ValueError("任务关联的市场实例不存在")
        task = self._authorize(
            session.scalar(
                select(TaskRow)
                .where(TaskRow.id == task_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            ),
            token,
            owner,
        )
        listings = list(
            session.scalars(
                select(ListingRow)
                .where(ListingRow.instance_id == instance.id)
                .order_by(ListingRow.id)
                .with_for_update()
            )
        )
        quotes = list(
            session.scalars(
                select(QuoteRow)
                .where(QuoteRow.instance_id == instance.id)
                .order_by(QuoteRow.id)
                .with_for_update()
            )
        )
        return instance, task, listings, quotes

    # 用途：把数据库当前的卖家、商品、挂牌和报价组合成临时模拟器，复用确定性业务策略。
    def _market(self, session, instance, listings, quotes) -> MarketSimulator:
        # 先把外键关联的卖家、商品放进字典，再组合成当前挂牌。
        # 库存、价格和版本来自刚锁住的行，绝不重新读取初始模板覆盖它们。
        sellers = {
            s.id: s
            for s in session.scalars(select(SellerRow).where(SellerRow.instance_id == instance.id))
        }
        products = {
            p.id: p
            for p in session.scalars(
                select(ProductRow).where(ProductRow.instance_id == instance.id)
            )
        }
        current = []
        for row in listings:
            current.append(
                Listing(
                    **products[row.product_key].data,
                    product_id=products[row.product_key].local_id,
                    seller_id=sellers[row.seller_key].local_id,
                    listing_id=row.local_id,
                    stock=row.stock,
                    version=row.version,
                    list_price=row.list_price,
                    reserve_price=row.reserve_price,
                    concession_step=row.concession_step,
                    ship_minutes=row.ship_minutes,
                )
            )
        config = instance.config
        template = MarketTemplate(
            template_id=config["template_id"],
            seller_names={s.local_id: s.name for s in sellers.values()},
            listings=current,
            pricing_mode=config["pricing_mode"],
            quote_ttl_min=config["quote_ttl_min"],
        )
        market = MarketSimulator(Scenario.model_validate(config["scenario"]), template)
        market.restore_runtime(instance.runtime, [q.data for q in quotes], instance.sim_time)
        return market

    # 用途：将本次模拟得到的时刻、运行状态、挂牌变化和报价写回当前会话，等待事务统一提交。
    def _save_market(self, session, instance, listings, quotes, market) -> None:
        # JSON 字段整体赋新值，保证 ORM 能识别变化；不依赖嵌套字典原地修改被自动发现。
        instance.sim_time = market.sim_time
        instance.runtime = market.runtime_dump()
        for row in listings:
            listing = market._find_listing(row.local_id)
            for field in (
                "stock",
                "version",
                "list_price",
                "reserve_price",
                "concession_step",
                "ship_minutes",
            ):
                setattr(row, field, getattr(listing, field))
        existing = {q.id: q for q in quotes}
        for quote in market._quotes.values():
            key = f"{instance.id}:{quote.quote_id}"
            if key in existing:
                existing[key].data = quote.model_dump()
            else:
                session.add(
                    QuoteRow(
                        id=key,
                        instance_id=instance.id,
                        task_id=quote.task_id,
                        listing_key=f"{instance.id}:{quote.listing_id}",
                        data=quote.model_dump(),
                    )
                )

    # 用途：分配实例内递增事件序号并追加公开事件；只加入会话，不单独提交事务。
    def _emit(self, session, instance, task, kind: str, data: dict) -> None:
        seq = instance.next_seq
        instance.next_seq += 1
        session.add(
            EventRow(
                id=f"{instance.id}:{seq}",
                instance_id=instance.id,
                task_id=task.id,
                seq=seq,
                sim_time=instance.sim_time,
                kind=kind,
                data=deepcopy(data),
            )
        )

    # 用途：按任务当前委托版本读取保存的 JSON，并转换为经过校验的 Mandate 对象。
    # 输入是当前会话与确定存在的任务；缺少对应版本时立即报错，不使用空数据替代授权。
    def _mandate(self, session: Session, task: TaskRow) -> Mandate:
        row = session.get(MandateRow, f"{task.id}:{task.mandate_version}")
        if row is None:
            raise ValueError("任务对应的委托版本不存在")
        return Mandate.model_validate(row.data)

    # 用途：读取指定任务的实际订单并转换为公开字典，供数量累计、页面和恢复查询使用。
    def _orders(self, session, task_id: str) -> list[dict]:
        rows = session.scalars(
            select(OrderRow).where(OrderRow.task_id == task_id).order_by(OrderRow.id)
        )
        fields = (
            "id",
            "op_id",
            "item_id",
            "qty",
            "unit_price",
            "total",
            "created_at",
            "promised_at",
            "mandate_version",
        )
        return [
            {**{f: getattr(row, f) for f in fields}, "listing_id": row.listing_key.split(":", 1)[1]}
            for row in rows
        ]

    # 用途：鉴权后查询原动作的已提交结果，不推进模拟时间；未查到不代表原请求未执行。
    def get_operation(self, task_id: str, token: str, action_id: str) -> dict:
        with self.db.sessions() as session:
            self._authorize(session.get(TaskRow, task_id), token)
            row = session.get(ActionRow, f"{task_id}:{action_id}")
            # 未查到不证明原请求未执行，客户端仍沿用原动作号。
            return success(
                {"known": row is not None, "result": deepcopy(row.result) if row else None}
            )

    # 用途：汇总当前任务的公开委托、状态、订单、账本、用量和验收结果，不导出内部场景或令牌摘要。
    def snapshot(self, task_id: str, token: str) -> dict:
        with self.db.sessions() as session:
            # 接收鉴权后的任务对象，让调用方也得到“任务确定存在”的类型信息。
            task = self._authorize(session.get(TaskRow, task_id), token)
            instance = session.get(InstanceRow, task.instance_id)
            if instance is None:
                raise ValueError("任务关联的市场实例不存在")
            ledger = list(session.scalars(select(LedgerRow).where(LedgerRow.task_id == task.id)))
            calls = list(session.scalars(select(LLMCallRow).where(LLMCallRow.task_id == task.id)))
            # 不序列化整行，防止把令牌摘要、隐藏场景等内部字段带出去。
            return {
                "task_id": task.id,
                "status": task.status,
                "spent": task.spent,
                "sim_time": instance.sim_time,
                "mandate": self._mandate(session, task).model_dump(mode="json"),
                "orders": self._orders(session, task.id),
                "ledger": [
                    {"order_id": r.order_id, "amount": r.amount, "spent_after": r.spent_after}
                    for r in ledger
                ],
                "report": task.report,
                "verdict": task.verdict,
                "llm_calls": [call.data for call in calls],
            }

    # 用途：鉴权后按调用编号保存模型用量与费用观测；相同记录去重，不改变市场时钟或花费授权。
    def record_llm_call(self, task_id: str, token: str, call_id: str, data: dict) -> dict:
        # 使用模型调用编号去重。这个接口只记观测，不改变授权或推进模拟时钟。
        with self.db.sessions.begin() as session:
            _, task, _, _ = self._locked(session, task_id, token)
            previous = session.get(LLMCallRow, call_id)
            if previous is not None:
                if previous.task_id != task_id:
                    raise AccessDenied()
                return (
                    success({"recorded": True})
                    if previous.data == data
                    else rejected("INVALID_ARGUMENT", "调用记录内容冲突")
                )
            session.add(LLMCallRow(id=call_id, task_id=task.id, data=deepcopy(data)))
            return success({"recorded": True})

    # 用途：返回当前任务中序号大于 after 的至多 100 条公开事件，供 SSE 分批补发。
    def events(self, task_id: str, token: str, after: int = 0) -> list[dict]:
        with self.db.sessions() as session:
            self._authorize(session.get(TaskRow, task_id), token)
            rows = session.scalars(
                select(EventRow)
                .where(EventRow.task_id == task_id, EventRow.seq > after)
                .order_by(EventRow.seq)
                .limit(100)
            )
            return [
                {"seq": r.seq, "sim_time": r.sim_time, "kind": r.kind, "data": r.data} for r in rows
            ]

    # 用途：接收受信任执行器提交的普通工具动作，把它交给统一事务入口处理。
    def execute(self, task_id: str, token: str, action_id: str, name: str, arguments: dict) -> dict:
        return self._execute(task_id, token, action_id, name, arguments, owner=False)

    # 用途：把人工断货请求交给统一事务入口，并强制要求 owner 令牌。
    def intervene(self, task_id: str, token: str, action_id: str, listing_id: str) -> dict:
        return self._execute(
            task_id, token, action_id, "intervene_stockout", {"listing_id": listing_id}, owner=True
        )

    # 用途：统一处理参数校验、鉴权、加锁、动作去重、业务分派、状态保存和事件追加。
    # 所有相关写入在同一事务提交；数据库异常回滚，确定的业务拒绝正常保存。
    def _execute(
        self, task_id: str, token: str, action_id: str, name: str, arguments: dict, owner: bool
    ) -> dict:
        if not action_id or len(action_id) > 120:
            return rejected("INVALID_ARGUMENT", "需要有效动作编号")
        model = ListingParams if owner and name == "intervene_stockout" else TOOL_MODELS.get(name)
        if model is None:
            return rejected("INVALID_ARGUMENT", "未知工具")
        try:
            params = model.model_validate(arguments).model_dump(mode="json")
        except ValidationError:
            return rejected("INVALID_ARGUMENT", "参数类型、范围或字段不正确")
        if name == "get_operation":
            return self.get_operation(task_id, token, params["action_id"])
        if name == "list_task_orders":
            state = self.snapshot(task_id, token)
            return success({"orders": state["orders"], "spent": state["spent"]})
        digest = request_digest(name, params)
        with self.db.sessions.begin() as session:
            instance, task, listings, quotes = self._locked(session, task_id, token, owner)
            previous = session.get(ActionRow, f"{task_id}:{action_id}")
            # 先鉴权再返回缓存；相同操作重试不会重复推进时钟或扣款。
            if previous:
                return (
                    deepcopy(previous.result)
                    if previous.digest == digest
                    else rejected("INVALID_ARGUMENT", "动作号内容冲突")
                )
            market = self._market(session, instance, listings, quotes)
            mandate = self._mandate(session, task)
            if task.status != "running":
                result = rejected("MANDATE_VIOLATION", "任务已经结束")
            elif name in {"place_order", "request_approval"}:
                market.advance(name)
                result = self._place_order(
                    session, task, instance, market, mandate, action_id, params, name
                )
            elif name == "finish":
                from market.judge import judge

                session.flush()
                task.report = params
                # 未结束任务的 verdict 字段允许为 None；本次验收先用明确的字典变量接收。
                # 再保存结果并更新状态，避免从可空 ORM 字段直接读取 outcome。
                verdict: dict = judge(session, task, market, mandate, params)
                task.verdict = verdict
                task.status = verdict["outcome"]
                result = success({"verdict": verdict})
            elif name == "intervene_stockout":
                try:
                    market.set_stock(params["listing_id"], 0)
                    result = success({"listing_id": params["listing_id"], "stock": 0})
                except KeyError:
                    result = rejected("NOT_FOUND")
            else:
                result = market.perform(task_id, name, params)
            self._save_market(session, instance, listings, quotes, market)
            session.add(
                ActionRow(
                    id=f"{task_id}:{action_id}",
                    task_id=task_id,
                    action_id=action_id,
                    name=name,
                    digest=digest,
                    arguments=params,
                    result=deepcopy(result),
                )
            )
            self._emit(
                session,
                instance,
                task,
                "action",
                {"action_id": action_id, "tool": name, "arguments": params, "result": result},
            )
            for event in market.pending_events:
                self._emit(session, instance, task, event["type"], event)
            session.flush()
            if self.before_commit is not None:
                self.before_commit()
            # 确定业务拒绝也正常提交；只有数据库异常/故障才回滚全部写入。
            return deepcopy(result)

    # 用途：在已持有锁的事务内检查报价、清单剩余数量、库存、时间和累计预算。
    # 合法成交才扣库存、建订单并追加账本；超预算不会自动使用 20% 的审批申请额度。
    def _place_order(self, session, task, instance, market, mandate, action_id, params, name):
        # 这里收到的 params 已通过 Pydantic 校验，且调用者、任务、实例均已锁定。
        # 返回的是确定业务结果；抛出的数据库异常由事务上下文统一回滚。
        try:
            quote = market.validate_quote(task.id, params["quote_id"], params["qty"])
        except KeyError:
            return rejected("NOT_FOUND")
        except ValueError as error:
            return rejected(str(error))
        item = next((x for x in mandate.items if x.item_id == params["item_id"]), None)
        orders = self._orders(session, task.id)
        purchased = sum(o["qty"] for o in orders if o["item_id"] == params["item_id"])
        if item is None or purchased + params["qty"] > item.qty:
            return rejected("MANDATE_VIOLATION", "清单项不存在或累计数量超限")
        listing = market._find_listing(quote.listing_id)
        if listing.stock < params["qty"]:
            return rejected("OUT_OF_STOCK")
        promised = market.sim_time + listing.ship_minutes
        if market.sim_time > mandate.order_deadline or (
            mandate.delivery_deadline is not None and promised > mandate.delivery_deadline
        ):
            return rejected("MANDATE_VIOLATION", "超过下单或承诺送达期限")
        total = quote.unit_price * params["qty"]
        proposed = task.spent + total
        if proposed > mandate.budget_total:
            limit = mandate.budget_total * (1 + mandate.approval_policy.max_over_ratio)
            if proposed > limit:
                return rejected("OVER_POLICY_LIMIT", "累计金额超过可申请上限")
            if name == "request_approval":
                # 三个开发场景的模拟用户均拒绝超预算。完整人工审批在 M2 实现。
                return rejected("MANDATE_VIOLATION", "本场景模拟用户拒绝超预算申请")
            return {
                "status": "approval_required",
                "data": {
                    "op_id": action_id,
                    "proposed_spent": proposed,
                    "mandate_version": mandate.version,
                },
                "error": {"code": "APPROVAL_REQUIRED", "message": "尚未得到单笔批准"},
            }
        if name == "request_approval":
            return success({"approval_needed": False})
        # 下列写入均在持有任务/挂牌锁的同一事务内，任一失败全部回滚。
        listing.stock -= params["qty"]
        market._quotes[quote.quote_id].used = True
        task.spent = proposed
        order_id = str(uuid4())
        session.add(
            OrderRow(
                id=order_id,
                task_id=task.id,
                op_id=action_id,
                item_id=item.item_id,
                quote_key=f"{instance.id}:{quote.quote_id}",
                listing_key=f"{instance.id}:{listing.listing_id}",
                mandate_version=mandate.version,
                listing_version=listing.version,
                qty=params["qty"],
                unit_price=quote.unit_price,
                total=total,
                created_at=market.sim_time,
                promised_at=promised,
            )
        )
        session.flush()
        session.add(
            LedgerRow(order_id=order_id, task_id=task.id, amount=total, spent_after=proposed)
        )
        return success(
            {
                "order_id": order_id,
                "item_id": item.item_id,
                "listing_id": listing.listing_id,
                "qty": params["qty"],
                "total": total,
                "spent": proposed,
                "promised_at": promised,
            }
        )
