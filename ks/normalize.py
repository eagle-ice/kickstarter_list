"""把两个数据源的原始记录压平成同一张表。

Web Robots 的 dump 和站内 discover 接口返回的是**同一个 project 对象**
(实测确认，42 个顶层字段一致)，所以这里只需要一个 normalizer。
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

# 压平后的列顺序 —— db.py 的建表语句和这里必须保持一致
COLUMNS = [
    "snapshot_date", "source", "run_id",
    "project_id", "name", "blurb", "slug", "state",
    "goal", "pledged", "usd_pledged", "converted_pledged_amount", "percent_funded",
    "backers_count", "currency", "current_currency", "static_usd_rate", "fx_rate",
    "country", "country_displayable_name",
    "created_at", "launched_at", "deadline", "state_changed_at",
    "duration_days", "days_remaining",
    "category_id", "category_name", "category_parent_name", "category_slug",
    "location_id", "location_name", "location_state", "location_country",
    "creator_id", "creator_name", "creator_slug",
    "staff_pick", "spotlight", "is_starrable", "disable_communication",
    "prelaunch_activated", "is_launched", "is_post_campaign_pledging",
    "has_video", "photo_url", "project_url",
]


def _ts_to_dt(value: Any) -> datetime | None:
    """Unix 秒 -> aware datetime。KS 的时间戳全是 UTC 秒。"""
    if value in (None, "", 0):
        return None
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    except (ValueError, OSError, TypeError):
        return None


def _num(value: Any) -> float | None:
    """usd_pledged 在 dump 里是字符串，goal/pledged 是数字，统一成 float。"""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def _category_parts(cat: dict) -> tuple[Any, Any, Any, Any]:
    """category.slug 形如 'games/tabletop-games'，父类目要从 slug 或 parent_name 取。

    顶级品类（slug 无 '/'，如 'music'）本身就是父类目 —— 否则这些项目在按
    category_parent_name 分组的报表里会落进一个空分类。
    """
    if not cat:
        return None, None, None, None
    slug = cat.get("slug") or ""
    name = cat.get("name")
    parent = cat.get("parent_name")
    if not parent:
        parent = slug.split("/", 1)[0].replace("-", " ").title() if "/" in slug else name
    return cat.get("id"), name, parent, slug


def normalize(raw: dict, *, snapshot_date: date, source: str, run_id: str | None = None) -> dict:
    """单条 project 对象 -> 压平的 dict。缺字段一律给 None，不抛异常。"""
    cat_id, cat_name, cat_parent, cat_slug = _category_parts(raw.get("category") or {})
    loc = raw.get("location") or {}
    creator = raw.get("creator") or {}
    urls = (raw.get("urls") or {}).get("web") or {}

    launched = _ts_to_dt(raw.get("launched_at"))
    deadline = _ts_to_dt(raw.get("deadline"))

    duration_days = None
    if launched and deadline:
        duration_days = round((deadline - launched).total_seconds() / 86400, 2)

    # 相对快照日算剩余天数，回溯历史快照时才有意义
    days_remaining = None
    if deadline:
        snap_dt = datetime(snapshot_date.year, snapshot_date.month, snapshot_date.day, tzinfo=timezone.utc)
        days_remaining = round((deadline - snap_dt).total_seconds() / 86400, 2)

    goal = _num(raw.get("goal"))
    pledged = _num(raw.get("pledged"))
    # percent_funded 不是每条都有（discover 接口偶尔缺），缺了就自己算
    pct = raw.get("percent_funded")
    pct = _num(pct) if pct is not None else (round(pledged / goal * 100, 4) if goal else None)

    return {
        "snapshot_date": snapshot_date,
        "source": source,
        "run_id": run_id,
        "project_id": raw.get("id"),
        "name": raw.get("name"),
        "blurb": raw.get("blurb"),
        "slug": raw.get("slug"),
        "state": raw.get("state"),
        "goal": goal,
        "pledged": pledged,
        "usd_pledged": _num(raw.get("usd_pledged")),
        "converted_pledged_amount": _num(raw.get("converted_pledged_amount")),
        "percent_funded": pct,
        "backers_count": raw.get("backers_count"),
        "currency": raw.get("currency"),
        "current_currency": raw.get("current_currency"),
        "static_usd_rate": _num(raw.get("static_usd_rate")),
        "fx_rate": _num(raw.get("fx_rate")),
        "country": raw.get("country"),
        "country_displayable_name": raw.get("country_displayable_name"),
        "created_at": _ts_to_dt(raw.get("created_at")),
        "launched_at": launched,
        "deadline": deadline,
        "state_changed_at": _ts_to_dt(raw.get("state_changed_at")),
        "duration_days": duration_days,
        "days_remaining": days_remaining,
        "category_id": cat_id,
        "category_name": cat_name,
        "category_parent_name": cat_parent,
        "category_slug": cat_slug,
        "location_id": loc.get("id"),
        "location_name": loc.get("name"),
        "location_state": loc.get("state"),
        "location_country": loc.get("country"),
        "creator_id": creator.get("id"),
        "creator_name": creator.get("name"),
        "creator_slug": creator.get("slug"),
        "staff_pick": raw.get("staff_pick"),
        "spotlight": raw.get("spotlight"),
        "is_starrable": raw.get("is_starrable"),
        "disable_communication": raw.get("disable_communication"),
        "prelaunch_activated": raw.get("prelaunch_activated"),
        "is_launched": raw.get("is_launched"),
        "is_post_campaign_pledging": raw.get("is_in_post_campaign_pledging_phase"),
        "has_video": bool(raw.get("video")),
        "photo_url": (raw.get("photo") or {}).get("full"),
        "project_url": urls.get("project"),
    }


def to_row(rec: dict) -> tuple:
    """按 COLUMNS 顺序取值，喂给 executemany。"""
    return tuple(rec.get(c) for c in COLUMNS)
