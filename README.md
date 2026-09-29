# kickstrter

Kickstarter 众筹数据管道：**月度全量打底 + 每日增量抓取**，落 DuckDB，产出日报与分析查询。

用途覆盖：选品找爆款 / 竞品品类监控 / 市场研究 / 自己发起众筹时的对标定档。

---

## 为什么是这个架构

站点侧的实测结论（2026-09 验证）：

| 通道 | 结果 | 说明 |
|---|---|---|
| 官方公开 API | 不存在 | 旧 API 已下线；站内 `/graph` GraphQL 需登录态 |
| `requests` / `httpx` / `curl` | **全部 403** | 整站在 Cloudflare managed challenge 后面，连 `robots.txt` 都 403；补齐 UA、`sec-ch-ua`、`sec-fetch-*` 依然 403 |
| Playwright headless | **403** | `headless-shell` 和 `--headless=new` 都被识破 |
| Playwright **headful** | **200 ✅** | bundled chromium 与 `channel="chrome"` 均可 |
| Web Robots 月度 dump | 200 ✅ | 免费 S3 直链，无需对抗 |

所以：

```
┌─ 历史基线（ks backfill）──────────────────┐
│ Web Robots 月度 dump → DuckDB             │  131 份，回溯到 2015-10
│ 免费 / 零对抗 / 全量，但只有月度粒度        │
└───────────────────────────────────────────┘
                   ↓ 同一张 snapshots 表
┌─ 每日增量（ks daily）─────────────────────┐
│ headful Chromium 过 CF → discover?format=json │
│ 真日更，算出：新上线 / 日增量 / 状态流转      │
└───────────────────────────────────────────┘
```

**关键点**：`discover` 接口返回的 project 对象与 Web Robots dump **完全同构**（实测仅差一个未使用的 `source_url`），因此两条管道共用 `ks/normalize.py` 一套解析和同一张表。

---

## 安装

```bash
python3 -m venv .venv
./.venv/bin/python -m pip install -r requirements.txt
./.venv/bin/python -m playwright install chromium
```

## 快速开始

```bash
# 1. 看有哪些月度 dump 可用（131 份，2015-10 至今）
./.venv/bin/python -m ks backfill --list

# 2. 导入最近一份全量 dump 做基线（~227MB，几分钟）
./.venv/bin/python -m ks backfill --count 1

#    想要历史趋势就多导几份
./.venv/bin/python -m ks backfill --since 2025-01-01

# 3. 跑一轮日更抓取（会弹出浏览器窗口，这是必须的）
./.venv/bin/python -m ks daily --max-pages 30

# 4. 看日报
./.venv/bin/python -m ks report

# 5. 发起众筹前的对标
./.venv/bin/python -m ks benchmark --parent Technology

# 6. 任意 SQL
./.venv/bin/python -m ks sql "SELECT * FROM latest WHERE state='live' ORDER BY pledged DESC LIMIT 10"

# 7. 入库情况
./.venv/bin/python -m ks status
```

## 数据模型

一张 append-only 时序表 + 两个视图：

| 对象 | 说明 |
|---|---|
| `snapshots` | 每 (项目, 快照日, 来源) 一行，47 列。**日增量全靠它** |
| `latest` | 每个项目的最新一条快照 |
| `deltas` | 相邻两次快照的差：`pledged_delta` / `backers_delta` / `pledged_per_day` / `state_changed` |

`pledged_per_day` 会按 `gap_days` 摊平，所以月度 dump 和日更数据能横向比较。

同一 `(snapshot_date, source)` 重跑会先删后插，**幂等**。

## 日报内容

`ks daily` / `ks report` 输出六节：

1. 当日新上线项目
2. 当日筹款增量 Top（正在起量的）
3. 状态流转（live→successful/failed/canceled）
4. 上线 7 天内已达标 —— 早期爆款信号
5. 品类热度（在筹数 / 累计募集 / 当日增量）
6. 关键词监控命中（配置 `watch_keywords` 后出现）

更深的分析见 `sql/analytics.sql`，按四类用途分好了段。

## 配置

编辑 `config.yaml`：

- `sorts` — 日更抓取的检索维度，默认 `newest`(新上线) / `end_date`(冲刺期) / `magic`(平台推荐)
- `watch_categories` — 要深挖的品类 id，如 `16`=Technology、`12`=Games
- `watch_keywords` — 关键词监控，命中 name/blurb
- `max_pages_per_sort` — 每维度翻页上限，每页 12 条。全量在筹约 3200 个，想全覆盖 `newest` 需约 270 页
- `request_delay` — 页间随机间隔，默认 3~7 秒

## 定时跑

**headless 过不了 Cloudflare**，所以定时任务必须有图形会话：

- **macOS**：用 `launchd`（不要用 cron），且需保持已登录的桌面会话
- **Linux 服务器**：套 `xvfb-run`

```bash
# macOS launchd 示例：每天 09:00
# ~/Library/LaunchAgents/com.user.kickstrter.plist 里指向：
cd /path/to/kickstrter && ./.venv/bin/python -m ks daily

# Linux
xvfb-run -a ./.venv/bin/python -m ks daily
```

## Web 界面

```bash
./.venv/bin/python -m ks serve --open      # 默认 http://127.0.0.1:8000
```

三个页签：

- **项目筛选** — 关键词、大类/子类（联动）、国家、状态、已筹区间、完成率、支持者数、
  剩余天数、上线日期；点表头切换排序；分页；一键导出 CSV（带 BOM，Excel 不乱码）
- **榜单** — 4 个榜单维度切换，显示名次变动（↑12 / ↓5 / NEW）
- **品类汇总** — 按大类/子类/国家聚合募集额、中位数、平均完成率

只读连接，不干扰抓取任务。抓取持锁期间接口返回提示而非报错，服务不用重启。
接口文档在 `/api/docs`。

## 全量抓取（建基线）

```bash
./.venv/bin/python -m ks full        # 扫完当前所有在筹项目，约 25 分钟
```

实测 100% 覆盖：3103 条入库，与平台报告的 3101 一致。

**为什么要按品类切片**：Kickstarter 分页硬上限 200 页 = 2400 条（`page=201` 返回 404），
而在筹项目超过 3000 —— 单个检索条件永远抓不全。各品类在筹数合计与平台总数完全一致，
且最大的 Games 只有 743 个，所以按 15 个品类逐个翻页能拿到完整覆盖，页数成本相同。

中途失败或 Ctrl+C，已抓到的数据仍会入库，不会白跑。

### ⚠ 查"当前在筹"要用 live_now，不是 state='live'

`latest` 里的 `state` 是**最后一次观测到的状态**。项目结束后若没再抓到，它在 `latest`
里仍然显示 `live`。实测虚高 29%：

```
latest 里 state='live'   4,009   ← 含 906 条早已结束的旧记录
live_now                 3,103   ← 真实在筹（平台报告 3101）
```

所以自己写 SQL 查当前在筹时，用 `live_now_zh` 而不是 `latest_zh`。
日报、Web 界面、`sql/analytics.sql` 都已经切过去了。

## 补结局（别漏这步）

```bash
./.venv/bin/python -m ks outcomes        # 抓最近结束项目的成败
```

**为什么必须单独抓**：项目一结束就从 `state=live` 的结果里消失。只跑 `ks full` / `ks new`
的话，你永远观测不到任何项目变成 successful 还是 failed，库里只会留下一堆过期的 `live`
记录。实测 3 天内有 223 个项目这样消失。

有效终态只有 `successful` / `failed` / `canceled`（`suspended` 不是有效筛选，KS 会忽略）。
分页上限下可回溯 68~87 天，日常增量足够。

## 增量抓取（只抓新的）

```bash
./.venv/bin/python -m ks new                    # 自动从库里最后一次快照往后补
./.venv/bin/python -m ks new --since 2026-09-18 # 指定起点
```

实测：补 2 天的新项目 **1 分 21 秒**（138 个，翻 15 页就停），全量抓要约 20 分钟。

原理：`sort=newest` 严格按上线时间倒序（实测 120 条零违例），翻到早于起点的项目就停，
再多翻 2 页防边界抖动。

### ⚠ 增量只能补「新上线」，补不了「筹款变化」

这是 Kickstarter 接口的硬限制，不是实现问题：

- discover **没有 `updated_since` 之类的过滤参数**（实测 `launched_after` / `since` /
  `date_range` 全部被忽略，表现和瞎编的 `bogus_param_xyz` 完全一样）
- 3100 个在筹项目的金额每天都在变，想知道谁涨了多少，**只能重新抓一遍**

所以：

| 你要什么 | 用什么 | 耗时 |
|---|---|---|
| 今天新上线了哪些项目 | `ks new` | ~1 分钟 |
| 谁在涨、涨了多少（日增量） | `ks daily` 或 `ks rank` | 4~20 分钟 |
| 名次升降 | `ks rank` | ~6 分钟 |

**只跑 `ks new` 的话，`deltas` 视图和日报的「筹款增量」「名次变动」会是空的**，
因为算增量需要同一批项目的两次快照。

实用组合：每天 `ks new`（1 分钟）掌握新品，每周或隔几天跑一次 `ks rank --top 1000`
（6 分钟）拿存量项目的进展。

## 榜单抓取

```bash
./.venv/bin/python -m ks rank --sort most_funded --top 1000   # 抓前 1000 名
./.venv/bin/python -m ks board --sort most_funded --top 50    # 查看已入库榜单
```

可用榜单：`most_funded` 募集金额榜 / `most_backed` 支持者数榜 / `popularity` 人气榜 /
`magic` 平台推荐榜 / `newest` 最新上线 / `end_date` 临近结束。

实测分页可翻到 150 页以上，前 1000 名（84 页）没有问题，约 6 分钟。

**名次会有小幅倒挂，这是正常的**：1000 名要跨 84 次请求、约 6 分钟抓完，期间项目金额
在变。实测 244/1000 处倒挂，其中 65% 幅度 <1%，且集中在名次靠后（前 100 名只有 5 处）。
已验证不是分页 bug——倒挂在翻页边界的占比 8.2%，与随机预期 8.3% 一致。

## 中文显示

品类、状态、国家都已译成中文，日报和 `sql/analytics.sql` 直接可读。

自己写 SQL 时用 `latest_zh` / `deltas_zh` 视图，它们比 `latest` / `deltas`
多出 `状态`、`大类`、`子类`、`国家` 四个中文列：

```bash
./.venv/bin/python -m ks sql "
SELECT name AS 项目, 大类, 子类, 国家, 状态,
       round(pledged_usd) AS 已筹USD, round(percent_funded) AS 完成率
FROM latest_zh WHERE state='live' ORDER BY pledged_usd DESC LIMIT 10"
```

**项目名和一句话简介保持英文原文**，没有机器翻译 —— 译了既不准确，也没法拿去
Kickstarter 站内搜索或跟原页面对照。日报里的项目名可以直接点击打开原页面。

## 两个必须知道的数据口径

### 1. 金额一律看美元列

项目金额以**本币**计价。`pledged` 跨项目比较是错的 —— 一个 ¥25,645,602 的日元项目
实际只有 $162,107，按本币排序会排在 $10,433,558 的美元项目前面。

视图已派生 `pledged_usd` / `goal_usd` / `pledged_delta_usd`，报表和 `sql/analytics.sql`
全部用美元口径。自己写查询时也请用这些列。

### 2. 数据源偏差：绝对成功率偏高

Web Robots 爬的是 discover 列表页，过采样热门/成功项目。本库整体成功率约 **67%**，
而 Kickstarter 官方口径约 **41%** —— 长尾失败项目收不全。

**不要直接引用绝对成功率**。横向比较（哪个目标档位、哪个品类更容易成）仍然有效，
因为偏差在各分层之间大致同向。`ks benchmark` 每次都会打印这个提醒。

## 注意：DuckDB 单写者

抓取任务运行时数据库被独占锁定，`ks report` 等命令会报 `Conflicting lock is held`。
定时任务里把抓取和出报表串行执行；想在抓取途中查数据就先复制一份 `.duckdb` 文件。

## 测试

```bash
./.venv/bin/python -m pytest tests/ -q          # 全部
./.venv/bin/python -m pytest tests/ -q -k delta # 单个
```

## 合规提醒

Kickstarter 的服务条款禁止自动化抓取。本项目默认配置刻意保守（单日一轮、页间 3~7 秒随机、并发为 1），**请不要为了跑快调低间隔**。

如果需要完全无风险的路线，只用 `ks backfill`（Web Robots 是第三方公开发布的数据集），放弃日更粒度。
