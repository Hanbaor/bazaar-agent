# 只创建尚不存在的表，不删除个人开发数据。
from market.ledger.db import Database


# 用途：创建缺失的数据表并输出提示，结束时释放数据库连接池。
def main() -> None:
    database = Database()
    try:
        database.initialize()
        print("数据库表初始化完成")
    finally:
        database.close()


if __name__ == "__main__":
    main()
