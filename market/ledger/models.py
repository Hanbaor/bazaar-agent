# SQLAlchemy ORM 模型描述数据库表；Pydantic 描述接口数据，两者职责不同。
# M1 使用 create_all 建表；修改既有线上结构的迁移工具放到后续阶段。
from sqlalchemy import CheckConstraint, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


# 类说明：所有 ORM 表的共同基类，汇集元数据以便按关联关系建表。
class Base(DeclarativeBase):
    """所有表共用的元数据，Base.metadata.create_all 会按外键顺序建表。"""


# 类说明：市场实例表，保存该局配置、模拟时刻、运行状态和事件序号。
class InstanceRow(Base):
    __tablename__ = "market_instances"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    config: Mapped[dict] = mapped_column(JSONB)  # 含隐藏场景，只供服务端读取。
    runtime: Mapped[dict] = mapped_column(JSONB)
    sim_time: Mapped[int] = mapped_column(Integer)
    next_seq: Mapped[int] = mapped_column(Integer, default=1)
    __table_args__ = (CheckConstraint("sim_time >= 0 AND next_seq > 0"),)


# 类说明：卖家表，用实例编号隔离不同局中的同名卖家。
class SellerRow(Base):
    __tablename__ = "sellers"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    instance_id: Mapped[str] = mapped_column(ForeignKey("market_instances.id"))
    local_id: Mapped[str] = mapped_column(String)
    name: Mapped[str] = mapped_column(String)
    __table_args__ = (UniqueConstraint("instance_id", "local_id"),)


# 类说明：商品表，保存规格、公开描述和仅供服务端验收的真值。
class ProductRow(Base):
    __tablename__ = "products"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    instance_id: Mapped[str] = mapped_column(ForeignKey("market_instances.id"))
    local_id: Mapped[str] = mapped_column(String)
    data: Mapped[dict] = mapped_column(JSONB)
    __table_args__ = (UniqueConstraint("instance_id", "local_id"),)


# 类说明：挂牌表，将卖家与商品关联起来，并约束当前价格、库存和版本。
class ListingRow(Base):
    __tablename__ = "listings"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    instance_id: Mapped[str] = mapped_column(ForeignKey("market_instances.id"))
    local_id: Mapped[str] = mapped_column(String)
    seller_key: Mapped[str] = mapped_column(ForeignKey("sellers.id"))
    product_key: Mapped[str] = mapped_column(ForeignKey("products.id"))
    stock: Mapped[int] = mapped_column(Integer)
    version: Mapped[int] = mapped_column(Integer)
    list_price: Mapped[int] = mapped_column(Integer)
    reserve_price: Mapped[int] = mapped_column(Integer)
    concession_step: Mapped[int] = mapped_column(Integer)
    ship_minutes: Mapped[int] = mapped_column(Integer)
    __table_args__ = (
        UniqueConstraint("instance_id", "local_id"),
        CheckConstraint("stock >= 0 AND version > 0 AND list_price >= reserve_price"),
        CheckConstraint("reserve_price >= 0 AND concession_step >= 0 AND ship_minutes >= 0"),
    )


# 类说明：采购任务表，保存任务令牌摘要、当前委托版本、累计花费及最终状态。
class TaskRow(Base):
    __tablename__ = "tasks"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    instance_id: Mapped[str] = mapped_column(ForeignKey("market_instances.id"))
    owner_hash: Mapped[str] = mapped_column(String)
    agent_hash: Mapped[str] = mapped_column(String)
    mandate_version: Mapped[int] = mapped_column(Integer)
    spent: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String, default="running")
    report: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    verdict: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    __table_args__ = (CheckConstraint("spent >= 0 AND mandate_version > 0"),)


# 类说明：委托版本表，保留每个任务的授权内容，避免修改版本后混用旧授权。
class MandateRow(Base):
    __tablename__ = "mandates"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"))
    version: Mapped[int] = mapped_column(Integer)
    data: Mapped[dict] = mapped_column(JSONB)
    __table_args__ = (UniqueConstraint("task_id", "version"),)


# 类说明：报价记录表，将服务端生成的报价关联到任务、挂牌和市场实例。
class QuoteRow(Base):
    __tablename__ = "quotes"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    instance_id: Mapped[str] = mapped_column(ForeignKey("market_instances.id"))
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"))
    listing_key: Mapped[str] = mapped_column(ForeignKey("listings.id"))
    data: Mapped[dict] = mapped_column(JSONB)  # 报价由服务端生成；使用后整体赋回新 JSON。


# 类说明：动作记录表，按任务与动作编号保存请求摘要和结果，支持持久化去重。
class ActionRow(Base):
    __tablename__ = "actions"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"))
    action_id: Mapped[str] = mapped_column(String)
    name: Mapped[str] = mapped_column(String)
    digest: Mapped[str] = mapped_column(String)
    arguments: Mapped[dict] = mapped_column(JSONB)
    result: Mapped[dict] = mapped_column(JSONB)
    __table_args__ = (UniqueConstraint("task_id", "action_id"),)


# 类说明：订单表，保存实际成交内容；操作号和报价关联的唯一约束防止重复成交。
class OrderRow(Base):
    __tablename__ = "orders"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"))
    op_id: Mapped[str] = mapped_column(String, unique=True)
    item_id: Mapped[str] = mapped_column(String)
    quote_key: Mapped[str] = mapped_column(ForeignKey("quotes.id"), unique=True)
    listing_key: Mapped[str] = mapped_column(ForeignKey("listings.id"))
    mandate_version: Mapped[int] = mapped_column(Integer)
    listing_version: Mapped[int] = mapped_column(Integer)
    qty: Mapped[int] = mapped_column(Integer)
    unit_price: Mapped[int] = mapped_column(Integer)
    total: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[int] = mapped_column(Integer)
    promised_at: Mapped[int] = mapped_column(Integer)
    __table_args__ = (
        CheckConstraint("qty > 0 AND unit_price >= 0 AND total = qty * unit_price"),
        CheckConstraint("created_at >= 0 AND promised_at >= created_at"),
    )


# 类说明：只追加的扣款账本，每张订单最多对应一条扣款记录。
class LedgerRow(Base):
    __tablename__ = "ledger_entries"
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"))
    amount: Mapped[int] = mapped_column(Integer)
    spent_after: Mapped[int] = mapped_column(Integer)
    __table_args__ = (CheckConstraint("amount >= 0 AND spent_after >= amount"),)


# 类说明：公开事件表，以实例内序号记录变化，支持按序补发与回放。
class EventRow(Base):
    __tablename__ = "events"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    instance_id: Mapped[str] = mapped_column(ForeignKey("market_instances.id"))
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"))
    seq: Mapped[int] = mapped_column(Integer)
    sim_time: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String)
    data: Mapped[dict] = mapped_column(JSONB)  # 只保存可向当前任务展示的公开数据。
    __table_args__ = (UniqueConstraint("instance_id", "seq"),)


# 类说明：模型调用观测表，记录用量、耗时和费用估算，不作为采购余额。
class LLMCallRow(Base):
    __tablename__ = "llm_calls"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"))
    data: Mapped[dict] = mapped_column(JSONB)  # 模型标识、token、耗时、费用估算与错误码。
