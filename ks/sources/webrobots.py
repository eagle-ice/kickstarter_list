"""Web Robots 月度全量 dump —— 历史基线来源。

https://webrobots.io/kickstarter-datasets/ 免费发布 Kickstarter 全站爬取结果，
S3 直链，NDJSON(.json.gz)。实测约 227MB/份，每行形如：
    {"table_id":..., "run_id":..., "data": {<完整 project 对象>}}
月度节奏，不是日更 —— 它负责打底和回补，日更由 discover.py 负责。
"""
from __future__ import annotations

import gzip
import io
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterator

import httpx

from ..config import DATA_DIR, Config

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
# 形如 Kickstarter_2026-09-10T03_20_48_478Z.json.gz
_LINK_RE = re.compile(r'https://s3\.amazonaws\.com/weruns/forfun/Kickstarter/Kickstarter_([\d\-]+)T[\d_]+Z\.json\.gz')


@dataclass(frozen=True)
class Dump:
    url: str
    dump_date: date

    @property
    def filename(self) -> str:
        return self.url.rsplit("/", 1)[-1]


def list_dumps(cfg: Config | None = None, *, timeout: float = 60.0) -> list[Dump]:
    """抓索引页，按日期倒序返回所有可用 dump。"""
    cfg = cfg or Config.load()
    with httpx.Client(follow_redirects=True, timeout=timeout, headers={"User-Agent": UA}) as client:
        html = client.get(cfg.webrobots_index).raise_for_status().text

    seen: dict[str, Dump] = {}
    for match in _LINK_RE.finditer(html):
        url = match.group(0)
        if url in seen:
            continue
        seen[url] = Dump(url=url, dump_date=date.fromisoformat(match.group(1)))
    return sorted(seen.values(), key=lambda d: d.dump_date, reverse=True)


def download(dump: Dump, dest_dir: Path | None = None, *, timeout: float = 600.0) -> Path:
    """流式下载到本地，已存在且大小对得上就跳过。"""
    dest_dir = Path(dest_dir or DATA_DIR / "dumps")
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / dump.filename

    with httpx.Client(follow_redirects=True, timeout=timeout, headers={"User-Agent": UA}) as client:
        head = client.head(dump.url)
        expected = int(head.headers.get("content-length", 0))
        if dest.exists() and expected and dest.stat().st_size == expected:
            return dest

        tmp = dest.with_suffix(dest.suffix + ".part")
        with client.stream("GET", dump.url) as resp:
            resp.raise_for_status()
            with tmp.open("wb") as fh:
                for chunk in resp.iter_bytes(chunk_size=1 << 20):
                    fh.write(chunk)
        tmp.replace(dest)
    return dest


def iter_records(path: Path) -> Iterator[tuple[dict, str]]:
    """流式解析 NDJSON.gz，yield (project 对象, run_id)。坏行跳过不中断。"""
    with gzip.open(path, "rt", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            data = obj.get("data")
            if isinstance(data, dict) and data.get("id"):
                yield data, obj.get("run_id") or ""


def iter_records_stream(dump: Dump, *, timeout: float = 600.0) -> Iterator[tuple[dict, str]]:
    """不落盘直接边下边解析 —— 只想入库一次、不想留 227MB 文件时用。"""
    with httpx.Client(follow_redirects=True, timeout=timeout, headers={"User-Agent": UA}) as client:
        with client.stream("GET", dump.url) as resp:
            resp.raise_for_status()
            raw = _ResponseReader(resp)
            with gzip.open(raw, "rt", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    data = obj.get("data")
                    if isinstance(data, dict) and data.get("id"):
                        yield data, obj.get("run_id") or ""


class _ResponseReader(io.RawIOBase):
    """把 httpx 的流式响应包成 file-like，好让 gzip 直接读。"""

    def __init__(self, response):
        self._it = response.iter_bytes(chunk_size=1 << 20)
        self._buf = b""

    def readable(self) -> bool:
        return True

    def readinto(self, b) -> int:
        while not self._buf:
            try:
                self._buf = next(self._it)
            except StopIteration:
                return 0
        n = min(len(b), len(self._buf))
        b[:n] = self._buf[:n]
        self._buf = self._buf[n:]
        return n
