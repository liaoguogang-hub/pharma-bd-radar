"""
数据模型与共用工具。
所有模块通过这里交换结构化数据，避免各模块自己定义格式。
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Literal

import yaml

# ============ 路径 ============
ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
SCORED_DIR = DATA_DIR / "scored"
SEEN_FILE = DATA_DIR / "seen.json"

CST = timezone(timedelta(hours=8))  # 医药新闻按北京时间判断新鲜度


def ensure_dirs() -> None:
    for d in (DATA_DIR, RAW_DIR, SCORED_DIR):
        d.mkdir(parents=True, exist_ok=True)


# ============ 配置加载 ============
def load_yaml(name: str) -> dict:
    with open(CONFIG_DIR / name, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# ============ 核心数据结构 ============
AccessLevel = Literal["full", "headline", "rss", "blocked"]


@dataclass
class RawItem:
    """采集到的原始条目"""
    id: str
    source: str
    lang: str
    title: str
    url: str
    published: str                    # ISO8601
    access: AccessLevel
    mode: str                          # rss | html | pdf
    content: str = ""                  # full 才有正文
    summary: str = ""                  # rss/headline 的摘要
    weight: float = 1.0
    topics: list[str] = field(default_factory=list)
    fetch_time: str = ""

    @property
    def age_hours(self) -> float:
        """
        距抓取时刻的小时数。
        日期未知时返回 -1（哨兵值，表示"未知"，不是"很旧"）。

        ⚠️ 早期实现有两种误导：
        - 列表页条目一律填抓取时刻 → 几个月前的旧文看起来像刚发布
        - 未知时返回 999 → 未知被当成"非常旧"而误杀
        未知就是未知，要能被上层识别并区别对待。
        """
        if not self.published:
            return -1.0
        try:
            dt = datetime.fromisoformat(self.published)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - dt).total_seconds() / 3600
        except Exception:
            return -1.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "RawItem":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class ScoredTopic:
    """LLM 评分后的选题"""
    id: str
    title: str
    url: str
    score: float
    dims: dict[str, float] = field(default_factory=dict)
    flags: list[str] = field(default_factory=list)
    confidence: float = 0.0
    reasoning: str = ""
    article_type: str = "event_report"     # event_report|landscape_review|historical_compare
    verify_todo: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)   # 同题多个来源
    content: str = ""                              # 可用正文素材
    scored_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ============ 工具 ============
def make_id(title: str, url: str = "") -> str:
    raw = f"{title.strip().lower()}|{url.split('?')[0]}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def clean_text(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = s.replace("&nbsp;", " ").replace("&amp;", "&").replace("&quot;", '"')
    s = s.replace("&#39;", "'").replace("&lt;", "<").replace("&gt;", ">")
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def today_cst() -> str:
    return datetime.now(CST).strftime("%Y-%m-%d")


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default