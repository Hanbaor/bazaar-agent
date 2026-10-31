# Engine 管理连接池；每次请求用新的 Session，不跨线程共享可变会话。

import os

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from market.ledger.models import Base


# 类说明：统一管理 PostgreSQL 连接池、会话工厂和建表操作。
class Database:
    # 用途：建立 PostgreSQL 连接池和会话工厂；可把表映射到测试 schema，实现数据隔离。
    def __init__(self, url: str | None = None, schema: str | None = None) -> None:
        self.engine = create_engine(url or os.environ["DATABASE_URL"], pool_pre_ping=True)
        if self.engine.dialect.name != "postgresql":
            raise ValueError("交易测试和应用必须使用 PostgreSQL")
        # 测试将表映射到临时 schema，不删除或覆盖个人开发数据。
        if schema:
            self.engine = self.engine.execution_options(schema_translate_map={None: schema})
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)

    # 用途：创建元数据中尚不存在的数据表，不删除旧表或清空已有业务数据。
    def initialize(self) -> None:
        Base.metadata.create_all(self.engine)

    # 用途：释放连接池资源，在脚本结束或服务关闭时调用。
    def close(self) -> None:
        self.engine.dispose()
