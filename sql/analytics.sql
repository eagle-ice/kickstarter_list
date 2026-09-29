-- Kickstarter 分析查询集
-- 用法：./.venv/bin/python -m ks sql "<粘贴其中一段>"
--
-- ⚠ 金额一律用 *_usd 列（pledged_usd / goal_usd）。项目金额以本币计价，
--   ¥25,645,602 只值 $162,107 —— 按 pledged 排序会让日元/港币项目系统性地压过美元项目。
-- ⚠ 数据源偏差：Web Robots 爬 discover 列表页，过采样成功项目。本库整体成功率约 67%，
--   而 Kickstarter 官方口径约 41%。绝对成功率不可直接引用，横向比较仍有效。
-- 或整体在 DuckDB CLI 里跑。
--
-- 核心对象：
--   snapshots   时序原表（每 项目×日期×来源 一行）
--   latest      每项目最新一条快照
--   deltas      相邻两次快照的增量
--   latest_zh    latest + 中文的 状态/大类/子类/国家 列
--   live_now_zh  **当前真正在筹**的项目（中文列）
--   deltas_zh    deltas + 中文列
--
-- ⚠ latest 里的 state='live' 是"最后观测时在筹"，不是"现在在筹"。项目结束后若没再
--   抓到，它在 latest 里仍然是 live。实测虚高 29%（4009 vs 真实 3101）。
--   要"当前在筹"一律用 live_now_zh。

-- ════════════════════════════════════════════════════ 一、选品 / 找爆款

-- 1.1 早期增长斜率榜：上线 ≤14 天、日均募集额最高的在筹项目
--     这是判断爆款最早的信号，比绝对金额更灵敏
SELECT name AS 项目, 大类, 子类, 国家,
       date_diff('day', launched_at::DATE, snapshot_date) AS 上线天数,
       round(pledged_usd)                                  AS 已筹USD,
       round(percent_funded, 1)                            AS 完成率,
       round(pledged_usd / greatest(date_diff('day', launched_at::DATE, snapshot_date), 1)) AS 日均募集USD,
       backers_count AS 支持者, project_url
FROM live_now_zh
WHERE date_diff('day', launched_at::DATE, snapshot_date) BETWEEN 1 AND 14
ORDER BY 日均募集USD DESC
LIMIT 40;

-- 1.2 超额倍数榜：完成率最离谱的项目，看什么品类天生容易爆
SELECT 大类, name AS 项目,
       round(goal_usd) AS 目标USD, round(pledged_usd) AS 已筹USD,
       round(percent_funded) AS 完成率, backers_count AS 支持者,
       round(pledged_usd / nullif(backers_count, 0), 1) AS 客单价USD, project_url
FROM latest_zh
WHERE state IN ('live', 'successful') AND percent_funded >= 500 AND goal_usd >= 1000
ORDER BY percent_funded DESC
LIMIT 40;

-- 1.3 连续多日高增长：真实动能，排除单日刷量
SELECT project_id, any_value(name) AS name,
       any_value(大类) AS 大类,
       count(*)                        AS 连续增长天数,
       round(sum(pledged_delta_usd))   AS 区间总增量USD,
       round(avg(pledged_usd_per_day)) AS 日均增量USD,
       any_value(project_url)          AS project_url
FROM deltas_zh
WHERE pledged_delta_usd > 0 AND snapshot_date >= current_date - INTERVAL 14 DAY
GROUP BY project_id
HAVING count(*) >= 3
ORDER BY 区间总增量USD DESC
LIMIT 40;

-- ════════════════════════════════════════════════════ 二、竞品 / 品类监控

-- 2.1 指定关键词的全部历史项目（改 term）
SELECT name AS 项目, 大类, 状态, 国家,
       round(goal_usd) AS 目标USD, round(pledged_usd) AS 已筹USD,
       round(percent_funded, 1) AS 完成率,
       backers_count AS 支持者, launched_at::DATE AS 上线日, project_url
FROM latest_zh
WHERE lower(name) LIKE '%3d print%' OR lower(blurb) LIKE '%3d print%'
ORDER BY pledged_usd DESC NULLS LAST
LIMIT 60;

-- 2.2 子类目逐年成功率与规模变化 —— 看赛道是在变热还是在退潮
SELECT 大类, 子类,
       year(launched_at)    AS 年份,
       count(*)             AS 项目数,
       round(100.0 * sum(CASE WHEN state = 'successful' THEN 1 ELSE 0 END) / count(*), 1) AS 成功率,
       round(median(pledged_usd)) AS 募集中位数USD
FROM latest_zh
WHERE state IN ('successful', 'failed') AND launched_at IS NOT NULL
  AND 大类 = '科技'
GROUP BY 1, 2, 3
HAVING count(*) >= 20
ORDER BY 子类, 年份;

-- 2.3 头部创作者：反复发起且持续成功的人，值得重点跟
SELECT creator_name AS 创作者, count(*) AS 项目数,
       round(sum(pledged_usd)) AS 累计募集USD,
       round(100.0 * sum(CASE WHEN state = 'successful' THEN 1 ELSE 0 END) / count(*), 1) AS 成功率,
       any_value(大类) AS 主营大类
FROM latest_zh
WHERE creator_name IS NOT NULL AND state IN ('successful', 'failed')
GROUP BY 1
HAVING count(*) >= 3
ORDER BY 累计募集USD DESC
LIMIT 40;

-- ════════════════════════════════════════════════════ 三、市场研究

-- 3.1 大盘：逐年项目数、成功率、募集总额
SELECT year(launched_at) AS 年份, count(*) AS 项目数,
       round(100.0 * sum(CASE WHEN state = 'successful' THEN 1 ELSE 0 END) / count(*), 1) AS 成功率,
       round(sum(usd_pledged) / 1e6, 1) AS 募集总额_百万美元,
       round(median(goal_usd))          AS 目标额中位数USD
FROM latest_zh
WHERE state IN ('successful', 'failed') AND launched_at IS NOT NULL
GROUP BY 1 ORDER BY 1;

-- 3.2 国家分布与成功率
SELECT 国家, count(*) AS 项目数,
       round(100.0 * sum(CASE WHEN state = 'successful' THEN 1 ELSE 0 END) / count(*), 1) AS 成功率,
       round(median(usd_pledged)) AS 募集中位数_美元
FROM latest_zh
WHERE state IN ('successful', 'failed')
GROUP BY 1 HAVING count(*) >= 100
ORDER BY 项目数 DESC LIMIT 30;

-- 3.3 大类竞争格局：项目数 vs 成功率，找「人少但好做」的赛道
SELECT 大类, count(*) AS 项目数,
       round(100.0 * sum(CASE WHEN state = 'successful' THEN 1 ELSE 0 END) / count(*), 1) AS 成功率,
       round(median(pledged_usd)) AS 募集中位数USD,
       round(median(backers_count)) AS 支持者中位数
FROM latest_zh
WHERE state IN ('successful', 'failed') AND 大类 IS NOT NULL
GROUP BY 1 ORDER BY 成功率 DESC;

-- ════════════════════════════════════════════════════ 四、自己发起众筹：对标定档

-- 4.1 目标额区间 × 成功率（改 category_parent_name 到你的赛道）
SELECT CASE WHEN goal_usd < 1000 THEN 'a <1k' WHEN goal_usd < 5000 THEN 'b 1k-5k'
            WHEN goal_usd < 10000 THEN 'c 5k-10k' WHEN goal_usd < 50000 THEN 'd 10k-50k'
            WHEN goal_usd < 100000 THEN 'e 50k-100k' ELSE 'f >100k' END AS 目标额区间USD,
       count(*) AS 样本数,
       round(100.0 * sum(CASE WHEN state = 'successful' THEN 1 ELSE 0 END) / count(*), 1) AS 成功率,
       round(median(backers_count)) AS 支持者中位数
FROM latest_zh
WHERE state IN ('successful', 'failed') AND goal_usd > 0
  AND 大类 = '科技'
GROUP BY 1 ORDER BY 1;

-- 4.2 众筹周期长度 × 成功率 —— 30 天是不是真的最优
SELECT CASE WHEN duration_days <= 15 THEN 'a ≤15天' WHEN duration_days <= 30 THEN 'b 16-30天'
            WHEN duration_days <= 45 THEN 'c 31-45天' ELSE 'd >45天' END AS 周期,
       count(*) AS 样本数,
       round(100.0 * sum(CASE WHEN state = 'successful' THEN 1 ELSE 0 END) / count(*), 1) AS 成功率,
       round(median(pledged_usd)) AS 募集中位数USD
FROM latest_zh
WHERE state IN ('successful', 'failed') AND duration_days IS NOT NULL
GROUP BY 1 ORDER BY 1;

-- 4.3 视频 / 编辑推荐对成功率的影响
SELECT has_video AS 有视频, staff_pick AS 编辑推荐, count(*) AS 样本数,
       round(100.0 * sum(CASE WHEN state = 'successful' THEN 1 ELSE 0 END) / count(*), 1) AS 成功率,
       round(median(pledged_usd)) AS 募集中位数USD
FROM latest_zh
WHERE state IN ('successful', 'failed')
GROUP BY 1, 2 ORDER BY 1, 2;

-- 4.4 客单价参考：同类目每个支持者平均掏多少，用来反推档位设置
SELECT 大类, 子类,
       count(*) AS 样本数,
       round(median(pledged_usd / nullif(backers_count, 0)), 1) AS 客单价中位数USD,
       round(quantile_cont(pledged_usd / nullif(backers_count, 0), 0.25), 1) AS p25,
       round(quantile_cont(pledged_usd / nullif(backers_count, 0), 0.75), 1) AS p75
FROM latest_zh
WHERE state = 'successful' AND backers_count >= 10
GROUP BY 1, 2 HAVING count(*) >= 30
ORDER BY 客单价中位数USD DESC LIMIT 40;
