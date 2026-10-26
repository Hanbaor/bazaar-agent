# 市场运行行为测试：覆盖公开搜索、信息边界、库存修改和实例隔离。
# 相比场景配置测试，这里已经创建 MarketSimulator 并实际调用其方法。

from pathlib import Path

import pytest
from pydantic import ValidationError

from market.scenarios import load_scenario
from market.sim.engine import MarketSimulator, create_market

# 通过项目根目录定位样例，避免依赖运行 pytest 时终端所在的子目录。
SCENARIO_PATH = (
    Path(__file__).resolve().parents[1] / "evals" / "scenarios" / "dev" / "s01_basic.json"
)


def make_market() -> MarketSimulator:
    # 每次调用都从相同配置创建新市场，保证测试之间不共享库存。
    return create_market(load_scenario(SCENARIO_PATH))


# 五组筛选分别检查英文大小写、类别、价格上限、零上限和无匹配结果。
@pytest.mark.parametrize(
    ("keyword", "category", "max_price", "expected_ids"),
    [
        ("usb", None, None, {"lamp_a", "lamp_b", "cable_a"}),
        ("", "lamp", None, {"lamp_a", "lamp_b"}),
        ("", "lamp", 13000, {"lamp_a"}),
        ("", None, 0, set()),
        ("不存在的商品", None, None, set()),
    ],
)
def test_search_filters(
    keyword: str,
    category: str | None,
    max_price: int | None,
    expected_ids: set[str],
) -> None:
    results = make_market().search_listings(
        keyword=keyword,
        category=category,
        max_price=max_price,
    )

    # 这里只关心返回了哪些挂牌，用集合比较，避免把遍历顺序当成业务要求。
    assert {listing["listing_id"] for listing in results} == expected_ids


def test_public_result_hides_secrets_and_cannot_change_stock() -> None:
    market = make_market()
    result = market.get_listing("lamp_a")

    # 公开结果不能包含卖家底价或商品真值，否则 Agent 会得到内部答案。
    assert "reserve_price" not in result
    assert "truth_condition" not in result

    # 修改的是返回的字典快照，市场内部库存应保持原值。
    result["stock"] = 0

    assert market.get_listing("lamp_a")["stock"] == 1


def test_market_instances_do_not_share_stock() -> None:
    first = make_market()
    second = make_market()

    # 这里通过真正的状态修改入口改第一局，第二局应仍有自己的初始库存。
    first.set_stock("lamp_a", 0)

    assert first.get_listing("lamp_a")["stock"] == 0
    assert second.get_listing("lamp_a")["stock"] == 1


def test_negative_stock_is_rejected() -> None:
    market = make_market()

    # 检查对象创建后的再次赋值也受保护，而不只是在读取模板时检查。
    with pytest.raises(ValidationError, match="stock"):
        market.set_stock("lamp_a", -1)

    # 失败的赋值也不能破坏原来合法的库存。
    assert market.get_listing("lamp_a")["stock"] == 1


def test_unknown_listing_is_rejected() -> None:
    # 当前内部方法用 KeyError 表达查不到；后续工具接口会映射为错误响应。
    with pytest.raises(KeyError, match="NOT_FOUND"):
        make_market().get_listing("missing")
