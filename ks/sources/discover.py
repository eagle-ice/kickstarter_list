"""站内 discover 接口日更抓取 —— 需要真实浏览器过 Cloudflare。

实测结论（2026-09，务必先读）：
  * kickstarter.com 全站在 Cloudflare managed challenge 后面，连 robots.txt 都 403。
  * requests / httpx / curl 即使补全 UA + sec-ch-ua + sec-fetch-* 也是 403。
  * Playwright **headless 一律 403**（headless-shell 和 --headless=new 都挂）。
  * Playwright **headful 才能过**（bundled chromium 与 channel="chrome" 均可，200）。
    => 服务器上跑必须配虚拟显示（Linux 用 Xvfb），macOS 需有已登录的图形会话。

过了挑战之后，在页面上下文里 fetch `?format=json`，拿到的 project 对象
和 Web Robots dump 完全同构，所以两条管道共用 ks.normalize。

分页要点：首页响应里的 `seed` 必须透传给后续页，否则结果集会在翻页途中重排、
导致漏抓和重抓。
"""
from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterator, Sequence
from urllib.parse import urlencode

from ..config import DATA_DIR, Config

BASE = "https://www.kickstarter.com/discover/advanced"

# 可用的榜单维度（实测均支持翻到 150 页以上）
RANK_SORTS = {
    "most_funded":  "募集金额榜",
    "most_backed":  "支持者数榜",
    "popularity":   "人气榜",
    "magic":        "平台推荐榜",
    "newest":       "最新上线",
    "end_date":     "临近结束",
}
WARMUP_URL = f"{BASE}?state=live&sort=newest"
PAGE_SIZE = 12   # KS 固定每页 12 条
MAX_PAGE = 200   # 实测硬上限：page=200 返回 200，page=201 返回 404
                 # => 单个检索条件最多只能拿到 2400 个项目

# 顶级品类 id。实测各品类在筹数合计与平台 total_hits 完全一致（3101 vs 3101），
# 且最大的 Games 只有 743 个，全都远低于 2400 上限
# —— 所以按品类切片是绕过分页上限、拿到 100% 覆盖的正确做法。
PARENT_CATEGORIES = {
    1:  "Art",        3:  "Comics",       26: "Crafts",     6:  "Dance",
    7:  "Design",     9:  "Fashion",      11: "Film & Video", 10: "Food",
    12: "Games",      13: "Journalism",   14: "Music",      15: "Photography",
    18: "Publishing", 16: "Technology",   17: "Theater",
}

_FETCH_JS = """async (u) => {
    const r = await fetch(u, {headers: {'Accept': 'application/json'}, credentials: 'include'});
    return {status: r.status, body: await r.text()};
}"""


@dataclass
class Query:
    """一次检索维度。sort / category_id / term 可自由组合。"""
    sort: str = "newest"
    state: str = "live"
    category_id: int | None = None
    term: str | None = None
    label: str = ""

    def url(self, page: int, seed: int | None = None) -> str:
        params: dict[str, object] = {"state": self.state, "sort": self.sort,
                                     "format": "json", "page": page}
        if self.category_id is not None:
            params["category_id"] = self.category_id
        if self.term:
            params["term"] = self.term
        if seed is not None:
            params["seed"] = seed
        return f"{BASE}?{urlencode(params)}"

    def describe(self) -> str:
        if self.label:
            return self.label
        bits = [f"sort={self.sort}", f"state={self.state}"]
        if self.category_id is not None:
            bits.append(f"cat={self.category_id}")
        if self.term:
            bits.append(f"term={self.term!r}")
        return " ".join(bits)


class DiscoverClient:
    """持有一个过了 Cloudflare 的浏览器会话，按需翻页。

    用 persistent context 把 profile 落盘，后续运行能复用已建立的会话，
    减少重复触发挑战。
    """

    def __init__(self, cfg: Config | None = None, *, profile_dir: Path | None = None):
        self.cfg = cfg or Config.load()
        self.profile_dir = Path(profile_dir or DATA_DIR / "browser-profile")
        self._pw = None
        self._ctx = None
        self._page = None

    def __enter__(self) -> "DiscoverClient":
        from playwright.sync_api import sync_playwright

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._pw = sync_playwright().start()
        self._ctx = self._pw.chromium.launch_persistent_context(
            user_data_dir=str(self.profile_dir),
            headless=self.cfg.headless,  # 默认 False —— 见模块 docstring
            args=["--disable-blink-features=AutomationControlled"],
            locale="en-US",
            timezone_id="America/New_York",
            viewport={"width": 1440, "height": 900},
        )
        self._ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
        )
        self._page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        self._warmup()
        return self

    def __exit__(self, *exc) -> None:
        try:
            if self._ctx:
                self._ctx.close()
        finally:
            if self._pw:
                self._pw.stop()

    def _warmup(self) -> None:
        """先正常访问一次页面，让 Cloudflare 挑战自行通过。"""
        self._page.goto(WARMUP_URL, wait_until="domcontentloaded",
                        timeout=self.cfg.nav_timeout_ms)
        for _ in range(25):
            if "Just a moment" not in self._page.title():
                return
            time.sleep(1.5)
        raise RuntimeError(
            "没能通过 Cloudflare 挑战。确认 headless=false 且当前有可用图形会话"
            "（Linux 服务器需 Xvfb）。"
        )

    def _sleep(self) -> None:
        lo, hi = self.cfg.request_delay
        time.sleep(random.uniform(lo, hi))

    def fetch_page(self, query: Query, page: int, seed: int | None = None) -> dict:
        """取一页。404 表示翻过了分页上限，返回空页而不是抛异常
        —— 否则深翻时整轮抓取会崩掉，已抓的数据全部丢失。"""
        res = self._page.evaluate(_FETCH_JS, query.url(page, seed))
        if res["status"] == 404:
            return {"projects": [], "has_more": False, "total_hits": 0}
        if res["status"] != 200:
            raise RuntimeError(f"discover 返回 {res['status']}（{query.describe()} page={page}）")
        return json.loads(res["body"])

    def iter_projects(self, query: Query, *, max_pages: int | None = None) -> Iterator[dict]:
        """翻页拉取。遇到 has_more=false / 空页 / 触顶就停。"""
        max_pages = min(max_pages or self.cfg.max_pages_per_sort, MAX_PAGE)
        seed: int | None = None
        seen: set[int] = set()

        for page in range(1, max_pages + 1):
            if page > 1:
                self._sleep()
            payload = self.fetch_page(query, page, seed)
            seed = seed or payload.get("seed")  # 首页拿到 seed 后固定住
            projects = payload.get("projects") or []
            if not projects:
                break

            new = 0
            for proj in projects:
                pid = proj.get("id")
                if pid and pid not in seen:
                    seen.add(pid)
                    new += 1
                    yield proj
            # 翻到重复页说明已经触到分页上限，再翻也是浪费
            if new == 0:
                break
            if not payload.get("has_more"):
                break

    def iter_since(self, query: Query, since: datetime, *, max_pages: int = MAX_PAGE,
                   grace_pages: int = 2, field: str = "launched_at"):
        """只拉 field >= since 的项目，翻到更早的就停。

        两种用法，都依赖对应排序是倒序的（均已实测）：
          * field="launched_at" + sort=newest   -> 新上线项目（120 条零违例）
          * field="deadline"    + sort=end_date + state=successful/failed
                                                -> 最近结束的项目（最近结束在前）

        grace_pages：遇到第一页全是过期项目后再多翻几页才真停，防止边界抖动
        （KS 偶尔在翻页边界重复或轻微乱序）导致漏抓。
        """
        seed: int | None = None
        seen: set[int] = set()
        stale_pages = 0
        cutoff = int(since.timestamp())

        for page in range(1, min(max_pages, MAX_PAGE) + 1):
            if page > 1:
                self._sleep()
            payload = self.fetch_page(query, page, seed)
            seed = seed or payload.get("seed")
            projects = payload.get("projects") or []
            if not projects:
                break

            page_has_fresh = False
            for proj in projects:
                pid = proj.get("id")
                ts = proj.get(field)
                if not pid or pid in seen:
                    continue
                seen.add(pid)
                if ts is not None and ts < cutoff:
                    continue          # 过期项目跳过，但继续看本页剩余的
                page_has_fresh = True
                yield proj

            if page_has_fresh:
                stale_pages = 0
            else:
                stale_pages += 1
                if stale_pages > grace_pages:
                    break
            if not payload.get("has_more"):
                break

    def iter_ranked(self, query: Query, *, top_n: int):
        """按榜单顺序产出 (名次, project)，名次从 1 起。

        名次 = 结果流里的位置，所以**必须严格保序**：不能用 set 打乱，
        重复 id 直接跳过而不占名次（KS 偶尔会在翻页边界重复返回同一项目）。
        """
        seed: int | None = None
        seen: set[int] = set()
        rank = 0
        page = 0
        max_pages = min(-(-top_n // PAGE_SIZE) + 5, MAX_PAGE)  # 余量抵消重复，但不超上限

        while rank < top_n and page < max_pages:
            page += 1
            if page > 1:
                self._sleep()
            payload = self.fetch_page(query, page, seed)
            seed = seed or payload.get("seed")
            projects = payload.get("projects") or []
            if not projects:
                break

            fresh = 0
            for proj in projects:
                pid = proj.get("id")
                if not pid or pid in seen:
                    continue
                seen.add(pid)
                fresh += 1
                rank += 1
                yield rank, proj
                if rank >= top_n:
                    return
            if fresh == 0:          # 整页都是重复 -> 触到分页上限
                break
            if not payload.get("has_more"):
                break

    # 实测有效的终态只有这三个。`suspended` **不是**有效筛选 —— 它和瞎编的
    # state 值表现完全一致（都返回 684254 条且实际 state 全是 live），
    # 说明 KS 直接忽略了它，不要加进来。
    TERMINAL_STATES = ("successful", "failed", "canceled")

    def iter_recently_ended(self, since: datetime, *, states=TERMINAL_STATES,
                            max_pages: int = MAX_PAGE, on_state=None):
        """拉最近结束的项目，用来补上"从在筹集合里消失"的那批的最终结果。

        只抓 state=live 的话，项目一结束就从结果里消失，我们永远观测不到它
        变成 successful / failed / canceled —— 库里会一直留着过期的 live 记录。
        实测 3 天内有 223 个项目这样消失。

        200 页上限下可回溯 68~87 天，日常增量足够；更早的补不回来。
        """
        for st in states:
            q = Query(sort="end_date", state=st)
            got = 0
            for proj in self.iter_since(q, since, max_pages=max_pages,
                                        field="deadline"):
                got += 1
                yield st, proj
            if on_state:
                on_state(st, got)
            self._sleep()

    def resolve_project(self, project_id: int, name: str) -> dict | None:
        """按名称精确搜索定位单个项目，返回它当前的完整对象。

        为什么需要这个：`discover?state=successful&sort=end_date` 的列表**并不完整**
        —— 实测有项目确实是 successful、截止日落在列表覆盖的页码范围内，却不出现在
        列表里。但用 term 搜索能稳定找到。所以已知 id 的项目要用这个兜底。
        """
        q = Query(sort="newest", state="", term=name[:60])
        try:
            payload = self.fetch_page(q, 1)
        except Exception:
            return None
        for proj in payload.get("projects") or []:
            if proj.get("id") == project_id:
                return proj
        return None

    def iter_all_by_category(self, *, state: str = "live", sort: str = "newest",
                             on_category=None):
        """按品类逐个翻页，绕过 200 页 / 2400 条的分页上限。

        单个检索条件最多只能翻到 2400 个项目，而在筹总数超过 3000，
        所以整体扫一遍必须切片。品类是天然的切分维度：实测各品类在筹数
        合计与平台总数完全一致，且没有任何品类接近 2400。
        """
        for cid, name in PARENT_CATEGORIES.items():
            q = Query(sort=sort, state=state, category_id=cid, label=f"category:{cid}")
            try:
                hits = self.total_hits(q)
            except Exception as exc:
                if on_category:
                    on_category(cid, name, None, f"total_hits 失败: {exc}")
                continue
            if hits > PAGE_SIZE * MAX_PAGE and on_category:
                on_category(cid, name, hits,
                            f"超出分页上限 {PAGE_SIZE * MAX_PAGE}，这个品类会抓不全")
            elif on_category:
                on_category(cid, name, hits, None)

            pages = min(-(-hits // PAGE_SIZE) + 3, MAX_PAGE)
            got = 0
            for proj in self.iter_projects(q, max_pages=pages):
                got += 1
                yield cid, name, proj
            self._sleep()

    def total_hits(self, query: Query) -> int:
        return int(self.fetch_page(query, 1).get("total_hits") or 0)


def build_queries(cfg: Config) -> list[Query]:
    """把 config 展开成要跑的检索维度列表。"""
    queries = [Query(sort=s, state="live") for s in cfg.sorts]
    for cat in cfg.watch_categories:
        queries.append(Query(sort="newest", state="live", category_id=int(cat),
                             label=f"category:{cat}"))
    for term in cfg.watch_keywords:
        queries.append(Query(sort="newest", state="live", term=term, label=f"term:{term}"))
    return queries
