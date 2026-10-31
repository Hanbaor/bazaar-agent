def matches_item(item_id: str, listing: dict) -> bool:
    specs = listing["specs"]
    if item_id == "lamp":
        return (
            listing["category"] == "lamp"
            and specs.get("dimmable") is True
            and specs.get("power_source") == "USB"
        )
    if item_id == "cable":
        power = specs.get("power_w")
        return (
            listing["category"] == "cable"
            and specs.get("length_cm") == 100
            and type(power) is int
            and power >= 60
            and specs.get("connector") == "USB-C"
        )
    if item_id == "organizer":
        return listing["category"] == "organizer" and specs.get("usage") == "desktop"
    return False


# 用途：判断公开描述是否声称全新；这只是公开说法，实际成色仍由独立验收核对。
def public_new_claim(listing: dict) -> bool:
    # 这是公开描述中的声明，不是真值保证。最终成色由 judge 单独核对。
    return "全新" in listing["description"]
