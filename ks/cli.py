"""命令行入口：python -m ks <command>"""
from __future__ import annotations

import argparse
import sys
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

from rich.console import Console

from . import db, i18n, report
from .config import Config
from .normalize import COLUMNS, normalize, to_row
from .sources import discover as disc
from .sources import webrobots as wr

# 后台或重定向运行时 stdout 默认全缓冲，backfill/daily 这种长任务会一行不输出。
# 强制行缓冲，让进度能实时看到。
try:
    sys.stdout.reconfigure(line_buffering=True)
except (AttributeError, ValueError):
    pass

console = Console()
BATCH = 20_000
COL_PID = COLUMNS.index("project_id")


def _now():
    return datetime.now(timezone.utc)


# ----------------------------------------------------------------- backfill

def cmd_backfill(args) -> int:
    """从 Web Robots 月度 dump 导入历史基线。"""
    cfg = Config.load(args.config)
    dumps = wr.list_dumps(cfg)
    if not dumps:
        console.print("[red]没取到任何 dump 链接[/red]")
        return 1

    if args.list:
        console.print(f"共 {len(dumps)} 份 dump，{dumps[-1].dump_date} ~ {dumps[0].dump_date}")
        for d in dumps[: args.limit or 20]:
            console.print(f"  {d.dump_date}  {d.filename}")
        return 0

    picked = dumps[: args.count]
    if args.since:
        since = date.fromisoformat(args.since)
        picked = [d for d in dumps if d.dump_date >= since]

    con = db.connect(args.db)
    for dump in sorted(picked, key=lambda d: d.dump_date):
        if db.has_snapshot(con, dump.dump_date, "webrobots") and not args.force:
            console.print(f"[dim]跳过 {dump.dump_date}（已入库，--force 可覆盖）[/dim]")
            continue

        started = _now()
        run_id = f"wr-{dump.dump_date}-{uuid.uuid4().hex[:8]}"
        console.print(f"导入 [bold]{dump.dump_date}[/bold] ...")

        source_iter = (
            wr.iter_records_stream(dump) if args.no_cache
            else wr.iter_records(wr.download(dump))
        )

        con.execute("DELETE FROM snapshots WHERE snapshot_date = ? AND source = 'webrobots'",
                    [dump.dump_date])
        buf, total, seen = [], 0, set()
        for raw, rid in source_iter:
            pid = raw.get("id")
            if pid in seen:          # dump 内部同一项目可能被多条检索路径重复收录
                continue
            seen.add(pid)
            buf.append(to_row(normalize(raw, snapshot_date=dump.dump_date,
                                        source="webrobots", run_id=rid or run_id)))
            if len(buf) >= BATCH:
                total += db.insert_rows(con, buf); buf.clear()
                console.print(f"  ... {total:,}", highlight=False)
        total += db.insert_rows(con, buf)

        db.log_ingest(con, run_id, "webrobots", dump.dump_date, total,
                      started, _now(), f"dump={dump.filename}")
        console.print(f"[green]✓ {dump.dump_date} 入库 {total:,} 条[/green]")
    con.close()
    return 0


# -------------------------------------------------------------------- daily

def _last_snapshot(con, source: str = "discover"):
    got = con.execute(
        "SELECT max(snapshot_date) FROM snapshots WHERE source = ?", [source]).fetchone()
    return got[0] if got else None


def _last_observation_before(con, day: date, source: str = "discover"):
    """今天之前最近一次观测日。

    不能直接用 max(snapshot_date) 当增量起点：当天先跑过 ks new 之后，
    max 就是今天，起点被推到今天，中间那几天结束的项目会被整段跳过。
    """
    got = con.execute(
        "SELECT max(snapshot_date) FROM snapshots WHERE source = ? AND snapshot_date < ?",
        [source, day]).fetchone()
    return got[0] if got else None


def cmd_new(args) -> int:
    """增量抓取：只拉上线时间晚于 since 的新项目。

    注意这只补「新上线」。已有项目的筹款金额每天都在变，增量模式拿不到它们的
    最新值，也就算不出日增量 —— 那必须重新抓一遍，见 ks daily / ks rank。
    """
    cfg = Config.load(args.config)
    if args.max_pages:
        cfg.max_pages_per_sort = args.max_pages

    con = db.connect(args.db)
    day = date.fromisoformat(args.date) if args.date else date.today()

    if args.since:
        since_d = date.fromisoformat(args.since)
    else:
        since_d = _last_snapshot(con)
        if since_d is None:
            console.print("[yellow]库里还没有快照，无法推断增量起点。"
                          "先跑 ks backfill 或 ks daily 建基线，或用 --since 指定日期[/yellow]")
            con.close()
            return 1
    since_dt = datetime(since_d.year, since_d.month, since_d.day, tzinfo=timezone.utc)

    console.print(f"增量抓取：只取 [bold]{since_d}[/bold] 之后上线的项目")
    run_id = f"new-{day}-{uuid.uuid4().hex[:8]}"
    started = _now()
    q = disc.Query(sort="newest", state=args.state)

    collected: dict[int, dict] = {}
    with disc.DiscoverClient(cfg) as client:
        for proj in client.iter_since(q, since_dt, max_pages=args.max_pages or 200):
            pid = proj.get("id")
            if pid:
                collected[pid] = proj
                if len(collected) % 60 == 0:
                    console.print(f"  ... 已取 {len(collected)}")

    if not collected:
        console.print(f"[green]✓ {since_d} 之后没有新上线项目[/green]")
        db.log_ingest(con, run_id, "discover", day, 0, started, _now(), "incremental: 0 new")
        con.close()
        return 0

    rows = [to_row(normalize(p, snapshot_date=day, source="discover", run_id=run_id))
            for p in collected.values()]
    # 增量是追加语义：同一天可能已有 daily 抓的数据，不能整批覆盖
    existing = {r[0] for r in con.execute(
        "SELECT project_id FROM snapshots WHERE snapshot_date = ? AND source = 'discover'",
        [day]).fetchall()}
    fresh = [r for r in rows if r[COL_PID] not in existing]
    n = db.insert_rows(con, fresh)
    db.log_ingest(con, run_id, "discover", day, n, started, _now(),
                  f"incremental since={since_d}")
    console.print(f"[green]✓ 新上线 {len(collected)} 个，入库 {n} 条"
                  f"（{len(collected) - n} 条当天已有，跳过）[/green]")

    if not args.no_report:
        console.print()
        rows_out = report.new_launches(con, day, 20)
        console.print(report._table(
            f"{since_d} 之后新上线（{len(collected)} 个）",
            [("项目", report.TXT), ("子类", report.TXT_S), ("国家", report.TXT_S),
             ("目标$", report.NUM), ("已筹$", report.NUM),
             ("完成率", report.NUM), ("支持者", report.NUM)],
            [(report._link(r[0], r[8]), i18n.category(r[2], r[1]),
              i18n.country(r[3]), report.money(r[4]), report.money(r[5]),
              report.pct(r[6]), f"{r[7]:,}" if r[7] else "0") for r in rows_out]))
    con.close()
    return 0


def cmd_outcomes(args) -> int:
    """补最近结束项目的最终结果（successful / failed）。

    只抓 state=live 的话，项目一结束就从结果里消失，库里会一直留着过期的
    live 记录，也拿不到任何项目的成败结局。
    """
    cfg = Config.load(args.config)
    if args.delay:
        cfg.request_delay = (args.delay, args.delay * 1.8)

    day = date.fromisoformat(args.date) if args.date else date.today()
    con = db.connect(args.db)

    if args.since:
        since_d = date.fromisoformat(args.since)
    else:
        since_d = _last_observation_before(con, day) or day
    since_dt = datetime(since_d.year, since_d.month, since_d.day, tzinfo=timezone.utc)

    console.print(f"补结局：拉 [bold]{since_d}[/bold] 之后结束的项目"
                  f"（跨 {(day - since_d).days} 天）")
    run_id = f"out-{day}-{uuid.uuid4().hex[:8]}"
    started = _now()
    collected: dict[int, dict] = {}

    def on_state(st, n):
        console.print(f"  [cyan]{i18n.state(st)}[/cyan] {n} 个")

    try:
        with disc.DiscoverClient(cfg) as client:
            for st, proj in client.iter_recently_ended(
                    since_dt, on_state=on_state):
                pid = proj.get("id")
                if pid:
                    collected[pid] = proj
    except Exception as exc:
        console.print(f"[red]中断：{exc}[/red]　已抓到的仍会入库")

    if not collected:
        console.print("[yellow]没抓到最近结束的项目[/yellow]")
        con.close()
        return 0

    rows = [to_row(normalize(p, snapshot_date=day, source="discover", run_id=run_id))
            for p in collected.values()]
    existing = {r[0] for r in con.execute(
        "SELECT project_id FROM snapshots WHERE snapshot_date = ? AND source = 'discover'",
        [day]).fetchall()}
    fresh = [r for r in rows if r[COL_PID] not in existing]
    n = db.insert_rows(con, fresh)
    db.log_ingest(con, run_id, "discover", day, n, started, _now(),
                  f"outcomes since={since_d}")
    console.print(f"[green]✓ 结束项目 {len(collected)} 个，入库 {n} 条"
                  f"（{len(collected) - n} 条当天已有）[/green]")

    # 还有多少"最后观测时是 live 但早就过了截止日"的残留
    stale = con.execute("""
        SELECT count(*) FROM latest
        WHERE state = 'live' AND deadline < current_timestamp
    """).fetchone()[0]
    if stale:
        console.print(f"[dim]仍有 {stale} 个项目标记为 live 但已过截止日，"
                      f"可加大 --since 范围继续补[/dim]")
    con.close()
    return 0


def cmd_resolve(args) -> int:
    """定点解析状态过期的项目：标记为 live 但已过截止日的那批。

    `ks outcomes` 走的是 discover 的已结束列表，而那个列表并不完整。
    这里对已知 id 的项目逐个 term 搜索兜底，准确但慢。
    """
    cfg = Config.load(args.config)
    if args.delay:
        cfg.request_delay = (args.delay, args.delay * 1.8)

    day = date.fromisoformat(args.date) if args.date else date.today()
    con = db.connect(args.db)
    stale = con.execute("""
        SELECT project_id, name FROM latest
        WHERE state = 'live' AND deadline < current_timestamp
        ORDER BY deadline LIMIT ?
    """, [args.limit]).fetchall()

    if not stale:
        console.print("[green]没有状态过期的项目[/green]")
        con.close()
        return 0

    console.print(f"定点解析 [bold]{len(stale)}[/bold] 个状态过期的项目"
                  f"（每个一次搜索，约 {len(stale) * 5 / 60:.0f} 分钟）")
    run_id = f"rsv-{day}-{uuid.uuid4().hex[:8]}"
    started = _now()
    resolved: dict[int, dict] = {}
    missed = 0

    try:
        with disc.DiscoverClient(cfg) as client:
            for i, (pid, name) in enumerate(stale, 1):
                proj = client.resolve_project(pid, name or "")
                if proj:
                    resolved[pid] = proj
                else:
                    missed += 1
                if i % 25 == 0:
                    console.print(f"  ... {i}/{len(stale)}　已解析 {len(resolved)}，"
                                  f"未命中 {missed}")
                client._sleep()
    except Exception as exc:
        console.print(f"[red]中断：{exc}[/red]　已解析的仍会入库")

    if not resolved:
        console.print("[yellow]一个都没解析出来[/yellow]")
        con.close()
        return 0

    rows = [to_row(normalize(p, snapshot_date=day, source="discover", run_id=run_id))
            for p in resolved.values()]
    existing = {r[0] for r in con.execute(
        "SELECT project_id FROM snapshots WHERE snapshot_date = ? AND source = 'discover'",
        [day]).fetchall()}
    fresh = [r for r in rows if r[COL_PID] not in existing]
    n = db.insert_rows(con, fresh)
    db.log_ingest(con, run_id, "discover", day, n, started, _now(), "resolve stale")

    states = {}
    for p in resolved.values():
        st = p.get("state")
        states[st] = states.get(st, 0) + 1
    console.print(f"[green]✓ 解析 {len(resolved)} 个，入库 {n} 条，未命中 {missed}[/green]")
    console.print("  " + "　".join(f"{i18n.state(k)} {v}" for k, v in sorted(states.items())))

    left = con.execute("""
        SELECT count(*) FROM latest WHERE state = 'live' AND deadline < current_timestamp
    """).fetchone()[0]
    console.print(f"  仍有 {left} 个状态过期")
    con.close()
    return 0


def cmd_full(args) -> int:
    """全量扫一遍当前所有在筹项目，建立基线。

    按品类切片翻页。原因：单个检索条件最多只能翻 200 页 / 2400 条（实测 page=201
    返回 404），而在筹总数超过 3000 —— 不切片必然抓不全。各品类在筹数合计与平台
    总数一致且都远低于 2400，所以品类是能拿到 100% 覆盖的切分维度。
    """
    cfg = Config.load(args.config)
    if args.delay:
        cfg.request_delay = (args.delay, args.delay * 1.8)

    day = date.fromisoformat(args.date) if args.date else date.today()
    con = db.connect(args.db)
    run_id = f"full-{day}-{uuid.uuid4().hex[:8]}"
    started = _now()

    collected: dict[int, dict] = {}
    expected = 0
    failed: list[str] = []
    note = ""

    def on_category(cid, name, hits, warn):
        nonlocal expected
        if hits is not None:
            expected += hits
        if warn:
            console.print(f"  [yellow]{name}: {warn}[/yellow]")
            failed.append(f"{name}: {warn}")
        else:
            console.print(f"  [cyan]{name}[/cyan] {hits} 个", highlight=False)

    try:
        with disc.DiscoverClient(cfg) as client:
            total_hits = client.total_hits(disc.Query(sort=args.sort, state=args.state))
            console.print(f"全量抓取 state={args.state}　平台报告 "
                          f"[bold]{total_hits:,}[/bold] 个，按 "
                          f"{len(disc.PARENT_CATEGORIES)} 个品类切片\n")
            for cid, name, proj in client.iter_all_by_category(
                    state=args.state, sort=args.sort, on_category=on_category):
                pid = proj.get("id")
                if pid:
                    collected[pid] = proj
    except KeyboardInterrupt:
        note = "用户中断"
        console.print("\n[yellow]已中断，保存已抓到的数据[/yellow]")
    except Exception as exc:
        note = f"中途失败: {type(exc).__name__}: {exc}"
        console.print(f"\n[red]抓取中断：{exc}[/red]")
        console.print("[yellow]已抓到的数据仍会入库[/yellow]")

    if not collected:
        console.print("[red]一条都没抓到[/red]")
        con.close()
        return 1

    rows = [to_row(normalize(p, snapshot_date=day, source="discover", run_id=run_id))
            for p in collected.values()]
    n = db.replace_snapshot(con, rows, snapshot_date=day, source="discover")
    cov = 100 * n / expected if expected else 0
    db.log_ingest(con, run_id, "discover", day, n, started, _now(),
                  f"full by-category coverage={cov:.1f}% {note}".strip())

    color = "green" if cov >= 98 and not note else "yellow"
    console.print(f"\n[{color}]✓ 入库 {n:,} 条，覆盖率 {cov:.1f}%"
                  f"（各品类合计 {expected:,}）[/{color}]")
    if failed:
        console.print(f"[yellow]有 {len(failed)} 个品类有问题：{failed}[/yellow]")

    if not args.no_report:
        console.print()
        report.daily_report(con, day, keywords=cfg.watch_keywords)
    con.close()
    return 0


def cmd_daily(args) -> int:
    """跑一轮日更抓取。"""
    cfg = Config.load(args.config)
    if args.headless:
        cfg.headless = True           # 只为调试；实测 headless 过不了 Cloudflare
    if args.max_pages:
        cfg.max_pages_per_sort = args.max_pages

    day = date.fromisoformat(args.date) if args.date else date.today()
    con = db.connect(args.db)
    if db.has_snapshot(con, day, "discover") and not args.force:
        console.print(f"[yellow]{day} 已抓过，加 --force 重抓[/yellow]")
        con.close()
        return 0

    run_id = f"disc-{day}-{uuid.uuid4().hex[:8]}"
    started = _now()
    queries = disc.build_queries(cfg)
    collected: dict[int, dict] = {}

    with disc.DiscoverClient(cfg) as client:
        for q in queries:
            try:
                hits = client.total_hits(q)
                console.print(f"[cyan]{q.describe()}[/cyan]  total_hits={hits}")
                n0 = len(collected)
                for proj in client.iter_projects(q, max_pages=cfg.max_pages_per_sort):
                    pid = proj.get("id")
                    if pid:
                        collected[pid] = proj      # 跨维度天然去重
                console.print(f"  新增 {len(collected) - n0}，累计 {len(collected)}")
            except Exception as exc:               # 单个维度失败不该拖垮整轮
                console.print(f"  [red]失败：{type(exc).__name__}: {exc}[/red]")

    rows = [to_row(normalize(p, snapshot_date=day, source="discover", run_id=run_id))
            for p in collected.values()]
    n = db.replace_snapshot(con, rows, snapshot_date=day, source="discover")
    db.log_ingest(con, run_id, "discover", day, n, started, _now(),
                  f"queries={len(queries)}")
    console.print(f"[green]✓ {day} 入库 {n:,} 条[/green]")

    if not args.no_report:
        console.print()
        report.daily_report(con, day, keywords=cfg.watch_keywords)
    con.close()
    return 0


# --------------------------------------------------------------------- rank

def cmd_rank(args) -> int:
    """抓取榜单前 N 名，名次入库。"""
    cfg = Config.load(args.config)
    if args.delay:
        cfg.request_delay = (args.delay, args.delay * 1.8)

    sort = args.sort
    if sort not in disc.RANK_SORTS:
        console.print(f"[red]未知榜单 {sort}[/red]，可选：{', '.join(disc.RANK_SORTS)}")
        return 1

    day = date.fromisoformat(args.date) if args.date else date.today()
    con = db.connect(args.db)
    run_id = f"rank-{sort}-{day}-{uuid.uuid4().hex[:8]}"
    started = _now()
    q = disc.Query(sort=sort, state=args.state)

    console.print(f"抓取 [bold]{disc.RANK_SORTS[sort]}[/bold]（{sort}）前 "
                  f"[bold]{args.top}[/bold] 名，范围 state={args.state}")

    ranked: list[tuple[int, dict]] = []
    with disc.DiscoverClient(cfg) as client:
        console.print(f"  总量 total_hits={client.total_hits(q):,}")
        for rank, proj in client.iter_ranked(q, top_n=args.top):
            ranked.append((rank, proj))
            if rank % 120 == 0:
                console.print(f"  ... 已抓 {rank} 名")

    if not ranked:
        console.print("[red]一条都没抓到[/red]")
        con.close()
        return 1

    # 项目明细进 snapshots（与日更同源同构），名次进 rankings
    rows = [to_row(normalize(p, snapshot_date=day, source="discover", run_id=run_id))
            for _, p in ranked]
    seen, uniq = set(), []
    for row in rows:
        pid = row[COL_PID]
        if pid not in seen:
            seen.add(pid)
            uniq.append(row)
    db.replace_snapshot(con, uniq, snapshot_date=day, source="discover")

    n = db.replace_rankings(
        con, [(rank, p["id"], run_id) for rank, p in ranked],
        snapshot_date=day, sort=sort, state=args.state)
    db.log_ingest(con, run_id, "discover", day, n, started, _now(),
                  f"rank sort={sort} top={args.top}")
    console.print(f"[green]✓ {sort} 榜单入库 {n} 名（项目明细 {len(uniq)} 条）[/green]")

    if not args.no_report:
        console.print()
        report.leaderboard(con, day, sort, limit=args.show)
    con.close()
    return 0


def cmd_board(args) -> int:
    """查看已入库的榜单。"""
    con = db.connect(args.db)
    report.leaderboard(con, date.fromisoformat(args.date) if args.date else None,
                       args.sort, limit=args.top, moves=not args.no_moves)
    con.close()
    return 0


# ------------------------------------------------------------------- report

def cmd_report(args) -> int:
    cfg = Config.load(args.config)
    con = db.connect(args.db, read_only=False)
    day = date.fromisoformat(args.date) if args.date else None
    report.daily_report(con, day, keywords=args.keyword or cfg.watch_keywords)
    con.close()
    return 0


def cmd_benchmark(args) -> int:
    con = db.connect(args.db)
    scope = args.category or args.parent or "全站"
    # 允许命令行直接传中文品类名
    args.parent = i18n.to_parent_en(args.parent)
    args.category = i18n.to_category_en(args.category)
    NUM, TXT = report.NUM, report.TXT

    rows = report.benchmark(con, category=args.category, parent=args.parent)
    console.print(report._table(
        f"同类目对标：{scope}（金额均为美元）",
        [("状态", TXT), ("样本", NUM), ("目标中位", NUM), ("募集中位", NUM),
         ("支持者", NUM), ("周期", NUM), ("客单价", NUM),
         ("视频%", NUM), ("推荐%", NUM)],
        [(i18n.state(r[0]), f"{r[1]:,}", report.money(r[2]), report.money(r[3]),
          report.money(r[4]), f"{r[5]:g}", report.money(r[6]),
          f"{r[7]:g}", f"{r[8]:g}") for r in rows]))

    rows = report.success_rate_by_goal(con, parent=args.parent)
    console.print(report._table(
        f"目标额区间成功率：{args.parent or '全站'}",
        [("目标额区间(USD)", TXT), ("样本数", NUM), ("成功率%", NUM), ("支持者中位", NUM)],
        [(r[0], f"{r[1]:,}", f"{r[2]:g}", report.money(r[3])) for r in rows]))

    _bias_note(con)
    con.close()
    return 0


def _bias_note(con) -> None:
    """数据源有选择性偏差，绝对成功率不可直接引用 —— 每次输出都提醒。"""
    rate = con.execute("""
        SELECT round(100.0 * sum(CASE WHEN state = 'successful' THEN 1 ELSE 0 END)
                     / count(*), 1)
        FROM latest WHERE state IN ('successful', 'failed')
    """).fetchone()[0]
    console.print(
        f"[yellow]⚠ 偏差提醒[/yellow]：本库整体成功率 [bold]{rate}%[/bold]，"
        "而 Kickstarter 官方口径约 41%。\n"
        "  Web Robots 爬的是 discover 列表页，过采样热门/成功项目，长尾失败项目收不全。\n"
        "  [bold]绝对成功率偏高，不要直接引用[/bold]；用于横向比较（哪个档位/品类更优）仍然有效。"
    )


def cmd_sql(args) -> int:
    con = db.connect(args.db)
    query = args.query
    if query.endswith(".sql"):
        query = open(query, encoding="utf-8").read()
    try:
        con.sql(query).show(max_rows=args.limit)
    finally:
        con.close()
    return 0


def cmd_serve(args) -> int:
    """启动本地 Web 服务。只读，不影响抓取任务。"""
    import uvicorn

    from . import web

    if args.db:
        web.db = web.Db(Path(args.db))
    url = f"http://{args.host}:{args.port}"
    console.print(f"[green]Web 服务已启动[/green] → [bold]{url}[/bold]")
    console.print(f"[dim]接口文档 {url}/api/docs　Ctrl+C 停止[/dim]")
    if args.open:
        import webbrowser
        webbrowser.open(url)
    uvicorn.run(web.app, host=args.host, port=args.port, log_level=args.log_level)
    return 0


def cmd_status(args) -> int:
    con = db.connect(args.db)
    console.print(report._table("快照概况", ["日期", "来源", "行数"], con.execute("""
        SELECT snapshot_date, source, count(*) FROM snapshots
        GROUP BY 1, 2 ORDER BY 1 DESC, 2 LIMIT 30
    """).fetchall()))
    total, projects = con.execute(
        "SELECT count(*), count(DISTINCT project_id) FROM snapshots").fetchone()
    console.print(f"总行数 [bold]{total:,}[/bold]　去重项目数 [bold]{projects:,}[/bold]")
    con.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="ks", description="Kickstarter 众筹数据管道")
    p.add_argument("--db", help="DuckDB 路径（默认 data/kickstarter.duckdb）")
    p.add_argument("--config", help="config.yaml 路径")
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("backfill", help="导入 Web Robots 月度全量 dump")
    b.add_argument("--count", type=int, default=1, help="导入最近 N 份 dump")
    b.add_argument("--since", help="导入该日期之后的全部 dump，如 2025-01-01")
    b.add_argument("--list", action="store_true", help="只列出可用 dump")
    b.add_argument("--limit", type=int, default=20)
    b.add_argument("--no-cache", action="store_true", help="边下边解析，不落盘")
    b.add_argument("--force", action="store_true")
    b.set_defaults(func=cmd_backfill)

    d = sub.add_parser("daily", help="日更抓取 discover 接口")
    d.add_argument("--date", help="指定快照日期（默认今天）")
    d.add_argument("--max-pages", type=int, help="每个检索维度最多翻几页")
    d.add_argument("--headless", action="store_true", help="仅调试用；实测过不了 Cloudflare")
    d.add_argument("--force", action="store_true")
    d.add_argument("--no-report", action="store_true")
    d.set_defaults(func=cmd_daily)

    rk = sub.add_parser("rank", help="抓取榜单前 N 名")
    rk.add_argument("--sort", default="most_funded",
                    help="榜单维度：" + " / ".join(disc.RANK_SORTS))
    rk.add_argument("--top", type=int, default=1000, help="抓前几名（默认 1000）")
    rk.add_argument("--state", default="live", help="榜单范围，默认 live 在筹中")
    rk.add_argument("--date")
    rk.add_argument("--delay", type=float, help="页间基础间隔秒数")
    rk.add_argument("--show", type=int, default=30, help="抓完显示前几名")
    rk.add_argument("--no-report", action="store_true")
    rk.set_defaults(func=cmd_rank)

    bd = sub.add_parser("board", help="查看已入库的榜单")
    bd.add_argument("--sort", default="most_funded")
    bd.add_argument("--top", type=int, default=30)
    bd.add_argument("--date")
    bd.add_argument("--no-moves", action="store_true", help="不显示名次变动")
    bd.set_defaults(func=cmd_board)

    fl = sub.add_parser("full", help="全量扫当前所有在筹项目（建基线）")
    fl.add_argument("--sort", default="newest",
                    help="用哪个排序翻页，默认 newest（排序最稳定）")
    fl.add_argument("--state", default="live")
    fl.add_argument("--date")
    fl.add_argument("--delay", type=float, help="页间基础间隔秒数")
    fl.add_argument("--no-report", action="store_true")
    fl.set_defaults(func=cmd_full)

    rs = sub.add_parser("resolve", help="定点解析状态过期的项目（outcomes 的兜底）")
    rs.add_argument("--limit", type=int, default=400, help="本次最多解析几个")
    rs.add_argument("--date")
    rs.add_argument("--delay", type=float)
    rs.set_defaults(func=cmd_resolve)

    oc = sub.add_parser("outcomes", help="补最近结束项目的成败结局")
    oc.add_argument("--since", help="起点日期，默认取库里最后一次 discover 快照")
    oc.add_argument("--date")
    oc.add_argument("--delay", type=float)
    oc.set_defaults(func=cmd_outcomes)

    nw = sub.add_parser("new", help="增量抓取：只拉新上线项目")
    nw.add_argument("--since", help="起点日期，默认取库里最后一次 discover 快照")
    nw.add_argument("--date", help="写入哪个快照日期，默认今天")
    nw.add_argument("--state", default="live")
    nw.add_argument("--max-pages", type=int, help="翻页上限，默认 200")
    nw.add_argument("--no-report", action="store_true")
    nw.set_defaults(func=cmd_new)

    r = sub.add_parser("report", help="输出日报")
    r.add_argument("--date")
    r.add_argument("--keyword", action="append")
    r.set_defaults(func=cmd_report)

    bm = sub.add_parser("benchmark", help="同类目对标（发起众筹定档用）")
    bm.add_argument("--parent", help="大类，中英文均可，如 科技 / Technology")
    bm.add_argument("--category", help="子类，中英文均可，如 桌游 / Tabletop Games")
    bm.set_defaults(func=cmd_benchmark)

    s = sub.add_parser("sql", help="直接跑 SQL 或 .sql 文件")
    s.add_argument("query")
    s.add_argument("--limit", type=int, default=40)
    s.set_defaults(func=cmd_sql)

    sv = sub.add_parser("serve", help="启动本地 Web 查询服务")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8000)
    sv.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    sv.add_argument("--log-level", default="warning")
    sv.set_defaults(func=cmd_serve)

    st = sub.add_parser("status", help="查看入库情况")
    st.set_defaults(func=cmd_status)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
