"""本地 Web 服务：在浏览器里筛选查询库里的真实数据。

只读。DuckDB 是单写者，抓取任务运行时会独占锁，此时接口返回 503 并提示原因，
服务本身不退出 —— 抓完自动恢复。
"""
from __future__ import annotations

import threading
from datetime import date
from pathlib import Path
from typing import Any

import duckdb
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse

from .config import DB_PATH
from .sources.discover import RANK_SORTS

STATIC = Path(__file__).resolve().parent / "static"

# ORDER BY 不能参数化，只能白名单。键是前端传的值，值是真实列名。
SORTABLE = {
    "pledged_usd":    "pledged_usd",
    "percent_funded": "percent_funded",
    "backers_count":  "backers_count",
    "goal_usd":       "goal_usd",
    "launched_at":    "launched_at",
    "deadline":       "deadline",
    "days_remaining": "days_remaining",
}


class Db:
    """惰性只读连接。抓取任务持锁时自动重试，不让服务挂掉。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._con: duckdb.DuckDBPyConnection | None = None
        self._lock = threading.Lock()

    def _open(self):
        if not self.path.exists():
            raise HTTPException(503, f"数据库不存在：{self.path}，先跑 ks backfill")
        try:
            return duckdb.connect(str(self.path), read_only=True)
        except duckdb.IOException as exc:
            raise HTTPException(
                503, "数据库正被抓取任务独占（DuckDB 单写者）。等 ks rank / ks daily "
                     "跑完再刷新即可，服务不用重启。"
            ) from exc

    def query(self, sql: str, params: list | None = None) -> list[dict]:
        with self._lock:
            if self._con is None:
                self._con = self._open()
            try:
                cur = self._con.execute(sql, params or [])
            except duckdb.Error:
                # 连接可能已失效（库被重写），丢掉重开一次
                try:
                    self._con.close()
                except Exception:
                    pass
                self._con = None
                self._con = self._open()
                cur = self._con.execute(sql, params or [])
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]


db = Db(DB_PATH)
app = FastAPI(title="Kickstarter 数据查询", docs_url="/api/docs")


def _jsonable(rows: list[dict]) -> list[dict]:
    """date/datetime 转字符串，Decimal 转 float。"""
    out = []
    for r in rows:
        d = {}
        for k, v in r.items():
            if hasattr(v, "isoformat"):
                d[k] = v.isoformat()
            elif isinstance(v, (int, float, str, bool)) or v is None:
                d[k] = v
            else:
                d[k] = float(v) if hasattr(v, "__float__") else str(v)
        out.append(d)
    return out


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/meta")
def meta():
    """下拉框选项 + 库存概况。"""
    return {
        "parents": [r["大类"] for r in db.query(
            "SELECT DISTINCT 大类 FROM latest_zh WHERE 大类 IS NOT NULL ORDER BY 1")],
        "categories": db.query(
            "SELECT DISTINCT 大类, 子类 FROM latest_zh "
            "WHERE 子类 IS NOT NULL ORDER BY 1, 2"),
        "countries": [r["国家"] for r in db.query(
            "SELECT 国家, count(*) n FROM latest_zh WHERE 国家 IS NOT NULL "
            "GROUP BY 1 ORDER BY n DESC")],
        "states": db.query(
            "SELECT DISTINCT state, 状态 FROM latest_zh WHERE state IS NOT NULL ORDER BY 1"),
        "rank_sorts": [{"key": k, "label": v} for k, v in RANK_SORTS.items()],
        "snapshots": _jsonable(db.query(
            "SELECT snapshot_date, source, count(*) n FROM snapshots "
            "GROUP BY 1,2 ORDER BY 1 DESC, 2")),
        "totals": db.query(
            "SELECT count(*) AS row_count, count(DISTINCT project_id) AS projects "
            "FROM snapshots")[0],
    }


@app.get("/api/projects")
def projects(
    q: str | None = None,
    parent: str | None = None,
    category: str | None = None,
    country: str | None = None,
    state: str | None = None,
    min_usd: float | None = None,
    max_usd: float | None = None,
    min_pct: float | None = None,
    max_pct: float | None = None,
    min_backers: int | None = None,
    launched_after: str | None = None,
    days_left_max: float | None = None,
    has_video: bool | None = None,
    staff_pick: bool | None = None,
    sort: str = "pledged_usd",
    order: str = "desc",
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
):
    where: list[str] = []
    params: list[Any] = []

    def add(clause: str, *vals):
        where.append(clause)
        params.extend(vals)

    if q:
        add("(lower(name) LIKE ? OR lower(blurb) LIKE ?)", f"%{q.lower()}%", f"%{q.lower()}%")
    if parent:
        add("大类 = ?", parent)
    if category:
        add("子类 = ?", category)
    if country:
        add("国家 = ?", country)
    # state='live' 要查 live_now（排除"最后观测时是 live 但早已结束"的旧记录），
    # 其余状态照常查 latest_zh
    use_live_now = state == "live"
    if state and not use_live_now:
        add("state = ?", state)
    if min_usd is not None:
        add("pledged_usd >= ?", min_usd)
    if max_usd is not None:
        add("pledged_usd <= ?", max_usd)
    if min_pct is not None:
        add("percent_funded >= ?", min_pct)
    if max_pct is not None:
        add("percent_funded <= ?", max_pct)
    if min_backers is not None:
        add("backers_count >= ?", min_backers)
    if launched_after:
        add("launched_at >= ?", launched_after)
    if days_left_max is not None:
        add("days_remaining <= ? AND days_remaining >= 0", days_left_max)
    if has_video is not None:
        add("has_video = ?", has_video)
    if staff_pick is not None:
        add("staff_pick = ?", staff_pick)

    table = "live_now_zh" if use_live_now else "latest_zh"
    w = (" WHERE " + " AND ".join(where)) if where else ""
    col = SORTABLE.get(sort)
    if col is None:
        raise HTTPException(400, f"不支持的排序字段 {sort}，可选：{', '.join(SORTABLE)}")
    direction = "DESC" if order.lower() != "asc" else "ASC"

    total = db.query(f"SELECT count(*) n FROM {table}{w}", params)[0]["n"]
    rows = db.query(f"""
        SELECT project_id, name, blurb, 大类, 子类, 国家, 状态, state,
               goal_usd, pledged_usd, percent_funded, backers_count, currency,
               launched_at, deadline, days_remaining, duration_days,
               has_video, staff_pick, creator_name, project_url
        FROM {table}{w}
        ORDER BY {col} {direction} NULLS LAST
        LIMIT ? OFFSET ?
    """, params + [page_size, (page - 1) * page_size])

    return {"total": total, "page": page, "page_size": page_size,
            "rows": _jsonable(rows)}


@app.get("/api/leaderboard")
def leaderboard(sort: str = "most_funded", top: int = Query(100, ge=1, le=1000),
                day: str | None = None):
    if sort not in RANK_SORTS:
        raise HTTPException(400, f"未知榜单 {sort}")
    if day is None:
        got = db.query("SELECT max(snapshot_date) d FROM rankings WHERE sort = ?", [sort])
        day = got[0]["d"] if got and got[0]["d"] else None
    if day is None:
        return {"sort": sort, "day": None, "rows": [],
                "hint": f"还没抓过这个榜，先跑：ks rank --sort {sort}"}

    rows = db.query("""
        SELECT b.rank, b.name, b.blurb, b.project_id,
               dp.parent_zh AS 大类, dc.category_zh AS 子类, dn.country_zh AS 国家,
               ds.state_zh  AS 状态,
               b.goal_usd, b.pledged_usd, b.percent_funded, b.backers_count,
               b.currency, b.launched_at, b.deadline, b.days_remaining, b.project_url,
               m.rank_gain, m.is_new_entry, m.prev_rank
        FROM leaderboard b
        LEFT JOIN dim_parent   dp ON dp.parent = b.category_parent_name
        LEFT JOIN dim_category dc ON dc.parent IS NOT DISTINCT FROM b.category_parent_name
                                 AND dc.category = b.category_name
        LEFT JOIN dim_country  dn ON dn.country = b.country
        LEFT JOIN dim_state    ds ON ds.state   = b.state
        LEFT JOIN rank_moves   m  ON m.project_id = b.project_id
                                 AND m.snapshot_date = b.snapshot_date AND m.sort = b.sort
        WHERE b.snapshot_date = ? AND b.sort = ?
        ORDER BY b.rank LIMIT ?
    """, [day, sort, top])
    return {"sort": sort, "label": RANK_SORTS[sort], "day": str(day),
            "rows": _jsonable(rows)}


@app.get("/api/stats")
def stats(by: str = "parent", state: str | None = "live"):
    """品类/国家维度汇总。"""
    dim = {"parent": "大类", "category": "子类", "country": "国家"}.get(by)
    if dim is None:
        raise HTTPException(400, "by 只能是 parent / category / country")
    table = "live_now_zh" if state == "live" else "latest_zh"
    where, params = "", []
    if state and state != "live":
        where, params = " WHERE state = ?", [state]
    return {"by": by, "rows": _jsonable(db.query(f"""
        SELECT {dim} AS 维度, count(*) AS 项目数,
               round(sum(pledged_usd))    AS 募集总额USD,
               round(median(pledged_usd)) AS 募集中位USD,
               round(median(goal_usd))    AS 目标中位USD,
               round(avg(percent_funded), 1) AS 平均完成率,
               sum(backers_count)         AS 支持者总数
        FROM {table}{where}
        GROUP BY 1 HAVING {dim} IS NOT NULL
        ORDER BY 募集总额USD DESC NULLS LAST
    """, params))}
