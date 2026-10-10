"""
追踪页采集器（tracker 类型）。

背景：
BioPharma Dive 维护了几个"持续更新数据库"页面：
- Drugs from China are reshaping biotech（中国对外授权交易库）
- Biotech M&A is accelerating（全球 BD 追踪）

这些不是单条新闻，而是累积数据库。实测页面内有：
- 数据库表格（公司/金额/靶点/阶段）
- 关键统计（"2025年三分之一行业授权支出涉及中国资产" — Jefferies 数据）
- 图表结论（"复杂抗体和小分子最热门""肿瘤和自免药最受关注"
  "交易以早期候选药为主""交易金额规模在扩大"）

这些结构化信息对写"赛道综述"极有价值，比单条新闻强得多。
所以单独做 tracker 采集器，把数据库行解析成结构化条目。
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

import httpx
try:
    from selectolax.lexbor import LexborHTMLParser as HTMLParser
except ImportError:  # pragma: no cover
    from selectolax.parser import HTMLParser

from common import RawItem, clean_text, make_id, now_iso

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0 Safari/537.36"}

TRACKERS = {
    "biopharmadive_china_deals": {
        "name": "BioPharma Dive - 中国授权交易数据库",
        "url": "https://www.biopharmadive.com/news/"
               "china-biotech-drug-licensing-deals-pipeline/758283/",
        "focus": "中国生物医药公司向欧美授权",
        "weight": 1.5,
    },
    "biopharmadive_ma_tracker": {
        "name": "BioPharma Dive - 全球 BD 追踪",
        "url": "https://www.biopharmadive.com/news/"
               "biotech-pharma-deals-merger-acquisitions-tracker/604262/",
        "focus": "全球生物科技并购与授权",
        "weight": 1.2,
    },
}

# 表格里的金额列识别
AMOUNT_RE = re.compile(
    r"\$\s?\d[\d.,]*\s*(?:billion|bn|million|m\b)?|"
    r"[\d.]+\s*(?:亿|万)\s*(?:美元|元)?", re.I)


async def fetch_tracker(client: httpx.AsyncClient,
                        key: str,
                        spec: dict) -> tuple[list[RawItem], dict]:
    """
    抓追踪页，返回 (条目列表, 页面洞察)。
    """
    try:
        r = await client.get(spec["url"], headers=HEADERS,
                             timeout=30, follow_redirects=True)
    except Exception as e:
        print(f"  [tracker err] {key}: {type(e).__name__}")
        return [], {}
    if r.status_code != 200:
        print(f"  [tracker {r.status_code}] {key}")
        return [], {}

    html = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", r.text)
    html = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", html)
    try:
        tree = HTMLParser(html)
    except Exception:
        return [], {}

    # ---------- 1. 解析数据库表格 ----------
    items: list[RawItem] = []
    rows: list[dict] = []

    for table in tree.css("table"):
        heads = [clean_text(th.text())[:24] for th in table.css("th")]
        for tr in table.css("tr")[1:]:
            tds = tr.css("td")
            if not tds:
                continue
            cells = [clean_text(td.text()) for td in tds]
            if not any(cells):
                continue
            # 跳过全空/极短行
            if sum(len(c) for c in cells) < 20:
                continue
            row = {}
            for i, c in enumerate(cells):
                key = heads[i] if i < len(heads) else f"col{i}"
                row[key] = c
            rows.append(row)

    # ---------- 2. 表格转条目 ----------
    for row in rows[:80]:
        text = " | ".join(f"{k}={v}" for k, v in row.items() if v)
        title = self._title_from_row(row) or text[:90]
        if len(title) < 10:
            continue
        amt = AMOUNT_RE.search(text)
        items.append(RawItem(
            id=make_id(title, text[:120]),
            source=key, lang="en",
            title=title,
            url=spec["url"],
            published=now_iso(),
            access="full", mode="tracker",
            content=text[:2500],          # 表格行本身就是结构化内容
            summary=text[:600],
            weight=spec["weight"],
            topics=["tracker", "china_deals" if "china" in key else "global"],
            fetch_time=now_iso(),
        ))

    # ---------- 3. 抽取页面洞察 ----------
    insights = {}
    if "china" in key:
        # Jefferies 等第三方统计
        m = re.search(r"([^.]*?one-third[^.]*\.)", clean_text(tree.text()))
        if m:
            insights["jefferies"] = clean_text(m.group(1))[:400]
        # 图表结论
        caps = []
        for h in tree.css("h3, h4"):
            t = clean_text(h.text())
            if 20 < len(t) < 200 and any(k in t.lower() for k in
                    ["deal", "drug", "antibod", "cancer", "immune", "stage",
                     "size", "scope", "licens", "target", "popular"]):
                caps.append(t)
        if caps:
            insights["chart_findings"] = caps[:6]

    # 页面元信息
    pub = re.search(r"Published\s+([A-Z][a-z]+\.?\s+\d{1,2},\s*\d{4})",
                    clean_text(tree.text()))
    upd = re.search(r"Updated\s+([A-Z][a-z]+\.?\s+\d{1,2},\s*\d{4})",
                    clean_text(tree.text()))
    if pub or upd:
        insights["published"] = pub.group(1) if pub else ""
        insights["updated"] = upd.group(1) if upd else ""

    print(f"  [tracker] {key}: {len(items)} 行 / {len(rows)} 表行, "
          f"洞察 {len(insights)} 项")
    return items, insights

    @staticmethod
    def _title_from_row(row: dict) -> str:
        """从表格行拼一个可读标题"""
        # 常见列名：Company / Drug / Target / Buyer / Value
        for k in ("Company", "Chinese company", "Licensor", "Seller",
                  "Drug", "Asset", "Candidate", "Company "):
            for kk, vv in row.items():
                if kk.strip().lower() == k.lower() and vv:
                    return vv[:80]
        vals = [v for v in row.values() if v]
        return " - ".join(vals[:3])[:90] if vals else ""


def _title_from_row(row: dict) -> str:
    for k in ("Company", "Chinese company", "Licensor", "Seller",
              "Drug", "Asset", "Candidate"):
        for kk, vv in row.items():
            if kk.strip().lower() == k.lower() and vv:
                return vv[:80]
    vals = [v for v in row.values() if v]
    return " - ".join(vals[:3])[:90] if vals else ""


# BioPharma Dive 的数据库用 Datawrapper 托管，公开数据端点：
#   https://datawrapper.dwcdn.net/{chart_id}/{version}/dataset.csv
# ⚠️ 必须带版本号，去掉会 404。
DATAWRAPPER_IDS = {
    "biopharmadive_china_deals": [
        ("EaTzb", "7"), ("A9co6", "7"), ("gyy25", "5"),
        ("r57W5", "4"), ("ZTFPS", "1"),
    ],
}


def _parse_amount_m(v: str) -> float | None:
    """把 CSV 里的金额字符串转成数字（单位：百万美元）"""
    if not v:
        return None
    s = str(v).strip().replace("$", "").replace(",", "")
    if s in ("", "-", "NA", "n/a"):
        return None
    try:
        return float(s)
    except ValueError:
        m = re.search(r"(\d+(?:\.\d+)?)", s)
        return float(m.group(1)) if m else None


def _fmt_amount_m(v) -> str:
    """百万美元转可读文本"""
    if v is None:
        return ""
    try:
        x = float(v)
    except Exception:
        return ""
    if x >= 1000:
        return f"${x/1000:.2f}B"
    return f"${x:.0f}M"


def row_to_item(row: dict, source: str, url: str) -> RawItem | None:
    """把数据库一行转成 RawItem"""
    def g(*keys):
        for k in keys:
            for kk, vv in row.items():
                if kk.strip().lower() == k.lower():
                    return (vv or "").strip()
        return ""

    licensor = g("Licensor", "Chinese company")
    licensee = g("Licensee", "Buyer", "Company")
    drug = g("Drug", "Molecule", "Asset")
    target = g("Target", "Targets")
    modality = g("Modality")
    area = g("Therapeutic area")
    phase = g("Phase of development", "Phase")
    upfront = _parse_amount_m(g("Upfront value* ($m)", "Upfront"))
    total = _parse_amount_m(g("Total deal value** ($m)", "Total deal value"))
    date = g("Date of deal")
    link = g("Link to coverage")

    if not licensor and not licensee:
        return None

    # 标题：谁 买了 谁的 什么药
    bits = []
    if licensee:
        bits.append(licensee)
    if licensor and licensor != licensee:
        bits.append(f"从{licensor}")
    if drug:
        bits.append(drug)
    title = " ".join(bits) if bits else f"{licensor or licensee} 交易"
    if total:
        title += f"（{_fmt_amount_m(total)}）"

    content = (
        f"授权方(中国)：{licensor}\n受让方：{licensee}\n"
        f"日期：{date}\n"
        f"首付款：{_fmt_amount_m(upfront) or '未披露'}\n"
        f"总额：{_fmt_amount_m(total) or '未披露'}\n"
        f"药物/靶点：{drug} / {target}\n"
        f"治疗领域：{area}\n模态：{modality}\n阶段：{phase}\n"
        f"报道链接：{link}"
    )

    return RawItem(
        id=make_id(title, f"{licensor}|{licensee}|{drug}"),
        source=source, lang="en",
        title=title[:180],
        url=link or url,
        published=now_iso(),
        access="full", mode="tracker",
        content=content[:2500],
        summary=content[:400],
        weight=1.5,
        topics=["tracker", "china_deals", "structured"],
        fetch_time=now_iso(),
    )


async def fetch_datawrapper(client: httpx.AsyncClient, source: str,
                            chart_id: str, version: str) -> list[RawItem]:
    """从 Datawrapper 拉 CSV 并解析"""
    import csv as _csv
    import io as _io
    url = f"https://datawrapper.dwcdn.net/{chart_id}/{version}/dataset.csv"
    try:
        r = await client.get(url, headers=HEADERS, timeout=30,
                             follow_redirects=True)
    except Exception:
        return []
    if r.status_code != 200 or len(r.text) < 100:
        return []
    try:
        rows = list(_csv.DictReader(_io.StringIO(r.text)))
    except Exception:
        return []
    out = []
    for row in rows:
        it = row_to_item(row, source, url)
        if it:
            out.append(it)
    return out


async def run_all(max_rows: int = 200,
                  client: httpx.AsyncClient | None = None) -> tuple[list[RawItem], dict]:
    """抓取所有追踪页 + Datawrapper 数据库"""
    out: list[RawItem] = []
    insights: dict[str, dict] = {}

    own = client is None
    c = client or httpx.AsyncClient(timeout=30, http2=False)
    try:
        # 1) 结构化数据库
        for source, charts in DATAWRAPPER_IDS.items():
            got = 0
            for cid, ver in charts:
                items = await fetch_datawrapper(c, source, cid, ver)
                if items:
                    out.extend(items)
                    got += len(items)
            print(f"  [dw] {source}: {got} 条")

        # 2) 页面洞察
        for key, spec in TRACKERS.items():
            _, ins = await fetch_tracker(c, key, spec)
            if ins:
                insights[key] = ins
    finally:
        if own:
            await c.aclose()

    # 跨库去重（同一交易可能在多个图表里）
    seen, uniq = set(), []
    for it in out:
        k = (it.title[:60], it.content[:80])
        if k not in seen:
            seen.add(k)
            uniq.append(it)
    if len(uniq) != len(out):
        print(f"  [dw] 跨图表去重 -{len(out)-len(uniq)}")
    return uniq[:max_rows], insights


if __name__ == "__main__":
    import asyncio, json, sys, io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    items, ins = asyncio.run(run_all())
    print(f"\n合计 {len(items)} 条\n")
    for k, v in ins.items():
        print(f"--- {k} ---")
        for kk, vv in v.items():
            if isinstance(vv, list):
                for x in vv:
                    print(f"  · {x[:110]}")
            else:
                print(f"  {kk}: {vv[:200]}")
    print("\n--- 条目前 15---")
    for i, x in enumerate(items[:15], 1):
        print(f"{i:3}. {x.title[:80]}")
        print(f"     {x.content[:130]}")