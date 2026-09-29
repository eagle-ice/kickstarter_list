"""日报与分析查询。对应四类用途：选品 / 竞品监控 / 市场研究 / 发起对标。"""
from __future__ import annotations

from datetime import date, timedelta

import os
import sys

from rich.console import Console
from rich.table import Table
from rich.text import Text

from . import i18n

# 重定向到文件/管道时 rich 默认只给 80 列，表格会挤成一团。
# 非 TTY 时给一个够用的固定宽度；KS_WIDTH 可覆盖。
_WIDTH = int(os.environ.get("KS_WIDTH") or 0) or (None if sys.stdout.isatty() else 120)
console = Console(width=_WIDTH)

NAME_WIDTH = 26


def money(v) -> str:
    """金额统一成千分位整数。终端里小数点后两位纯属噪音。"""
    if v is None:
        return ""
    return f"{round(float(v)):,}"


def pct(v) -> str:
    if v is None:
        return ""
    v = float(v)
    return f"{v:,.0f}%" if v >= 100 else f"{v:.1f}%"


def _link(name, url) -> Text:
    """项目名截断 + 挂终端超链接，这样就不用单独占一个链接列。"""
    label = name or ""
    if len(label) > NAME_WIDTH:
        label = label[: NAME_WIDTH - 1] + "…"
    t = Text(label, overflow="ellipsis", no_wrap=True)
    if url:
        t.stylize(f"link {url}")
    return t


def _table(title: str, cols, rows: list[tuple]) -> Table:
    """cols 支持 "名称" 或 ("名称", {rich column kwargs})。"""
    t = Table(title=title, header_style="bold cyan", title_style="bold",
              expand=False, pad_edge=False)
    for c in cols:
        if isinstance(c, tuple):
            t.add_column(c[0], **c[1])
        else:
            t.add_column(c, overflow="ellipsis", no_wrap=True)
    for r in rows:
        t.add_row(*[v if isinstance(v, Text) else ("" if v is None else str(v)) for v in r])
    return t


# 数字列给足最小宽度：金额被截成 "17,…" 等于没有，宁可挤掉文本列
NUM = {"justify": "right", "no_wrap": True, "min_width": 8}
TXT = {"overflow": "ellipsis", "no_wrap": True}
# 子类这种可长可短的文本列，限死上限免得吃掉数字列的空间
TXT_S = {"overflow": "ellipsis", "no_wrap": True, "max_width": 10}


def latest_snapshot_date(con) -> date | None:
    got = con.execute("SELECT max(snapshot_date) FROM snapshots").fetchone()
    return got[0] if got else None


# ---------------------------------------------------------------- 选品 / 找爆款
# 注意：所有跨项目的金额比较一律用 *_usd 列。项目金额以本币计价，
# 按本币排序会让日元/港币项目系统性地压过美元项目（见 db.py 的 USD_RATE_EXPR）。

def new_launches(con, day: date, limit: int = 30):
    """当天新上线的项目。"""
    return con.execute("""
        SELECT name, category_parent_name, category_name, country,
               goal_usd, pledged_usd, percent_funded, backers_count, project_url
        FROM latest
        WHERE launched_at::DATE = ?
        ORDER BY pledged_usd DESC NULLS LAST
        LIMIT ?
    """, [day, limit]).fetchall()


def top_movers(con, day: date, limit: int = 30):
    """快照间筹款增量最大的项目 —— 找正在起量的爆款。"""
    return con.execute("""
        SELECT name, category_parent_name, country,
               pledged_delta_usd, backers_delta,
               pledged_usd, percent_funded, days_remaining, project_url
        FROM deltas
        WHERE snapshot_date = ? AND pledged_delta_usd > 0
        ORDER BY pledged_delta_usd DESC
        LIMIT ?
    """, [day, limit]).fetchall()


def fast_starters(con, day: date, max_age_days: int = 7, limit: int = 30):
    """上线 N 天内就冲得很猛的 —— 早期增长斜率是爆款最强信号。"""
    return con.execute("""
        SELECT name, category_parent_name,
               date_diff('day', launched_at::DATE, snapshot_date) AS age_days,
               percent_funded, pledged_usd, backers_count, goal_usd, project_url
        FROM live_now
        WHERE 1=1
          AND launched_at IS NOT NULL
          AND date_diff('day', launched_at::DATE, snapshot_date) BETWEEN 0 AND ?
          AND percent_funded >= 100
        ORDER BY percent_funded / greatest(date_diff('day', launched_at::DATE, snapshot_date), 1) DESC
        LIMIT ?
    """, [max_age_days, limit]).fetchall()


# ---------------------------------------------------------------- 状态流转

def state_changes(con, day: date, limit: int = 50):
    return con.execute("""
        SELECT name, prev_state, state, category_parent_name,
               pledged_usd, percent_funded, project_url
        FROM deltas
        WHERE snapshot_date = ? AND state_changed
        ORDER BY pledged_usd DESC NULLS LAST
        LIMIT ?
    """, [day, limit]).fetchall()


# ---------------------------------------------------------------- 竞品 / 品类监控

def category_heat(con, day: date, limit: int = 25):
    """品类热度：在筹数、募集总额(USD)、区间增量(USD)。"""
    return con.execute("""
        SELECT l.category_parent_name AS parent,
               count(*) AS live_projects,
               sum(l.pledged_usd) AS total_pledged_usd,
               avg(l.percent_funded) AS avg_pct,
               sum(d.pledged_delta_usd) AS delta_usd
        FROM live_now l
        LEFT JOIN deltas d
               ON d.project_id = l.project_id AND d.snapshot_date = ?
        WHERE l.category_parent_name IS NOT NULL
        GROUP BY 1
        ORDER BY delta_usd DESC NULLS LAST, total_pledged_usd DESC
        LIMIT ?
    """, [day, limit]).fetchall()


def keyword_watch(con, keywords: list[str], limit: int = 40):
    """关键词命中 name/blurb 的在筹项目。"""
    if not keywords:
        return []
    clause = " OR ".join(["(lower(name) LIKE ? OR lower(blurb) LIKE ?)"] * len(keywords))
    params: list[object] = []
    for kw in keywords:
        pat = f"%{kw.lower()}%"
        params += [pat, pat]
    params.append(limit)
    return con.execute(f"""
        SELECT name, category_parent_name, category_name, country,
               state, pledged_usd, percent_funded,
               backers_count, days_remaining, project_url
        FROM live_now
        WHERE 1=1 AND ({clause})
        ORDER BY pledged_usd DESC NULLS LAST
        LIMIT ?
    """, params).fetchall()


# ---------------------------------------------------------------- 发起对标

def benchmark(con, category: str | None = None, parent: str | None = None):
    """同类目成功/失败项目的目标额、周期、客单价分布（美元口径）。"""
    where = ["state IN ('successful', 'failed')", "goal_usd > 0", "duration_days IS NOT NULL"]
    params: list[object] = []
    if parent:
        where.append("category_parent_name = ?"); params.append(parent)
    if category:
        where.append("category_name = ?"); params.append(category)
    w = " AND ".join(where)
    return con.execute(f"""
        SELECT state,
               count(*) AS n,
               round(median(goal_usd))        AS median_goal,
               round(median(pledged_usd))     AS median_pledged,
               round(median(backers_count))   AS median_backers,
               round(median(duration_days), 1) AS median_days,
               round(median(CASE WHEN backers_count > 0 THEN pledged_usd / backers_count END), 1) AS median_per_backer,
               round(100.0 * sum(CASE WHEN has_video THEN 1 ELSE 0 END) / count(*), 1) AS pct_with_video,
               round(100.0 * sum(CASE WHEN staff_pick THEN 1 ELSE 0 END) / count(*), 1) AS pct_staff_pick
        FROM latest WHERE {w}
        GROUP BY state ORDER BY state
    """, params).fetchall()


def success_rate_by_goal(con, parent: str | None = None):
    """不同目标额区间的成功率（美元口径）—— 定目标金额最直接的依据。"""
    params: list[object] = []
    extra = ""
    if parent:
        extra = "AND category_parent_name = ?"; params.append(parent)
    return con.execute(f"""
        SELECT CASE
                 WHEN goal_usd <  1000  THEN 'a <1k'
                 WHEN goal_usd <  5000  THEN 'b 1k-5k'
                 WHEN goal_usd < 10000  THEN 'c 5k-10k'
                 WHEN goal_usd < 50000  THEN 'd 10k-50k'
                 WHEN goal_usd < 100000 THEN 'e 50k-100k'
                 ELSE                        'f >100k'
               END AS goal_band,
               count(*) AS n,
               round(100.0 * sum(CASE WHEN state = 'successful' THEN 1 ELSE 0 END) / count(*), 1) AS success_rate,
               round(median(backers_count)) AS median_backers
        FROM latest
        WHERE state IN ('successful', 'failed') AND goal_usd > 0 {extra}
        GROUP BY 1 ORDER BY 1
    """, params).fetchall()


# ---------------------------------------------------------------- 日报编排

def daily_report(con, day: date | None = None, *, keywords: list[str] | None = None) -> None:
    day = day or latest_snapshot_date(con)
    if day is None:
        console.print("[yellow]库里还没有数据，先跑 backfill / daily[/yellow]")
        return

    console.rule(f"[bold]Kickstarter 日报 {day}")
    total = con.execute("SELECT count(*) FROM live_now").fetchone()[0]
    n_delta = con.execute(
        "SELECT count(*) FROM deltas WHERE snapshot_date = ?", [day]).fetchone()[0]
    console.print(f"在筹项目 [bold]{total:,}[/bold]　可比对增量记录 [bold]{n_delta:,}[/bold]")
    console.print("[dim]项目名可点击直接打开页面[/dim]\n")

    # ① 当日新上线
    rows = new_launches(con, day, 15)
    console.print(_table(
        f"① 当日新上线 ({len(rows)})",
        [("项目", TXT), ("大类", TXT), ("子类", TXT), ("国家", TXT),
         ("目标$", NUM), ("已筹$", NUM), ("完成率", NUM), ("支持者", NUM)],
        [(_link(r[0], r[8]), i18n.parent(r[1]), i18n.category(r[2], r[1]),
          i18n.country(r[3]), money(r[4]), money(r[5]), pct(r[6]),
          f"{r[7]:,}" if r[7] else "0") for r in rows]))

    if n_delta:
        # ② 筹款增量
        rows = top_movers(con, day, 15)
        console.print(_table(
            "② 筹款增量 Top（美元，相邻快照之间）",
            [("项目", TXT), ("大类", TXT), ("增量$", NUM), ("增量人", NUM),
             ("累计$", NUM), ("剩余天", NUM)],
            [(_link(r[0], r[8]), i18n.parent(r[1]), money(r[3]),
              f"{r[4]:,}" if r[4] else "0", money(r[5]),
              f"{r[7]:.0f}" if r[7] is not None else "") for r in rows]))

        # ③ 状态流转
        rows = state_changes(con, day, 15)
        if rows:
            console.print(_table(
                "③ 状态流转",
                [("项目", TXT), ("原状态", TXT), ("新状态", TXT), ("大类", TXT),
                 ("已筹$", NUM), ("完成率", NUM)],
                [(_link(r[0], r[6]), i18n.state(r[1]), i18n.state(r[2]),
                  i18n.parent(r[3]), money(r[4]), pct(r[5])) for r in rows]))
    else:
        console.print("[dim]（增量需要同一项目至少两次快照）[/dim]\n")

    # ④ 早期爆款信号
    rows = fast_starters(con, day, 7, 12)
    console.print(_table(
        "④ 上线 7 天内已达标（早期爆款信号）",
        [("项目", TXT), ("大类", TXT), ("天数", NUM), ("完成率", NUM),
         ("已筹$", NUM), ("支持者", NUM), ("目标$", NUM)],
        [(_link(r[0], r[7]), i18n.parent(r[1]), str(r[2]), pct(r[3]), money(r[4]),
          f"{r[5]:,}" if r[5] else "0", money(r[6])) for r in rows]))

    # ⑤ 品类热度
    rows = category_heat(con, day, 15)
    console.print(_table(
        "⑤ 品类热度",
        [("大类", TXT), ("在筹数", NUM), ("累计募集$", NUM), ("平均完成率", NUM), ("区间增量$", NUM)],
        [(i18n.parent(r[0]), f"{r[1]:,}", money(r[2]), pct(r[3]), money(r[4]))
         for r in rows]))

    # ⑥ 关键词监控
    if keywords:
        rows = keyword_watch(con, keywords, 20)
        console.print(_table(
            f"⑥ 关键词监控 {keywords}（命中 {len(rows)}）",
            [("项目", TXT), ("大类", TXT), ("子类", TXT), ("国家", TXT),
             ("已筹$", NUM), ("完成率", NUM), ("支持者", NUM), ("剩余天", NUM)],
            [(_link(r[0], r[9]), i18n.parent(r[1]), i18n.category(r[2], r[1]),
              i18n.country(r[3]), money(r[5]), pct(r[6]),
              f"{r[7]:,}" if r[7] else "0", f"{r[8]:.0f}" if r[8] is not None else "")
             for r in rows]))


# ---------------------------------------------------------------- 榜单

def _rank_move(gain, is_new) -> str:
    """名次变动。数字变小=上升，所以 gain 为正代表往前冲了几名。"""
    if is_new:
        return "[cyan]NEW[/cyan]"
    if gain is None or gain == 0:
        return "[dim]—[/dim]"
    if gain > 0:
        return f"[green]↑{gain}[/green]"
    return f"[red]↓{abs(gain)}[/red]"


def leaderboard(con, day: date | None, sort: str, *, limit: int = 30,
                moves: bool = True) -> None:
    from .sources.discover import RANK_SORTS

    if day is None:
        got = con.execute(
            "SELECT max(snapshot_date) FROM rankings WHERE sort = ?", [sort]).fetchone()
        day = got[0] if got else None
    if day is None:
        console.print(f"[yellow]还没有 {sort} 榜单数据，先跑 ks rank --sort {sort}[/yellow]")
        return

    total = con.execute(
        "SELECT count(*) FROM rankings WHERE snapshot_date = ? AND sort = ?",
        [day, sort]).fetchone()[0]
    if not total:
        console.print(f"[yellow]{day} 没有 {sort} 榜单数据[/yellow]")
        return

    title = RANK_SORTS.get(sort, sort)
    console.rule(f"[bold]{title}　{day}　共 {total:,} 名")

    rows = con.execute("""
        SELECT b.rank, b.name, b.category_parent_name, b.category_name, b.country,
               b.pledged_usd, b.goal_usd, b.percent_funded, b.backers_count,
               b.days_remaining, b.project_url,
               m.rank_gain, m.is_new_entry
        FROM leaderboard b
        LEFT JOIN rank_moves m
               ON m.project_id = b.project_id AND m.snapshot_date = b.snapshot_date
              AND m.sort = b.sort
        WHERE b.snapshot_date = ? AND b.sort = ?
        ORDER BY b.rank
        LIMIT ?
    """, [day, sort, limit]).fetchall()

    cols = [("#", NUM)]
    if moves:
        cols.append(("变动", {"justify": "right", "no_wrap": True}))
    cols += [("项目", TXT), ("大类", TXT), ("子类", TXT), ("国家", TXT),
             ("已筹$", NUM), ("完成率", NUM), ("支持者", NUM), ("剩余天", NUM)]

    data = []
    for r in rows:
        row = [str(r[0])]
        if moves:
            row.append(_rank_move(r[11], r[12]))
        row += [_link(r[1], r[10]), i18n.parent(r[2]), i18n.category(r[3], r[2]),
                i18n.country(r[4]), money(r[5]), pct(r[7]),
                f"{r[8]:,}" if r[8] else "0",
                f"{r[9]:.0f}" if r[9] is not None else ""]
        data.append(tuple(row))
    console.print(_table(f"前 {len(rows)} 名", cols, data))

    if moves:
        _rank_highlights(con, day, sort)


def _rank_highlights(con, day: date, sort: str, limit: int = 8) -> None:
    """名次冲得最快的和新进榜的 —— 榜单的真正信号在变动里，不在静态排名。"""
    rise = con.execute("""
        SELECT m.rank_gain, m.rank, m.prev_rank, b.name, b.category_parent_name,
               b.pledged_usd, b.project_url
        FROM rank_moves m
        JOIN leaderboard b ON b.project_id = m.project_id
                          AND b.snapshot_date = m.snapshot_date AND b.sort = m.sort
        WHERE m.snapshot_date = ? AND m.sort = ? AND m.rank_gain > 0
        ORDER BY m.rank_gain DESC LIMIT ?
    """, [day, sort, limit]).fetchall()
    if rise:
        console.print(_table(
            "名次上升最快",
            [("上升", NUM), ("现名次", NUM), ("原名次", NUM), ("项目", TXT),
             ("大类", TXT), ("已筹$", NUM)],
            [(f"↑{r[0]}", str(r[1]), str(r[2]), _link(r[3], r[6]),
              i18n.parent(r[4]), money(r[5])) for r in rise]))

    new = con.execute("""
        SELECT m.rank, b.name, b.category_parent_name, b.pledged_usd,
               b.percent_funded, b.project_url
        FROM rank_moves m
        JOIN leaderboard b ON b.project_id = m.project_id
                          AND b.snapshot_date = m.snapshot_date AND b.sort = m.sort
        WHERE m.snapshot_date = ? AND m.sort = ? AND m.is_new_entry
        ORDER BY m.rank LIMIT ?
    """, [day, sort, limit]).fetchall()
    if new:
        console.print(_table(
            "新进榜",
            [("#", NUM), ("项目", TXT), ("大类", TXT), ("已筹$", NUM), ("完成率", NUM)],
            [(str(r[0]), _link(r[1], r[5]), i18n.parent(r[2]), money(r[3]), pct(r[4]))
             for r in new]))
