# pytest 自动发现 conftest 中的夹具；测试通过参数名申请 database 或 service。
import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.schema import CreateSchema, DropSchema

from market.ledger.db import Database
from market.ledger.service import MarketService


@pytest.fixture(scope="session")
# 用途：为本次测试会话建立随机命名的 PostgreSQL schema，供用例共享连接入口，结束时只清理该 schema。
def database():
    url = os.environ.get("BAZAAR_TEST_DATABASE_URL") or os.environ["DATABASE_URL"]
    schema = "m1_test_" + uuid4().hex
    administrator = create_engine(url)
    with administrator.begin() as connection:
        connection.execute(CreateSchema(schema))
    database = Database(url, schema=schema)
    database.initialize()
    try:
        yield database
    finally:
        database.close()
        with administrator.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True))
        administrator.dispose()


@pytest.fixture
# 用途：给每个测试创建一个业务服务对象，复用隔离测试数据库，不连接真实模型。
def service(database):
    return MarketService(database)
