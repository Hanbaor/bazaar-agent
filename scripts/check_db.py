# 数据库连接检查脚本。运行时通过 uv 的 --env-file 把 .env 加载为环境变量。
# 这里只执行 SELECT 1，确认 Python 能连接并查询 PostgreSQL。

import os

from sqlalchemy import create_engine, text


def main() -> None:
    # 方括号按键读取环境变量；缺少 DATABASE_URL 时会明确报错。
    # engine 管理数据库连接和连接池，创建它后在实际查询时建立所需连接。
    engine = create_engine(os.environ["DATABASE_URL"])
    # with 在退出代码块时释放本次连接；connection 是块内使用的连接对象。
    with engine.connect() as connection:
        # text 把 SQL 文本包装成 SQLAlchemy 可执行语句；one 取出唯一一行。
        result = connection.execute(text("SELECT 1")).one()
        # 结果是一行数据，正常时显示为类似 (1,) 的形式。
        print(result)


# 直接运行脚本时执行 main；被其他模块 import 时不主动连接数据库。
if __name__ == "__main__":
    main()
