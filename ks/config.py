"""配置加载。config.yaml 覆盖默认值。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "kickstarter.duckdb"


@dataclass
class Config:
    # --- 抓取节流：KS 的 ToS 禁止自动化抓取，这里刻意保守 ---
    request_delay: tuple[float, float] = (3.0, 7.0)   # 每页之间随机 sleep 秒数
    max_pages_per_sort: int = 20                      # discover 单个排序最多翻多少页（每页 12 条）
    headless: bool = True
    nav_timeout_ms: int = 60_000

    # --- 日更抓取范围 ---
    # discover 的排序维度；newest 抓新上线，end_date 抓临近结束，magic 抓平台推荐热度
    sorts: list[str] = field(default_factory=lambda: ["newest", "end_date", "magic"])
    # 额外要单独深挖的品类 slug（竞品/特定品类监控）
    watch_categories: list[str] = field(default_factory=list)
    # 关键词监控，落库后在 report 里做匹配
    watch_keywords: list[str] = field(default_factory=list)

    # --- Web Robots 月度全量 ---
    webrobots_index: str = "https://webrobots.io/kickstarter-datasets/"

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Config":
        path = Path(path or os.environ.get("KS_CONFIG", ROOT / "config.yaml"))
        cfg = cls()
        if path.exists():
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            for key, val in raw.items():
                if hasattr(cfg, key):
                    if key == "request_delay" and isinstance(val, list):
                        val = tuple(val)
                    setattr(cfg, key, val)
        return cfg
