# PostgreSQL 连通性测试。它会连接 DATABASE_URL 指定的数据库。
# 本地由 uv --env-file 加载配置；GitHub CI 由工作流注入测试数据库地址。

import os

from sqlalchemy import create_engine, text


def test_database_connection() -> None:
    # engine 管理本次测试需要的数据库连接资源。
    engine = create_engine(os.environ["DATABASE_URL"])

    try:
        # 退出 with 会归还连接；这个测试只读数据，不需要提交写入事务。
        with engine.connect() as connection:
            # scalar_one 取唯一一行的唯一一列，因此得到整数 1。
            result = connection.execute(text("SELECT 1")).scalar_one()
            # 查询成功还要检查结果，避免仅凭“没有异常”判定测试通过。
            assert result == 1
    finally:
        # 无论查询或断言是否出错，都清理本进程创建的连接池资源。
        engine.dispose()
