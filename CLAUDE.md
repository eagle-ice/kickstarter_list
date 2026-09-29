# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目性质

Kickstarter 众筹数据管道。两个数据源合流到一张 DuckDB 时序表，产出日报和分析查询。
所有代码注释、CLI 提示、报表标题用中文；标识符和 SQL 用英文。

## 命令

所有命令走 venv，不要用系统 python：

```bash
./.venv/bin/python -m ks backfill --list        # 列出可用月度 dump
./.venv/bin/python -m ks backfill --count 1     # 导入最近 1 份全量（~227MB）
./.venv/bin/python -m ks backfill --since 2025-01-01
./.venv/bin/python -m ks daily --max-pages 30   # 日更抓取（会弹浏览器窗口）
./.venv/bin/python -m ks report                 # 日报
./.venv/bin/python -m ks benchmark --parent Technology
./.venv/bin/python -m ks sql "SELECT ..."       # 也接 .sql 文件路径
./.venv/bin/python -m ks status

./.venv/bin/python -m pytest tests/ -q
./.venv/bin/python -m pytest tests/ -q -k delta          # 单个测试
./.venv/bin/python -m pytest tests/test_pipeline.py::TestDB::test_deltas_computed
```

环境搭建：`python3 -m venv .venv && ./.venv/bin/python -m pip install -r requirements.txt && ./.venv/bin/python -m playwright install chromium`

## 抓取侧的硬约束（改抓取代码前必读）

2026-09 实测，这些不是猜测：

- kickstarter.com 全站在 **Cloudflare managed challenge** 后面，**连 `/robots.txt` 都返回 403**。
- `requests` / `httpx` / `curl` 拿不到任何内容。补齐 UA、`sec-ch-ua`、`sec-fetch-*`、HTTP/2 **依然 403**。不要再尝试纯 HTTP 客户端方案。
- Playwright **headless 一律 403** —— `headless-shell` 和 `channel="chromium"` 的 `--headless=new` 都被识破。
- Playwright **headful 才能过**（bundled chromium 和 `channel="chrome"` 都行，返回 200）。
- 因此 `config.yaml` 的 `headless: false` **不能改成 true**。`ks daily --headless` 只是调试开关，实际会失败。
- 定时任务必须有图形会话：Linux 用 `xvfb-run`，macOS 用 `launchd` 且需已登录桌面。

过了挑战后，在页面上下文里 `page.evaluate` 发 `fetch` 打 `?format=json`，靠的是页面已有的会话状态，**不要换成 `page.request` 或 `context.request`**（它们走独立的请求栈，会丢掉挑战结果）。

## 架构

```
ks/sources/webrobots.py ─┐
                         ├─→ ks/normalize.py ─→ ks/db.py (snapshots) ─→ ks/report.py
ks/sources/discover.py  ─┘
```

**两个数据源返回同构对象**，这是整个设计的支点：Web Robots dump 里的 `data` 字段和站内
`discover?format=json` 返回的 project 对象字段完全一致（dump 仅多一个未使用的 `source_url`）。
所以只有一个 normalizer、一张表。**新增数据源前先确认它能压成同一个 47 列结构**，否则不要硬塞。

### 存储模型（`ks/db.py`）

- `snapshots` 是 **append-only 时序表**，粒度 (project_id, snapshot_date, source)。日增量完全依赖它，
  所以**不要改成按 project_id upsert 的维度表** —— 那样会丢掉全部时序能力。
- `latest` 视图 = 每项目最新一条；`deltas` 视图 = 相邻快照差值。
- `replace_snapshot()` 先删后插实现幂等，并且**校验行内 `snapshot_date` 与声明值一致**。
  这个校验不是多余的：不一致时 DELETE 匹配不到旧行，重跑会静默累积重复数据。
- `deltas.pledged_per_day` 按 `gap_days` 摊平，让月度 dump 和日更数据可横向比较。

### 中文显示（`ks/i18n.py`）

品类、状态、国家全部译成中文。**只翻译枚举值**（15 大类 / 174 子类 / 7 状态 / 25 国家）——
这些是固定集合可以人工校准。项目名和 blurb 是自由文本且多为专有名词，**不做机器翻译**：
译了既不准确，也没法拿去站内搜索或和原页面对照。

- 同名子类跨大类含义不同（Comedy 在影视=喜剧片、在音乐=喜剧音乐），用 `_BY_PARENT`
  的 `(大类, 子类)` 键覆盖 `_FLAT`。加新词时先确认有没有跨大类冲突。
- `R&B` / `DIY` 在 `AS_IS` 里，中文语境直接用原文，不算漏译。
- 未收录的值**原样返回**，不报错。Kickstarter 加了新品类不会让报表崩。
- `connect()` 每次会调 `sync_i18n()` 重建 `dim_*` 维表，改词表即刻生效，不用迁移。
- SQL 用 `latest_zh` / `deltas_zh` 视图，它们比 `latest` / `deltas` 多出
  `状态` / `大类` / `子类` / `国家` 中文列。`sql/analytics.sql` 已全部用中文视图。
- `tests/test_pipeline.py::TestI18n` 锁住同名冲突和未知值透传。

### 分页硬上限 200 页 / 2400 条

实测 `page=200` 返回 200、`page=201` 返回 **404**。单个检索条件最多只能拿到 2400 个项目，
而在筹总数超过 3000 —— **不切片就必然抓不全**。

- `ks full` 按 `PARENT_CATEGORIES`（15 个品类）逐个翻页绕过上限。实测各品类在筹数
  合计与平台 total_hits 完全一致（3101 vs 3101），最大的 Games 只有 743，都远低于 2400。
- `fetch_page` 把 404 当作"翻完了"返回空页，**不能改回抛异常** —— 之前就是因为抛异常，
  深翻到 201 页时整轮崩溃，已抓的 2400 条全部丢失。
- `iter_projects` / `iter_ranked` / `iter_since` 的翻页上限都被 `min(..., MAX_PAGE)` 夹住。
- `cmd_full` 用 try/except 包住抓取循环，中途失败或 Ctrl+C 时**已抓到的数据照样入库**。

### 只抓 live 会永远拿不到项目结局

项目一结束就从 `state=live` 的结果里消失，我们观测不到它变成 successful / failed /
canceled。实测 3 天内有 **223 个项目**这样消失，库里却还标着 `live`。

- `ks outcomes` 按 `sort=end_date` + 终态筛选抓最近结束的项目，用 `deadline` 做提前退出
  （实测这个组合确实是"最近结束在前"）。
- **有效终态只有 `successful` / `failed` / `canceled`**。`suspended` 不是有效筛选 ——
  它和瞎编的 state 值返回完全一致（684254 条且实际 state 全是 live），KS 直接忽略。
  别加进 `TERMINAL_STATES`，只会白跑几百个请求。
- 200 页上限下可回溯 68~87 天，日常增量够用；更早的结局补不回来。
- **`ks outcomes` 是日常流程的必要一步**，不跑就会持续累积过期 live 记录，
  而且永远不知道哪些项目最终成功了 —— 这对"研究什么项目会成功"直接致命。

### state='live' 不等于"现在在筹"

`latest` 取每个项目的最后一条快照，所以 `state` 是**最后一次观测到的状态**。
项目结束后若没再抓到，它在 `latest` 里仍然是 `live`。实测虚高 29%（4009 vs 真实 3101，
其中 604 条来自 9/10 的月度 dump）。

- **当前在筹一律用 `live_now` / `live_now_zh` 视图**，它限定 snapshot_date 等于
  最近一次全量扫描的日期。
- 视图有兜底：没跑过 `ks full` 时退回最近一次 discover 快照，否则视图会是空的。
- `latest` 多了 `days_since_observed` 列，可以判断数据有多旧。
- web 的 `state=live` 筛选和 `/api/stats` 都已切到 `live_now_zh`；
  其余状态（successful/failed 等）仍查 `latest_zh`，因为终态不会再变。

### 金额一律用美元口径（最容易踩的坑）

项目金额以**本币**计价。`pledged` 是本币金额，跨项目直接比较或排序是错的：
一个 ¥25,645,602 的日元项目实际只有 $162,107，却会排在 $10,433,558 的美元项目前面。

- `latest` / `deltas` 视图派生了 `usd_rate`、`goal_usd`、`pledged_usd`、`pledged_delta_usd`、
  `pledged_usd_per_day`。**所有跨项目的金额比较、排序、求和、分档都必须用这些列。**
- 换算依据 `static_usd_rate`（实测 `pledged * static_usd_rate == usd_pledged`，误差 0）。
  该字段为空时依次回退到 `usd_pledged/pledged` 比值、`currency='USD'` 时取 1.0。
- 仍为 NULL 的行全是 `submitted`/`started` 未上线草稿（pledged=0），分析里本就被过滤掉。
- `tests/test_pipeline.py::TestCurrency` 锁住这个行为，其中一条测试**反向确认**
  按本币排序会出错——不要因为"看起来是在测 bug"就删掉它。

### 数据源有选择性偏差

Web Robots 爬的是 discover 列表页，过采样热门/成功项目。本库整体成功率约 **67%**，
而 Kickstarter 官方口径约 **41%**。

- **绝对成功率不可直接引用**，横向比较（哪个目标档位/品类更优）仍然有效。
- `ks benchmark` 每次输出都会带上 `_bias_note()` 的提醒，不要删掉它。

### 字段约定（`ks/normalize.py`）

- `COLUMNS` 列表与 `db.py` 的 `CREATE TABLE` 列顺序**必须严格一致**，改一处就要改另一处。
  `tests/test_pipeline.py::TestNormalize::test_row_matches_columns` 会兜住长度不匹配。
- 时间戳字段是 UTC 秒，统一转 aware datetime；解析失败返回 `None` 而不是抛异常。
- `usd_pledged` 在 dump 里是**字符串**、在 discover 里是数字，一律过 `_num()`。
- `percent_funded` discover **不返回**，靠 `pledged/goal` 兜底计算；`goal=0` 时返回 `None`。
- `category_parent_name` discover **不返回**，从 `category.slug`（形如 `technology/hardware`）推导；
  顶级品类（slug 无 `/`，如 `music`）父类目就是它自己，否则这些项目会落进空分类。

### 分页（`ks/sources/discover.py`）

首页响应里的 `seed` 必须透传给后续页，否则结果集会在翻页途中重排，导致漏抓和重抓。
每页固定 12 条。翻到全是重复 id 的页说明触到分页上限，直接停。

### DuckDB 单写者锁

DuckDB 是单进程写。`ks daily` / `ks backfill` 跑的时候**任何其他连接都打不开**，
`read_only=True` 也不行（会报 `Conflicting lock is held`）。所以：

- 不要让 `ks daily` 和 `ks report` 同时跑，定时任务里要串行。
- 想在抓取过程中查数据，先 `.duckdb` 文件复制一份再查。

### Web 服务（`ks/web.py` + `ks/static/index.html`）

只读 FastAPI 服务，前端是单个无构建步骤的 HTML。

- **`ORDER BY` 不能参数化**，排序字段必须走 `SORTABLE` 白名单，非法值返回 400。
  其余筛选条件一律参数化绑定，**不要**用 f-string 把用户输入拼进 SQL。
  `tests/test_web.py` 有注入用例锁这个行为。
- `Db` 类惰性开只读连接。抓取任务持写锁时抛 503 并附中文说明，服务不退出，
  抓完自动恢复 —— 不要改成启动时建连接，那样抓取一开始服务就废了。
- 前端筛选传的是**中文**大类/子类（对应 `latest_zh` 的中文列），不是英文。
- DuckDB 读 `TIMESTAMPTZ` 需要 `pytz`，漏装会在查询时报
  `Required module 'pytz' failed to import`，不是启动时报。

### 增量抓取（`iter_since` / `ks new`）

- 靠 `sort=newest` **严格按 launched_at 倒序**这一前提做提前退出（实测 120 条零违例）。
  如果哪天这个前提不成立了，增量会漏抓 —— 改动前先重测倒序。
- `grace_pages=2`：遇到第一页全是过期项目后再多翻 2 页才真停，防翻页边界抖动漏抓。
  不要为了省时间把它改成 0。
- 没有 `launched_at` 的记录**保留**而不是丢弃，宁可多抓不可漏。
- 写库用 `insert_rows` 追加 + 先查当天已有 project_id 去重，**不能用
  `replace_snapshot`** —— 那会把同一天 `ks daily` 已抓的数据整批删掉。
- **Kickstarter 没有任何按更新时间过滤的参数**。实测 `launched_after` / `since` /
  `date_range` 返回 200 但完全被忽略（和瞎编参数表现一致），而 `raised` / `goal` /
  `staff_picks` 确实生效。所以存量项目的金额变化只能靠重抓，不要再去找增量接口。

### 榜单（`rankings` 表 / `ks rank`）

- 名次独立成表而非 snapshots 的列 —— 同一项目在不同榜单有不同名次。
- `iter_ranked()` **必须严格保序**，名次就是结果流里的位置。重复 id 跳过但不占名次
  （KS 偶尔在翻页边界重复返回同一项目），所以内部多留了 5 页余量。
- 实测 KS 的 `most_funded` 按**美元**排序，不是本币。
- **名次有小幅倒挂是正常现象**，不是 bug：1000 名跨 84 次请求约 6 分钟抓完，期间金额在变。
  实测 244/1000 处倒挂、65% 幅度 <1%、集中在靠后名次；翻页边界占比 8.2% 对比随机预期
  8.3%，已排除分页 bug。不要为了"修"这个去加复杂的重排逻辑。

### DuckDB 保留字

`rows`、`local` 都是保留字，用作列别名会报 `syntax error at or near`。
这类错误只在运行时暴露，加新查询时留意。

## 修改时注意

- 调低 `request_delay` 或提高并发会增加被 Cloudflare 拉黑的风险，整条管道随之报废。默认值是刻意保守的。
- `ks daily` 里单个检索维度失败不中断整轮（外层 try/except）—— 保留这个行为，部分数据好过没数据。
- 大 dump 用 `iter_records_stream()` 边下边解析，不要一次性读进内存（单份 227MB gz）。
- `data/` 下的 duckdb、dumps、browser-profile 都已在 `.gitignore` 里。
