"""
采集模块：RSS / HTML / PDF 三通道。

设计要点（来自实测教训）：
1. 每个源声明 access_level，抓不到时自动降级而不是丢弃
2. blocked 的源不浪费时间重试，只记录"有此报道"的事实
3. CloudFront 403 的源走 RSS 绕开
4. 单源失败不影响整体，用 try/except 隔离
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
from datetime import datetime, timezone
from urllib.parse import urljoin

import feedparser
import httpx
try:                                  # selectolax >= 1.0 用 lexbor 后端
    from selectolax.lexbor import LexborHTMLParser as HTMLParser
except ImportError:                 # 旧版回退
    from selectolax.parser import HTMLParser

from common import (RawItem, clean_text, ensure_dirs, load_yaml, make_id,
                    now_iso, RAW_DIR, DATA_DIR, write_json, today_cst)
from noise import filter_items
from deepen import looks_important
from search_backend import get_search_fn

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

TIMEOUT = httpx.Timeout(20.0, connect=10.0)
HEADERS = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8"}


# ============ 底层抓取 ============
async def _get(client: httpx.AsyncClient, url: str) -> httpx.Response | None:
    try:
        r = await client.get(url, headers=HEADERS, follow_redirects=True)
        if r.status_code == 200:
            return r
        # 403/406 等 → 记日志但不抛错，让上层决定
        return None
    except Exception as e:
        print(f"  [fetch] {url} -> {type(e).__name__}")
        return None


# ============ RSS 通道 ============
async def fetch_rss(client: httpx.AsyncClient, src: dict) -> list[RawItem]:
    r = await _get(client, src["url"])
    if not r:
        return []
    feed = feedparser.parse(r.content)
    out: list[RawItem] = []
    for e in feed.entries[:40]:
        title = clean_text(getattr(e, "title", ""))
        if not title:
            continue
        published = getattr(e, "published", "") or getattr(e, "updated", "")
        try:
            dt = datetime(*e.published_parsed[:6], tzinfo=timezone.utc).isoformat()
        except Exception:
            dt = now_iso()
        out.append(RawItem(
            id=make_id(title, getattr(e, "link", "")),
            source=src["id"], lang=src["lang"],
            title=title, url=getattr(e, "link", ""),
            published=dt, access="rss", mode="rss",
            summary=clean_text(getattr(e, "summary", ""))[:1500],
            weight=src.get("weight", 1.0),
            topics=src.get("topics", []),
            fetch_time=now_iso(),
        ))
    return out


# ============ HTML 通道 ============
# 各站点的文章链接提取规则（按 access 分级）
LIST_PATTERNS = {
    "biopharmadive_deals": {"base": "https://www.biopharmadive.com",
                       "item": "a[href*='/news/']"},
    "biopharmadive_home":  {"base": "https://www.biopharmadive.com",
                       "item": "a[href*='/news/']"},
    "bioworld_deals":      {"base": "https://www.bioworld.com",
                       "item": "a[href*='/articles/']"},
    "bioworld_asia":       {"base": "https://www.bioworld.com",
                       "item": "a[href*='/articles/']"},
    "stat_biotech":        {"base": "https://www.statnews.com",
                       "item": "a[href*='/2026/']"},
    "stat_pharma":         {"base": "https://www.statnews.com",
                       "item": "a[href*='/2026/']"},
    "phirda_industry":     {"base": "https://www.phirda.com",
                       "item": "a[href*='/artilce_']"},
    "pharnexcloud":        {"base": "https://www.pharnexcloud.com",
                       "item": "a[href*='/zixun/']"},
    "stcn_medicine":       {"base": "https://stcn.com",
                       "item": "a[href*='/article/detail/']"},
    "cnstock_health":      {"base": "https://www.cnstock.com",
                       "item": "a[href*='commonDetail/']"},
    "nbdd_cn":             {"base": "https://www.nbd.com.cn",
                       "item": "a[href*='/articles/']"},
    "pharmcube":           {"base": "https://bydrug.pharmcube.com",
                       "item": "a[href*='/news/detail/']"},
    # ⚠️ PharmExec 的文章路径是 /view/<slug>，不是 /news/。
    # 曾误写成 /news/ 导致判定为"数据中心 IP 被拦"，实际是选择器错误。
    "pharmexec_partnership": {"base": "https://www.pharmexec.com",
                       "item": "a[href*='/view/']"},
    "pharmexec_irlp":      {"base": "https://www.pharmexec.com",
                       "item": "a[href*='/view/']"},
}

# 标题里粘连的日期前缀（PharmExec 这类站点的列表页常见：
# "October 5th 2026Roundup: CSL & Alentis..."）。
# 不清理会污染标题、影响去重相似度。
LEADING_DATE = re.compile(
    r"^\s*(?:"
    r"(?:January|February|March|April|May|June|July|August|September|"
    r"October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|"
    r"Oct|Nov|Dec)\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}"
    r"|\d{1,2}\s+(?:January|February|March|April|May|June|July|August|"
    r"September|October|November|December)\s+\d{4}"
    r"|\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?"
    r")\s*[·\-—|:：]?\s*",
    re.I,
)


def clean_title(t: str) -> str:
    """去掉标题开头的日期，并压缩空白"""
    s = re.sub(r"\s+", " ", t or "").strip()
    # 最多剥两层（有些标题连写两个日期）
    for _ in range(2):
        new = LEADING_DATE.sub("", s)
        if new == s:
            break
        s = new.strip()
    return s or t


RELEVANT = re.compile(
    r"\b(deal|licens|deal(?:s|ing)?|acqui|merger|partner|collaborat|option|"
    r"upfront|milestone|trial|phase [123]|approv|FDA|NMPA|royalt|pipeline|"
    r"spin-?off|in-?licens|out-?licens|asset)\b"
    r"|交易|授权|收购|合作|许可|里程碑|临床|获批|首付款|上市申请|三期|研发|"
    r"引进|对外授权|共同开发|独家|权益|管线|适应症|新药|创新药|专利|"
    r"License-out|License-in",
    re.I,
)


async def fetch_html(client: httpx.AsyncClient, src: dict) -> list[RawItem]:
    r = await _get(client, src["url"])
    if not r:
        return []
    tree = HTMLParser(r.text)
    cfg = LIST_PATTERNS.get(src["id"])
    if not cfg:
        # 兜底：抓所有 h2/h3 里的链接
        nodes = tree.css("h2 a, h3 a")
        base = src["url"]
    else:
        nodes = tree.css(cfg["item"])
        base = cfg["base"]

    seen: set[str] = set()
    out: list[RawItem] = []
    for n in nodes[:120]:
        href = (n.attributes.get("href") or "").strip()
        if not href or href.startswith("#") or href.startswith("javascript"):
            continue
        url = urljoin(base, href)
        title = clean_title(clean_text(n.text()))
        if len(title) < 12:
            continue
        if url in seen:
            continue
        seen.add(url)
        if not RELEVANT.search(title):
            continue
        out.append(RawItem(
            id=make_id(title, url), source=src["id"], lang=src["lang"],
            title=title, url=url, published=now_iso(),
            access=src.get("access", "headline"), mode="html",
            weight=src.get("weight", 1.0),
            topics=src.get("topics", []), fetch_time=now_iso(),
        ))
    return out


# ============ 正文抓取（仅 access: full）===========
async def fetch_article(client: httpx.AsyncClient, item: RawItem) -> RawItem:
    if item.access != "full" or item.mode != "html":
        return item
    r = await _get(client, item.url)
    if not r:
        item.access = "headline"          # 降级
        return item
    tree = HTMLParser(r.text)
    # 正文容器候选
    best, best_len = "", 0
    for sel in ["article", "div.article-body", "div#article-body",
                "div.content", "main", "div.article__content"]:
        for node in tree.css(sel)[:1]:
            t = clean_text(node.text())
            if len(t) > best_len:
                best, best_len = t, len(t)
    if best_len > 200:
        item.content = best[:12000]
    else:
        item.access = "headline"
    return item


# ============ 主流程 ============
async def _run_one(client: httpx.AsyncClient, src: dict) -> list[RawItem]:
    # blocked：站点确认封爬（Cloudflare 403 等）
    # disabled：源无有效产出（地址失效/内容不对口），停用以免浪费请求
    if src.get("access") in ("blocked", "disabled"):
        print(f"  [skip] {src['id']} ({src['access']})")
        return []
    # search 模式依赖外部检索，本环境无法直接抓，留钩子给 Actions 侧实现
    if src.get("mode") == "search" or not src.get("url"):
        print(f"  [skip] {src['id']} (search fallback，Actions 侧处理)")
        return []
    try:
        if src["mode"] == "rss":
            items = await fetch_rss(client, src)
        else:
            items = await fetch_html(client, src)
        print(f"  [ok]   {src['id']}: {len(items)} 条")
        return items
    except Exception as e:
        print(f"  [err]  {src['id']}: {type(e).__name__}: {e}")
        return []


async def run(date: str | None = None) -> list[RawItem]:
    date = date or today_cst()
    ensure_dirs()
    cfg = load_yaml("sources.yaml")

    print(f"\n=== 采集 {date} ===")
    tracker_insights: dict = {}
    async with httpx.AsyncClient(timeout=TIMEOUT, http2=False) as client:
        results = await asyncio.gather(
            *[_run_one(client, s) for s in cfg["sources"]],
            return_exceptions=True
        )

        items: list[RawItem] = []
        for r in results:
            if isinstance(r, list):
                items.extend(r)

        # 噪声过滤（综合财经源收紧，医药垂直源放宽）
        items, dropped = filter_items(items)
        print(f"\n=== 噪声过滤：{len(items)+len(dropped)} -> {len(items)} 条"
              f"（丢弃 {len(dropped)}）===")

        # 抓正文：按源配额，保证中文源也有素材
        quota = {"biopharmadive_deals": 6, "pharnexcloud": 6,
                 "phirda_industry": 4, "stcn_medicine": 3,
                 "nbdd_cn": 2, "pharmcube": 2}
        full_items: list[RawItem] = []
        for sid, n in quota.items():
            full_items.extend(
                [i for i in items if i.access == "full"
                 and i.source == sid][:n])

        if full_items:
            print(f"\n=== 抓正文 {len(full_items)} 篇 ===")
            enriched = await asyncio.gather(
                *[fetch_article(client, i) for i in full_items],
                return_exceptions=True
            )
            ok = 0
            for i, e in zip(full_items, enriched):
                if isinstance(e, RawItem) and e.content:
                    i.content = e.content
                    ok += 1
                else:
                    i.access = "headline"
            print(f"  正文成功 {ok}/{len(full_items)}")

    # ---- 结构化追踪数据库（BioPharma Dive 中国授权交易库）----
    # 追踪数据库（必须在同一 client 上跑，走 async）
        try:
            from trackers import run_all as tracker_run
            titems, insights = await tracker_run()
            if titems:
                print(f"\n=== 追踪数据库 {len(titems)} 笔结构化交易 ===")
                have = {i.title[:60].lower() for i in items}
                added = 0
                for t in titems:
                    k = t.title[:60].lower()
                    if k not in have:
                        items.append(t)
                        have.add(k)
                        added += 1
                print(f"  新增 {added} 笔（去重后）")
                if insights:
                    tracker_insights.clear()
                    tracker_insights.update(insights)
        except Exception as e:
            print(f"  追踪数据库跳过: {type(e).__name__}: {e}")

    # ---- 第二轮：给"重要但没正文"的条目搜一手材料 ----
    if os.getenv("SEARCH_BACKEND"):
        print("\n=== 深度检索一手材料 ===")
        try:
            from deepen import run as deepen_run
            items = deepen_run(items, get_search_fn(), max_items=8)
        except Exception as e:
            print(f"  深度检索跳过: {type(e).__name__}: {e}")
    else:
        cands = [i for i in items
                 if len(i.content or "") <= 800
                 and looks_important(i.title)[0]]
        print(f"\n[提示] {len(cands)} 条无正文但信号强，"
              f"配置 SEARCH_BACKEND 后可自动搜一手材料")

    write_json(RAW_DIR / f"{date}.json", [i.to_dict() for i in items])
    print(f"\n共 {len(items)} 条 -> data/raw/{date}.json")

    if tracker_insights:
        write_json(DATA_DIR / "tracker_insights.json", tracker_insights)
        print(f"  追踪页洞察 -> data/tracker_insights.json")

    # 统计
    by_access: dict[str, int] = {}
    for i in items:
        by_access[i.access] = by_access.get(i.access, 0) + 1
    print("  access 分布:", by_access)
    return items


if __name__ == "__main__":
    # Windows 控制台默认 GBK，输出特殊符号会崩
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    d = sys.argv[1] if len(sys.argv) > 1 else None
    asyncio.run(run(d))