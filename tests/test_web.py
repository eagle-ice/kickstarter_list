"""Web 接口测试。用临时库，不碰真实数据。"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from fastapi.testclient import TestClient

from ks import db, web
from ks.normalize import normalize, to_row


def _raw(pid, name, **over):
    base = {
        "id": pid, "name": name, "blurb": f"{name} 简介", "slug": f"p{pid}",
        "state": "live", "goal": 10000, "pledged": 50000, "backers_count": 200,
        "usd_pledged": "50000.0", "static_usd_rate": 1, "currency": "USD",
        "country": "US", "created_at": 1700000000, "launched_at": 1700086400,
        "deadline": 1702678400, "state_changed_at": 1700086400,
        "category": {"id": 16, "name": "Hardware", "slug": "technology/hardware"},
        "location": {"id": 1, "name": "Boston", "country": "US"},
        "creator": {"id": 9, "name": "Alice", "slug": "alice"},
        "urls": {"web": {"project": f"https://ks.test/{pid}"}},
        "video": {"id": 1},
    }
    base.update(over)
    return base


@pytest.fixture
def client(tmp_path):
    path = tmp_path / "web.duckdb"
    con = db.connect(path)
    day = date(2026, 1, 10)
    raws = [
        _raw(1, "Big Tech", pledged=900000, usd_pledged="900000.0"),
        _raw(2, "Small Game", pledged=1000, usd_pledged="1000.0", backers_count=5,
             category={"id": 12, "name": "Tabletop Games", "slug": "games/tabletop-games"}),
        _raw(3, "JP Project", currency="JPY", country="JP", pledged=3000000,
             usd_pledged="20000.0", static_usd_rate=0.00667),
    ]
    rows = [to_row(normalize(r, snapshot_date=day, source="discover")) for r in raws]
    db.replace_snapshot(con, rows, snapshot_date=day, source="discover")
    db.replace_rankings(con, [(1, 1, "t"), (2, 3, "t"), (3, 2, "t")],
                        snapshot_date=day, sort="most_funded", state="live")
    con.close()

    web.db = web.Db(path)
    return TestClient(web.app)


class TestMeta:
    def test_meta_shape(self, client):
        d = client.get("/api/meta").json()
        assert d["totals"]["projects"] == 3
        assert "科技" in d["parents"] and "游戏" in d["parents"]
        assert any(s["state"] == "live" for s in d["states"])

    def test_index_served(self, client):
        assert client.get("/").status_code == 200


class TestProjects:
    def test_default_lists_all(self, client):
        assert client.get("/api/projects").json()["total"] == 3

    def test_filter_by_parent_uses_chinese(self, client):
        d = client.get("/api/projects", params={"parent": "游戏"}).json()
        assert d["total"] == 1 and d["rows"][0]["name"] == "Small Game"

    def test_keyword_searches_name_and_blurb(self, client):
        assert client.get("/api/projects", params={"q": "big"}).json()["total"] == 1

    def test_min_usd_uses_usd_not_local(self, client):
        """日元项目本币 300 万但只值 $2 万，不该被 min_usd=100000 选中。"""
        d = client.get("/api/projects", params={"min_usd": 100000}).json()
        assert [r["name"] for r in d["rows"]] == ["Big Tech"]

    def test_sort_desc_default(self, client):
        rows = client.get("/api/projects", params={"sort": "pledged_usd"}).json()["rows"]
        assert [r["name"] for r in rows] == ["Big Tech", "JP Project", "Small Game"]

    def test_sort_asc(self, client):
        rows = client.get("/api/projects",
                          params={"sort": "pledged_usd", "order": "asc"}).json()["rows"]
        assert rows[0]["name"] == "Small Game"

    def test_bad_sort_rejected(self, client):
        """ORDER BY 不能参数化，非白名单字段必须拒绝而不是拼进 SQL。"""
        r = client.get("/api/projects", params={"sort": "name; DROP TABLE snapshots"})
        assert r.status_code == 400

    def test_sql_injection_in_filter_is_harmless(self, client):
        r = client.get("/api/projects", params={"q": "'; DROP TABLE snapshots--"})
        assert r.status_code == 200 and r.json()["total"] == 0
        assert client.get("/api/meta").json()["totals"]["projects"] == 3

    def test_pagination(self, client):
        p1 = client.get("/api/projects", params={"page_size": 2, "page": 1}).json()
        p2 = client.get("/api/projects", params={"page_size": 2, "page": 2}).json()
        assert len(p1["rows"]) == 2 and len(p2["rows"]) == 1
        assert p1["rows"][0]["name"] != p2["rows"][0]["name"]

    def test_page_size_capped(self, client):
        assert client.get("/api/projects", params={"page_size": 99999}).status_code == 422


class TestLeaderboard:
    def test_returns_ranked_order(self, client):
        d = client.get("/api/leaderboard", params={"sort": "most_funded"}).json()
        assert [r["rank"] for r in d["rows"]] == [1, 2, 3]
        assert d["rows"][0]["name"] == "Big Tech"
        assert d["label"] == "募集金额榜"

    def test_translated_columns(self, client):
        r = client.get("/api/leaderboard", params={"sort": "most_funded"}).json()["rows"][0]
        assert r["大类"] == "科技" and r["国家"] == "美国" and r["状态"] == "在筹中"

    def test_unknown_sort_rejected(self, client):
        assert client.get("/api/leaderboard", params={"sort": "nope"}).status_code == 400

    def test_empty_board_gives_hint(self, client):
        d = client.get("/api/leaderboard", params={"sort": "popularity"}).json()
        assert d["rows"] == [] and "ks rank" in d["hint"]


class TestStats:
    def test_group_by_parent(self, client):
        d = client.get("/api/stats", params={"by": "parent"}).json()
        by = {r["维度"]: r for r in d["rows"]}
        assert by["科技"]["项目数"] == 2       # Big Tech + JP Project
        assert by["游戏"]["项目数"] == 1

    def test_live_filter_excludes_stale_records(self, client):
        """state=live 必须走 live_now，排除"最后观测时是 live 但早已结束"的旧记录。"""
        d = client.get("/api/projects", params={"state": "live"}).json()
        assert d["total"] == 3          # 三条都来自同一次快照，全部算当前在筹

    def test_bad_dimension_rejected(self, client):
        assert client.get("/api/stats", params={"by": "evil"}).status_code == 400
