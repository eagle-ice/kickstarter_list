"""DuckDB 存储层：append-only 快照表 + 派生视图。

设计要点：
- snapshots 是**时序表**，主键 (project_id, snapshot_date)。同一项目每天一行，
  这样才能算出日增量（pledged / backers 的 delta）和状态流转。
- 同一天重复跑幂等：先删当天该 source 的行再插。
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Iterable, Sequence

import duckdb

from .config import DB_PATH
from .normalize import COLUMNS

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_date              DATE      NOT NULL,
    source                     VARCHAR   NOT NULL,   -- 'webrobots' | 'discover'
    run_id                     VARCHAR,
    project_id                 BIGINT    NOT NULL,
    name                       VARCHAR,
    blurb                      VARCHAR,
    slug                       VARCHAR,
    state                      VARCHAR,
    goal                       DOUBLE,
    pledged                    DOUBLE,
    usd_pledged                DOUBLE,
    converted_pledged_amount   DOUBLE,
    percent_funded             DOUBLE,
    backers_count              BIGINT,
    currency                   VARCHAR,
    current_currency           VARCHAR,
    static_usd_rate            DOUBLE,
    fx_rate                    DOUBLE,
    country                    VARCHAR,
    country_displayable_name   VARCHAR,
    created_at                 TIMESTAMPTZ,
    launched_at                TIMESTAMPTZ,
    deadline                   TIMESTAMPTZ,
    state_changed_at           TIMESTAMPTZ,
    duration_days              DOUBLE,
    days_remaining             DOUBLE,
    category_id                BIGINT,
    category_name              VARCHAR,
    category_parent_name       VARCHAR,
    category_slug              VARCHAR,
    location_id                BIGINT,
    location_name              VARCHAR,
    location_state             VARCHAR,
    location_country           VARCHAR,
    creator_id                 BIGINT,
    creator_name               VARCHAR,
    creator_slug               VARCHAR,
    staff_pick                 BOOLEAN,
    spotlight                  BOOLEAN,
    is_starrable               BOOLEAN,
    disable_communication      BOOLEAN,
    prelaunch_activated        BOOLEAN,
    is_launched                BOOLEAN,
    is_post_campaign_pledging  BOOLEAN,
    has_video                  BOOLEAN,
    photo_url                  VARCHAR,
    project_url                VARCHAR
);

CREATE INDEX IF NOT EXISTS idx_snap_project  ON snapshots(project_id);
CREATE INDEX IF NOT EXISTS idx_snap_date     ON snapshots(snapshot_date);
CREATE INDEX IF NOT EXISTS idx_snap_cat      ON snapshots(category_parent_name, category_name);
CREATE INDEX IF NOT EXISTS idx_snap_launched ON snapshots(launched_at);

-- 榜单名次。名次本身就是数据，且日间变动（谁冲进前 10）是最有价值的信号，
-- 所以独立成表而不是塞进 snapshots 的一个列 —— 同一项目在不同榜单有不同名次。
CREATE TABLE IF NOT EXISTS rankings (
    snapshot_date DATE    NOT NULL,
    sort          VARCHAR NOT NULL,   -- most_funded / most_backed / popularity / magic
    state         VARCHAR NOT NULL,   -- 榜单范围，通常是 live
    rank          INTEGER NOT NULL,   -- 1 起
    project_id    BIGINT  NOT NULL,
    run_id        VARCHAR
);

CREATE INDEX IF NOT EXISTS idx_rank_date ON rankings(snapshot_date, sort);
CREATE INDEX IF NOT EXISTS idx_rank_proj ON rankings(project_id);

-- 抓取批次日志，用来判断某天是否已经跑过、跑到了什么程度
CREATE TABLE IF NOT EXISTS ingest_log (
    run_id        VARCHAR,
    source        VARCHAR,
    snapshot_date DATE,
    rows_ingested BIGINT,
    started_at    TIMESTAMPTZ,
    finished_at   TIMESTAMPTZ,
    note          VARCHAR
);
"""

# 汇率推导：跨项目比金额**必须**换算成美元。
# 项目金额以本币计价，¥25,645,602 只值 $162,107 —— 直接按 pledged 排序会让
# 日元/港币项目系统性地压过美元项目。static_usd_rate 实测精确
# (pledged * static_usd_rate == usd_pledged，误差 0)，但约 9000 行为空，故有两级兜底。
USD_RATE_EXPR = """
COALESCE(
    nullif(static_usd_rate, 0),
    CASE WHEN pledged > 0 AND usd_pledged IS NOT NULL THEN usd_pledged / pledged END,
    CASE WHEN currency = 'USD' THEN 1.0 END
)
"""

# 每个项目取最新一条快照
#
# ⚠ latest 里的 state 是**最后一次观测到的状态**，不是当前状态。
# 一个项目 9/10 观测时是 live，之后结束了但没再抓到，它在 latest 里仍然是 live。
# 实测这会让 state='live' 的计数虚高 29%（4009 vs 真实 3101）。
# 要「当前真在筹」请用 live_now 视图。
VIEW_LATEST = f"""
CREATE OR REPLACE VIEW latest AS
SELECT * EXCLUDE (rn),
       {USD_RATE_EXPR}                        AS usd_rate,
       goal * ({USD_RATE_EXPR})               AS goal_usd,
       COALESCE(usd_pledged, pledged * ({USD_RATE_EXPR})) AS pledged_usd,
       date_diff('day', snapshot_date, current_date) AS days_since_observed
FROM (
    SELECT *, ROW_NUMBER() OVER (
        PARTITION BY project_id ORDER BY snapshot_date DESC, source
    ) AS rn
    FROM snapshots
) WHERE rn = 1;
"""

# 当前真正在筹的项目：状态是 live **且**在最近一次全量扫描里被观测到。
# 只判断 state='live' 会混进早就结束、只是没再观测的旧记录。
VIEW_LIVE_NOW = """
CREATE OR REPLACE VIEW live_now AS
SELECT * FROM latest
WHERE state = 'live'
  AND snapshot_date = COALESCE(
      -- 优先用最近一次全量扫描的日期：只有全量才保证覆盖所有在筹项目
      (SELECT max(snapshot_date) FROM ingest_log
       WHERE note LIKE 'full%' AND rows_ingested > 0),
      -- 从没跑过全量时退回最近一次快照，否则这个视图会是空的
      (SELECT max(snapshot_date) FROM snapshots WHERE source = 'discover'),
      (SELECT max(snapshot_date) FROM snapshots)
  );
"""

# 相邻两次快照之间的增量 —— 日更的核心产出
VIEW_DELTAS = f"""
CREATE OR REPLACE VIEW deltas AS
WITH deduped AS (
    -- 同一天可能既有月度 dump 又有当日抓取。不去重的话 LAG 顺序不确定，
    -- 还会多出一条 gap_days=0 的伪增量。每 (项目, 日期) 只保留一条，优先当日抓取。
    SELECT * EXCLUDE (rn) FROM (
        SELECT *, ROW_NUMBER() OVER (
            PARTITION BY project_id, snapshot_date
            ORDER BY CASE source WHEN 'discover' THEN 0 ELSE 1 END
        ) AS rn
        FROM snapshots
    ) WHERE rn = 1
), base AS (
    SELECT *,
           COALESCE(usd_pledged, pledged * ({USD_RATE_EXPR})) AS pledged_usd,
           goal * ({USD_RATE_EXPR})                           AS goal_usd
    FROM deduped
), ordered AS (
    SELECT
        project_id, name, slug, project_url, state, snapshot_date, currency,
        category_name, category_parent_name, country, creator_name,
        goal, goal_usd, pledged, pledged_usd, backers_count, percent_funded,
        launched_at, deadline, days_remaining,
        LAG(pledged_usd)   OVER w AS prev_pledged_usd,
        LAG(pledged)       OVER w AS prev_pledged,
        LAG(backers_count) OVER w AS prev_backers,
        LAG(state)         OVER w AS prev_state,
        LAG(snapshot_date) OVER w AS prev_date
    FROM base
    WINDOW w AS (PARTITION BY project_id ORDER BY snapshot_date)
)
SELECT
    *,
    -- 增量一律用美元口径，本币增量跨项目不可比
    pledged_usd - prev_pledged_usd    AS pledged_delta_usd,
    pledged - prev_pledged            AS pledged_delta_local,
    backers_count - prev_backers      AS backers_delta,
    date_diff('day', prev_date, snapshot_date) AS gap_days,
    -- 按天摊平，跨度不为 1 天时（比如月度 dump）也能横向比较
    CASE WHEN date_diff('day', prev_date, snapshot_date) > 0
         THEN (pledged_usd - prev_pledged_usd) / date_diff('day', prev_date, snapshot_date)
    END                               AS pledged_usd_per_day,
    (state IS DISTINCT FROM prev_state) AS state_changed
FROM ordered
WHERE prev_date IS NOT NULL;
"""

# 榜单 + 项目明细，直接可读
VIEW_LEADERBOARD = f"""
CREATE OR REPLACE VIEW leaderboard AS
SELECT r.snapshot_date, r.sort, r.rank, r.project_id,
       s.name, s.blurb, s.state,
       s.category_parent_name, s.category_name, s.country,
       s.goal, s.pledged, s.currency, s.backers_count, s.percent_funded,
       COALESCE(s.usd_pledged, s.pledged * ({USD_RATE_EXPR})) AS pledged_usd,
       s.goal * ({USD_RATE_EXPR})                             AS goal_usd,
       s.launched_at, s.deadline, s.days_remaining, s.project_url
FROM rankings r
JOIN snapshots s
  ON s.project_id = r.project_id
 AND s.snapshot_date = r.snapshot_date
 AND s.source = 'discover';
"""

# 名次变动：同一榜单相邻两次快照之间的排名升降
VIEW_RANK_MOVES = """
CREATE OR REPLACE VIEW rank_moves AS
WITH ordered AS (
    SELECT snapshot_date, sort, rank, project_id,
           LAG(rank)          OVER w AS prev_rank,
           LAG(snapshot_date) OVER w AS prev_date
    FROM rankings
    WINDOW w AS (PARTITION BY sort, project_id ORDER BY snapshot_date)
)
SELECT *,
       -- 名次数字变小 = 排名上升，所以取 prev - now，正数代表上升了几名
       prev_rank - rank      AS rank_gain,
       (prev_rank IS NULL)   AS is_new_entry
FROM ordered;
"""

VIEWS = [VIEW_LATEST, VIEW_LIVE_NOW, VIEW_DELTAS, VIEW_LEADERBOARD, VIEW_RANK_MOVES]


def sync_i18n(con: duckdb.DuckDBPyConnection) -> None:
    """把译名表写进库里，SQL 查询也能拿到中文。每次 connect 重建，改词表即刻生效。"""
    from . import i18n

    con.execute("CREATE OR REPLACE TABLE dim_state (state VARCHAR, state_zh VARCHAR)")
    con.executemany("INSERT INTO dim_state VALUES (?, ?)", list(i18n.STATES.items()))

    con.execute("CREATE OR REPLACE TABLE dim_parent (parent VARCHAR, parent_zh VARCHAR)")
    con.executemany("INSERT INTO dim_parent VALUES (?, ?)", list(i18n.PARENTS.items()))

    con.execute("CREATE OR REPLACE TABLE dim_country (country VARCHAR, country_zh VARCHAR)")
    con.executemany("INSERT INTO dim_country VALUES (?, ?)", list(i18n.COUNTRIES.items()))

    # 子类需要 (大类, 子类) 联合键，因为 Comedy/Spaces 这类同名词跨大类含义不同
    pairs = con.execute("""
        SELECT DISTINCT category_parent_name, category_name
        FROM snapshots WHERE category_name IS NOT NULL
    """).fetchall()
    con.execute("CREATE OR REPLACE TABLE dim_category "
                "(parent VARCHAR, category VARCHAR, category_zh VARCHAR)")
    if pairs:
        con.executemany("INSERT INTO dim_category VALUES (?, ?, ?)",
                        [(par, cat, i18n.category(cat, par)) for par, cat in pairs])


# 中文视图：latest 的所有列 + 译好的品类/状态/国家
VIEW_LATEST_ZH = """
CREATE OR REPLACE VIEW latest_zh AS
SELECT l.*,
       coalesce(ds.state_zh, l.state)                      AS 状态,
       coalesce(dp.parent_zh, l.category_parent_name)      AS 大类,
       coalesce(dc.category_zh, l.category_name)           AS 子类,
       coalesce(dn.country_zh, l.country)                  AS 国家
FROM latest l
LEFT JOIN dim_state    ds ON ds.state    = l.state
LEFT JOIN dim_parent   dp ON dp.parent   = l.category_parent_name
LEFT JOIN dim_category dc ON dc.parent   IS NOT DISTINCT FROM l.category_parent_name
                          AND dc.category = l.category_name
LEFT JOIN dim_country  dn ON dn.country  = l.country;
"""

VIEW_LIVE_NOW_ZH = """
CREATE OR REPLACE VIEW live_now_zh AS
SELECT * FROM latest_zh
WHERE state = 'live'
  AND snapshot_date = COALESCE(
      -- 优先用最近一次全量扫描的日期：只有全量才保证覆盖所有在筹项目
      (SELECT max(snapshot_date) FROM ingest_log
       WHERE note LIKE 'full%' AND rows_ingested > 0),
      -- 从没跑过全量时退回最近一次快照，否则这个视图会是空的
      (SELECT max(snapshot_date) FROM snapshots WHERE source = 'discover'),
      (SELECT max(snapshot_date) FROM snapshots)
  );
"""

VIEW_DELTAS_ZH = """
CREATE OR REPLACE VIEW deltas_zh AS
SELECT d.*,
       coalesce(ds.state_zh, d.state)                 AS 状态,
       coalesce(dsp.state_zh, d.prev_state)           AS 原状态,
       coalesce(dp.parent_zh, d.category_parent_name) AS 大类,
       coalesce(dn.country_zh, d.country)             AS 国家
FROM deltas d
LEFT JOIN dim_state   ds  ON ds.state  = d.state
LEFT JOIN dim_state   dsp ON dsp.state = d.prev_state
LEFT JOIN dim_parent  dp  ON dp.parent = d.category_parent_name
LEFT JOIN dim_country dn  ON dn.country = d.country;
"""


def connect(path: str | Path | None = None, *, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    path = Path(path or DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path), read_only=read_only)
    if not read_only:
        con.execute(SCHEMA)
        for view in VIEWS:
            con.execute(view)
        sync_i18n(con)
        for view in (VIEW_LATEST_ZH, VIEW_LIVE_NOW_ZH, VIEW_DELTAS_ZH):
            con.execute(view)
    return con


def replace_snapshot(
    con: duckdb.DuckDBPyConnection,
    rows: Sequence[tuple],
    *,
    snapshot_date: date,
    source: str,
) -> int:
    """幂等写入：同一 (日期, source) 重跑会覆盖，不会产生重复行。"""
    if not rows:
        return 0
    # 防呆：行内的 snapshot_date 必须和声明的一致，否则 DELETE 删不掉对应行，
    # 重跑就会累积重复数据（这个错误是静默的，必须在写入前拦住）。
    date_idx = COLUMNS.index("snapshot_date")
    bad = {r[date_idx] for r in rows} - {snapshot_date}
    if bad:
        raise ValueError(
            f"rows 的 snapshot_date {sorted(map(str, bad))} 与声明的 {snapshot_date} 不一致"
        )
    con.execute(
        "DELETE FROM snapshots WHERE snapshot_date = ? AND source = ?",
        [snapshot_date, source],
    )
    placeholders = ", ".join(["?"] * len(COLUMNS))
    con.executemany(
        f"INSERT INTO snapshots ({', '.join(COLUMNS)}) VALUES ({placeholders})", rows
    )
    return len(rows)


def insert_rows(con: duckdb.DuckDBPyConnection, rows: Iterable[tuple]) -> int:
    """纯追加，不做去重 —— 给流式导入大 dump 用。"""
    rows = list(rows)
    if not rows:
        return 0
    placeholders = ", ".join(["?"] * len(COLUMNS))
    con.executemany(
        f"INSERT INTO snapshots ({', '.join(COLUMNS)}) VALUES ({placeholders})", rows
    )
    return len(rows)


def replace_rankings(con, rows: Sequence[tuple], *, snapshot_date: date,
                     sort: str, state: str) -> int:
    """写入某榜单某天的名次，幂等。rows = [(rank, project_id, run_id), ...]"""
    con.execute(
        "DELETE FROM rankings WHERE snapshot_date = ? AND sort = ? AND state = ?",
        [snapshot_date, sort, state],
    )
    if not rows:
        return 0
    con.executemany(
        "INSERT INTO rankings (snapshot_date, sort, state, rank, project_id, run_id) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [(snapshot_date, sort, state, rank, pid, rid) for rank, pid, rid in rows],
    )
    return len(rows)


def log_ingest(con, run_id, source, snapshot_date, rows, started, finished, note=""):
    con.execute(
        "INSERT INTO ingest_log VALUES (?, ?, ?, ?, ?, ?, ?)",
        [run_id, source, snapshot_date, rows, started, finished, note],
    )


def has_snapshot(con, snapshot_date: date, source: str) -> bool:
    got = con.execute(
        "SELECT count(*) FROM snapshots WHERE snapshot_date = ? AND source = ?",
        [snapshot_date, source],
    ).fetchone()
    return bool(got and got[0])
