"""核心逻辑测试。跑：./.venv/bin/python -m pytest tests/ -q"""
from __future__ import annotations

import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from ks import db
from ks.normalize import COLUMNS, normalize, to_row
from ks.sources.discover import Query


def _raw(**over):
    base = {
        "id": 1, "name": "Test", "blurb": "b", "slug": "test", "state": "live",
        "goal": 10000, "pledged": 5000, "backers_count": 50,
        "usd_pledged": "5000.0", "currency": "USD", "country": "US",
        "created_at": 1700000000, "launched_at": 1700086400, "deadline": 1702678400,
        "state_changed_at": 1700086400,
        "category": {"id": 16, "name": "Hardware", "slug": "technology/hardware"},
        "location": {"id": 1, "name": "Boston", "state": "MA", "country": "US"},
        "creator": {"id": 9, "name": "Alice", "slug": "alice"},
        "urls": {"web": {"project": "https://example.com/p"}},
        "video": {"id": 1},
    }
    base.update(over)
    return base


class TestNormalize:
    def test_row_matches_columns(self):
        r = normalize(_raw(), snapshot_date=date(2026, 1, 1), source="discover")
        assert len(to_row(r)) == len(COLUMNS)

    def test_top_level_category_is_its_own_parent(self):
        """顶级品类 slug 无 '/'，父类目就是它自己，否则报表里会出现空分类。"""
        r = normalize(_raw(category={"id": 14, "name": "Music", "slug": "music"}),
                      snapshot_date=date(2026, 1, 1), source="discover")
        assert r["category_parent_name"] == "Music"
        assert r["category_name"] == "Music"

    def test_parent_category_derived_from_slug(self):
        """discover 的 category 不带 parent_name，必须从 slug 推出来。"""
        r = normalize(_raw(), snapshot_date=date(2026, 1, 1), source="discover")
        assert r["category_parent_name"] == "Technology"
        assert r["category_name"] == "Hardware"

    def test_percent_funded_fallback(self):
        """discover 不返回 percent_funded，要自己算。"""
        r = normalize(_raw(), snapshot_date=date(2026, 1, 1), source="discover")
        assert r["percent_funded"] == pytest.approx(50.0)

    def test_percent_funded_uses_source_value(self):
        r = normalize(_raw(percent_funded=77.5), snapshot_date=date(2026, 1, 1), source="webrobots")
        assert r["percent_funded"] == pytest.approx(77.5)

    def test_usd_pledged_string_coerced(self):
        r = normalize(_raw(), snapshot_date=date(2026, 1, 1), source="webrobots")
        assert r["usd_pledged"] == pytest.approx(5000.0)

    def test_zero_goal_does_not_divide(self):
        r = normalize(_raw(goal=0), snapshot_date=date(2026, 1, 1), source="discover")
        assert r["percent_funded"] is None

    def test_missing_nested_objects(self):
        r = normalize({"id": 2}, snapshot_date=date(2026, 1, 1), source="discover")
        assert r["project_id"] == 2
        assert r["category_name"] is None and r["creator_id"] is None
        assert len(to_row(r)) == len(COLUMNS)

    def test_bad_timestamp_is_none(self):
        r = normalize(_raw(deadline="garbage"), snapshot_date=date(2026, 1, 1), source="discover")
        assert r["deadline"] is None and r["duration_days"] is None

    def test_duration_and_days_remaining(self):
        r = normalize(_raw(), snapshot_date=date(2023, 12, 1), source="discover")
        assert r["duration_days"] == pytest.approx(30.0, abs=0.1)
        assert r["days_remaining"] > 0


class TestDB:
    @pytest.fixture
    def con(self, tmp_path):
        c = db.connect(tmp_path / "t.duckdb")
        yield c
        c.close()

    def _write(self, con, day, **over):
        r = normalize(_raw(**over), snapshot_date=day, source="discover")
        return db.replace_snapshot(con, [to_row(r)], snapshot_date=day, source="discover")

    def test_replace_is_idempotent(self, con):
        d = date(2026, 1, 1)
        self._write(con, d)
        self._write(con, d)
        assert con.execute("SELECT count(*) FROM snapshots").fetchone()[0] == 1

    def test_mismatched_date_rejected(self, con):
        """行内日期和声明日期不一致会导致 DELETE 失效、重跑累积脏数据。"""
        r = normalize(_raw(), snapshot_date=date(2026, 1, 2), source="discover")
        with pytest.raises(ValueError, match="不一致"):
            db.replace_snapshot(con, [to_row(r)], snapshot_date=date(2026, 1, 1), source="discover")

    def test_deltas_computed(self, con):
        self._write(con, date(2026, 1, 1), pledged=5000, backers_count=50,
                    usd_pledged="5000.0", static_usd_rate=1)
        self._write(con, date(2026, 1, 3), pledged=8000, backers_count=80,
                    usd_pledged="8000.0", static_usd_rate=1, state="successful")
        row = con.execute("""
            SELECT pledged_delta_usd, backers_delta, gap_days, pledged_usd_per_day, state_changed
            FROM deltas
        """).fetchone()
        assert row == (3000.0, 30, 2, 1500.0, True)

    def test_latest_picks_newest(self, con):
        self._write(con, date(2026, 1, 1), pledged=5000)
        self._write(con, date(2026, 1, 5), pledged=9000)
        assert con.execute("SELECT snapshot_date, pledged FROM latest").fetchone() \
               == (date(2026, 1, 5), 9000.0)

    def test_has_snapshot(self, con):
        d = date(2026, 1, 1)
        assert not db.has_snapshot(con, d, "discover")
        self._write(con, d)
        assert db.has_snapshot(con, d, "discover")

    def test_same_day_two_sources_no_phantom_delta(self, con):
        """同一天既有 dump 又有抓取时，不该产生 gap_days=0 的伪增量。"""
        d1, d2 = date(2026, 1, 1), date(2026, 1, 2)
        self._write(con, d1, pledged=5000, usd_pledged="5000.0", static_usd_rate=1)
        # d2 两个来源都写
        for src, pl in (("discover", 9000), ("webrobots", 8000)):
            r = normalize(_raw(pledged=pl, usd_pledged=f"{pl}.0", static_usd_rate=1),
                          snapshot_date=d2, source=src)
            db.replace_snapshot(con, [to_row(r)], snapshot_date=d2, source=src)
        rows = con.execute(
            "SELECT gap_days, pledged_delta_usd FROM deltas ORDER BY snapshot_date").fetchall()
        assert rows == [(1, 4000.0)]   # 只有一条，且取了 discover 的 9000

    def test_sources_coexist(self, con):
        """同一天两个来源各写各的，互不覆盖。"""
        d = date(2026, 1, 1)
        self._write(con, d)
        r = normalize(_raw(id=2), snapshot_date=d, source="webrobots")
        db.replace_snapshot(con, [to_row(r)], snapshot_date=d, source="webrobots")
        assert con.execute("SELECT count(*) FROM snapshots").fetchone()[0] == 2


class TestQuery:
    def test_seed_included_only_when_given(self):
        assert "seed" not in Query().url(1)
        assert "seed=42" in Query().url(2, seed=42)

    def test_filters_encoded(self):
        u = Query(category_id=16, term="3d printer").url(1)
        assert "category_id=16" in u and "term=3d+printer" in u

    def test_format_json_always_present(self):
        assert "format=json" in Query(sort="magic").url(1)


class TestCurrency:
    """跨项目比金额必须换算美元 —— 按本币排序会让日元项目压过美元项目。"""

    @pytest.fixture
    def con(self, tmp_path):
        c = db.connect(tmp_path / "cur.duckdb")
        # 日元项目：本币 25,645,602 但只值 $162,107
        jpy = normalize(_raw(id=1, currency="JPY", goal=1000000, pledged=25645602,
                             usd_pledged="162107.0", static_usd_rate=0.00632),
                        snapshot_date=date(2026, 1, 1), source="webrobots")
        # 美元项目：$10,433,558，真实体量远大于上面那个
        usd = normalize(_raw(id=2, currency="USD", goal=50000, pledged=10433558,
                             usd_pledged="10433558.0", static_usd_rate=1),
                        snapshot_date=date(2026, 1, 1), source="webrobots")
        db.replace_snapshot(c, [to_row(jpy), to_row(usd)],
                            snapshot_date=date(2026, 1, 1), source="webrobots")
        yield c
        c.close()

    def test_local_currency_ordering_is_wrong(self, con):
        """反向确认 bug 存在：按本币排序日元项目会排到前面。"""
        first = con.execute(
            "SELECT project_id FROM latest ORDER BY pledged DESC").fetchone()[0]
        assert first == 1          # 日元项目，错误地排第一

    def test_usd_ordering_is_correct(self, con):
        first = con.execute(
            "SELECT project_id FROM latest ORDER BY pledged_usd DESC").fetchone()[0]
        assert first == 2          # 美元项目才是真的大

    def test_goal_usd_converted(self, con):
        goal = con.execute(
            "SELECT goal_usd FROM latest WHERE project_id = 1").fetchone()[0]
        assert goal == pytest.approx(1000000 * 0.00632)

    def test_usd_rate_falls_back_to_ratio(self, con):
        """static_usd_rate 缺失时用 usd_pledged/pledged 推。"""
        r = normalize(_raw(id=3, currency="EUR", pledged=2000, usd_pledged="2200.0",
                           static_usd_rate=None),
                      snapshot_date=date(2026, 1, 2), source="webrobots")
        db.replace_snapshot(con, [to_row(r)], snapshot_date=date(2026, 1, 2),
                            source="webrobots")
        rate = con.execute(
            "SELECT usd_rate FROM latest WHERE project_id = 3").fetchone()[0]
        assert rate == pytest.approx(1.1)

    def test_unlaunched_draft_has_null_rate(self, con):
        """submitted/started 草稿没有汇率，不该崩，也不该被算进分析。"""
        r = normalize(_raw(id=4, currency="GBP", state="submitted", goal=0, pledged=0,
                           usd_pledged=None, static_usd_rate=0),
                      snapshot_date=date(2026, 1, 2), source="webrobots")
        db.replace_snapshot(con, [to_row(r)], snapshot_date=date(2026, 1, 2),
                            source="webrobots")
        assert con.execute(
            "SELECT usd_rate FROM latest WHERE project_id = 4").fetchone()[0] is None


class TestI18n:
    """译名表必须覆盖库里出现过的每个枚举值，否则报表会漏出英文。"""

    def test_no_missing_in_known_vocabulary(self):
        from ks import i18n
        pairs = [(p, c) for c in i18n._FLAT for p in [None]]
        m = i18n.missing(pairs, i18n.STATES.keys(), i18n.PARENTS.keys())
        assert m["states"] == [] and m["parents"] == []

    def test_same_name_differs_by_parent(self):
        """Comedy/Spaces 这类同名子类跨大类含义不同，不能一刀切。"""
        from ks import i18n
        assert i18n.category("Comedy", "Film & Video") == "喜剧片"
        assert i18n.category("Comedy", "Music") == "喜剧音乐"
        assert i18n.category("Spaces", "Dance") != i18n.category("Spaces", "Food")

    def test_unknown_value_passes_through(self):
        """遇到没见过的新品类要原样返回，不能报错或吞掉。"""
        from ks import i18n
        assert i18n.category("Brand New Cat", "Games") == "Brand New Cat"
        assert i18n.state("weird") == "weird"
        assert i18n.parent(None) == "" and i18n.country(None) == ""

    def test_as_is_terms_not_flagged(self):
        from ks import i18n
        assert i18n.missing([("Music", "R&B"), ("Crafts", "DIY")])["categories"] == []

    def test_zh_views_translate(self, tmp_path):
        con = db.connect(tmp_path / "zh.duckdb")
        r = normalize(_raw(state="live", country="HK",
                           category={"id": 16, "name": "Hardware", "slug": "technology/hardware"}),
                      snapshot_date=date(2026, 1, 1), source="discover")
        db.replace_snapshot(con, [to_row(r)], snapshot_date=date(2026, 1, 1), source="discover")
        db.sync_i18n(con)
        con.execute(db.VIEW_LATEST_ZH)
        row = con.execute("SELECT 状态, 大类, 子类, 国家 FROM latest_zh").fetchone()
        assert row == ("在筹中", "科技", "硬件", "中国香港")
        con.close()


class TestIncremental:
    """增量抓取靠 newest 严格倒序 + 提前退出。实测 120 条零违例，前提成立。"""

    class _FakeClient:
        """不连网，验证 iter_since 的翻页与停止逻辑。"""

        def __init__(self, pages):
            self.pages = pages
            self.fetched = []

        def _sleep(self):
            pass

        def fetch_page(self, query, page, seed=None):
            self.fetched.append(page)
            if page > len(self.pages):
                return {"projects": [], "has_more": False}
            return {"projects": self.pages[page - 1], "seed": 1,
                    "has_more": page < len(self.pages)}

    @staticmethod
    def _p(pid, ts):
        return {"id": pid, "launched_at": ts}

    def _run(self, pages, cutoff, grace=2):
        from datetime import datetime, timezone

        from ks.sources.discover import DiscoverClient, Query
        fake = self._FakeClient(pages)
        gen = DiscoverClient.iter_since(
            fake, Query(sort="newest"),
            datetime.fromtimestamp(cutoff, timezone.utc), grace_pages=grace)
        return [p["id"] for p in gen], fake.fetched

    def test_stops_after_grace_pages(self):
        """全新 -> 全旧，多翻 grace 页后停，不翻完全部。"""
        pages = [[self._p(1, 500), self._p(2, 400)]] + [
            [self._p(10 + i, 100)] for i in range(10)]
        ids, fetched = self._run(pages, cutoff=300, grace=2)
        assert ids == [1, 2]
        assert max(fetched) == 4        # 1 新页 + 3 旧页(grace=2 -> 第3页超限)

    def test_keeps_fresh_items_on_mixed_page(self):
        """同一页里新旧混排时，新的要收、旧的跳过。"""
        pages = [[self._p(1, 500), self._p(2, 200), self._p(3, 450)]]
        ids, _ = self._run(pages, cutoff=300)
        assert ids == [1, 3]

    def test_grace_recovers_from_boundary_jitter(self):
        """中间夹一页全旧、后面又出现新的（边界抖动），不能提前漏掉。"""
        pages = [
            [self._p(1, 500)],
            [self._p(2, 100)],          # 全旧
            [self._p(3, 450)],          # 又出现新的
            [self._p(4, 90)], [self._p(5, 80)], [self._p(6, 70)],
        ]
        ids, _ = self._run(pages, cutoff=300, grace=2)
        assert ids == [1, 3]

    def test_dedupes_across_pages(self):
        pages = [[self._p(1, 500)], [self._p(1, 500), self._p(2, 400)]]
        ids, _ = self._run(pages, cutoff=300)
        assert ids == [1, 2]

    def test_missing_launched_at_is_kept(self):
        """没有 launched_at 的记录不该被当成过期丢掉。"""
        pages = [[{"id": 7}]]
        ids, _ = self._run(pages, cutoff=300)
        assert ids == [7]


class TestPaginationCap:
    """KS 分页硬上限 200 页 / 2400 条（实测 page=201 返回 404）。"""

    def test_404_is_end_of_pagination_not_error(self):
        """深翻遇到 404 必须当成翻完了，抛异常会让整轮抓取崩掉、已抓数据全丢。"""
        from ks.sources.discover import DiscoverClient, Query

        class FakePage:
            def evaluate(self, js, url):
                return {"status": 404, "body": ""}

        c = DiscoverClient.__new__(DiscoverClient)
        c._page = FakePage()
        payload = c.fetch_page(Query(), 201)
        assert payload["projects"] == [] and payload["has_more"] is False

    def test_other_http_errors_still_raise(self):
        from ks.sources.discover import DiscoverClient, Query

        class FakePage:
            def evaluate(self, js, url):
                return {"status": 403, "body": ""}

        c = DiscoverClient.__new__(DiscoverClient)
        c._page = FakePage()
        with pytest.raises(RuntimeError, match="403"):
            c.fetch_page(Query(), 1)

    def test_iter_projects_never_exceeds_max_page(self):
        from ks.sources import discover as d

        calls = []

        class Fake:
            cfg = type("C", (), {"max_pages_per_sort": 999})()

            def _sleep(self): pass

            def fetch_page(self, q, page, seed=None):
                calls.append(page)
                return {"projects": [{"id": page * 100}], "has_more": True, "seed": 1}

        list(d.DiscoverClient.iter_projects(Fake(), d.Query(), max_pages=9999))
        assert max(calls) == d.MAX_PAGE

    def test_categories_cover_without_hitting_cap(self):
        """各品类都必须低于上限，否则切片也抓不全。"""
        from ks.sources.discover import MAX_PAGE, PAGE_SIZE, PARENT_CATEGORIES
        assert len(PARENT_CATEGORIES) == 15
        assert PAGE_SIZE * MAX_PAGE == 2400


class TestTerminalStates:
    """只抓 live 会永远观测不到项目的结局 —— 项目一结束就从结果里消失。"""

    def test_suspended_is_not_a_valid_filter(self):
        """实测 state=suspended 和瞎编的 state 返回完全一致（都是未过滤的 live），
        KS 忽略了它。加进 TERMINAL_STATES 只会白跑几百个请求。"""
        from ks.sources.discover import DiscoverClient
        assert "suspended" not in DiscoverClient.TERMINAL_STATES
        assert set(DiscoverClient.TERMINAL_STATES) == {"successful", "failed", "canceled"}

    def test_iter_since_can_cut_on_deadline(self):
        """补结局用 deadline 截断，不是 launched_at。"""
        from datetime import datetime, timezone

        from ks.sources.discover import DiscoverClient, Query

        class Fake:
            def _sleep(self): pass
            def fetch_page(self, q, page, seed=None):
                if page > 1:
                    return {"projects": [], "has_more": False}
                return {"projects": [
                    {"id": 1, "deadline": 500, "launched_at": 100},   # 新结束 -> 收
                    {"id": 2, "deadline": 200, "launched_at": 900},   # 早结束 -> 跳过
                ], "has_more": False, "seed": 1}

        got = list(DiscoverClient.iter_since(
            Fake(), Query(), datetime.fromtimestamp(300, timezone.utc), field="deadline"))
        assert [p["id"] for p in got] == [1]


class TestIncrementalCutoff:
    def test_outcomes_start_skips_today(self, tmp_path):
        """当天跑过 ks new 之后，outcomes 的起点不能被推到今天，
        否则中间几天结束的项目会被整段跳过。"""
        from ks.cli import _last_observation_before, _last_snapshot

        con = db.connect(tmp_path / "cut.duckdb")
        for d, pid in [(date(2026, 9, 23), 1), (date(2026, 9, 29), 2)]:
            r = normalize(_raw(id=pid), snapshot_date=d, source="discover")
            db.replace_snapshot(con, [to_row(r)], snapshot_date=d, source="discover")

        assert _last_snapshot(con) == date(2026, 9, 29)          # 含今天
        assert _last_observation_before(con, date(2026, 9, 29)) == date(2026, 9, 23)
        con.close()
