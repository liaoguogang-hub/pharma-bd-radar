"""
去重模块。

两个层级：
1. 完全重复 —— 同 URL 或同 hash
2. 语义重复 —— 不同媒体报同一件事（标题相似度）

用 rapidfuzz 做标题近似，因为同一条交易在不同媒体的标题
往往只差几个词（"Novo to pay Hengrui up to $2.6B for a
once-weekly obesity pill" vs "Novo Nordisk licenses Hengrui
oral GLP-1 for $2.6 billion"）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from rapidfuzz import fuzz
from rapidfuzz import process

from common import (RawItem, RAW_DIR, SCORED_DIR, SEEN_FILE, load_yaml,
                    read_json, write_json, today_cst)


def normalize_title(t: str) -> str:
    """去掉标点、公司后缀等干扰，让相似度更准"""
    t = t.lower()
    t = re.sub(r"\s+[—\-|·].*$", "", t)          # 去掉破折号后的副标题
    t = re.sub(r"\(.*?\)", "", t)                  # 去括号
    t = re.sub(r"[^\w\s一-鿿]", " ", t)       # 去标点（保留中文）
    t = re.sub(r"\b(inc|corp|ltd|plc|co|company|holdings|therapeutics|"
               r"biosciences|biopharma|pharmaceuticals?)\b", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def entity_hint(t: str) -> str:
    """抽取公司名片段，作为辅助判据"""
    caps = re.findall(r"\b[A-Z][a-zA-Z]{3,}\b", t)
    stop = {"The", "This", "That", "With", "From", "After", "What", "Why",
            "How", "Its", "His", "Her", "They", "Their", "Both", "More",
            "Here", "Now", "New", "First", "Two", "Three", "Four", "Five"}
    caps = [c for c in caps if c not in stop]
    return " ".join(caps[:4]).lower()


def dedupe(items: list[RawItem], threshold: int = 82) -> list[RawItem]:
    """
    返回去重后的列表，被合并的来源会记到 keep.sources 里（用 extra_sources 字段）。
    """
    # 按发布时间倒序，保留最新的
    items = sorted(items, key=lambda x: x.published, reverse=True)

    kept: list[RawItem] = []
    norms: list[str] = []
    url_seen: set[str] = set()
    hash_seen: set[str] = set()
    merged: dict[str, list[str]] = {}

    for it in items:
        base = it.url.split("?")[0]
        if it.id in hash_seen or base in url_seen:
            # 完全重复，只记录来源
            for k in kept:
                if k.id == it.id or k.url.split("?")[0] == base:
                    merged.setdefault(k.id, []).append(it.source)
                    break
            continue

        n = normalize_title(it.title)
        dup_of = None
        if n:
            m = process.extractOne(n, norms, scorer=fuzz.token_set_ratio,
                                   score_cutoff=threshold)
            if m:
                dup_of = m[0]

        if dup_of is not None:
            for k in kept:
                if normalize_title(k.title) == dup_of:
                    merged.setdefault(k.id, []).append(it.source)
                    break
        else:
            kept.append(it)
            norms.append(n)
            url_seen.add(base)
            hash_seen.add(it.id)

    # 把合并的来源写回去
    for it in kept:
        extra = merged.get(it.id, [])
        if extra:
            it.topics = list(set(it.topics + [f"also:{s}" for s in extra]))

    return kept


def filter_history(items: list[RawItem], today: str | None = None) -> list[RawItem]:
    """
    跨天去重：只丢弃"已经写成稿"的条目。

    ⚠️ 两个踩过的坑，都在这里：

    【坑一】seen.json 早期不带日期，导致同一天的重跑把当天所有新闻
    都当成"已报道"。工作流每天跑两次，第二次永远产出 0 篇。
    实测：139 条被丢 110 条，只剩 2 条，最终 0 篇成稿。
    修法：条目带日期。

    【坑二】更隐蔽——去重基准用的是"抓过的"而不是"写过的"。
    一天抓 100+ 条但只写 2-3 篇，剩下 97 条明明没写过，
    却被永久标记为"已报道"。次日它们仍在源站首页，直接被过滤，
    于是天天产出 0 篇。
    实测：次日采集 143 条 → 丢弃 105 条 → 只剩 8 条 → 选题 0 篇。
    修法：seen.json 只记录【已发表】的选题（见 record_published），
    其余条目每次照常进入评分，让没入选的高分条目有机会第二天补上。
    """
    today = today or today_cst()
    data = read_json(SEEN_FILE, {})
    entries = _load_entries(data)

    # 已发表选题：不过滤同日的（同日重跑靠 compose 的文件幂等兜住）
    old = [e for e in entries if e.get("date", "") < today]
    seen_ids = {e["id"] for e in old if e.get("id")}
    seen_titles = [normalize_title(e["title"]) for e in old if e.get("title")]

    out, dropped = [], 0
    for it in items:
        if it.id in seen_ids:
            dropped += 1
            continue
        if seen_titles:
            m = process.extractOne(normalize_title(it.title), seen_titles,
                                   scorer=fuzz.token_set_ratio, score_cutoff=90)
            if m:
                dropped += 1
                continue
        out.append(it)

    if dropped:
        print(f"  历史去重：丢弃 {dropped} 条（往日已发表 {len(old)} 篇为基准）")
    else:
        print(f"  历史去重：基准 {len(old)} 篇往日已发表，本轮无丢弃")
    return out


def _load_entries(data: dict) -> list[dict]:
    """
    读 seen 条目。只接受 v3。

    版本历史（每次都是因为语义或格式错了才升版）：
      v1  {"ids":[], "titles":[]}
          无日期。同日重跑会把当天所有新闻当成"已报道"。
      v2  {"entries":[{id,title,date}]}
          有日期了，但记录的是【抓取到的全部条目】，
          导致没写过的高分条目被永久埋掉。
      v3  {"entries":[{id,title,date}]}
          记录【已发表选题】。格式与 v2 相同，语义不同，
          所以按 version 字段严格区分。

    旧版一律丢弃：宁可让最近一轮多评几条，也不能让错误的基准
    继续把好条目挡在外面。
    """
    if not isinstance(data, dict):
        return []
    ver = data.get("version")
    if ver == 3:
        return [e for e in data.get("entries", []) if isinstance(e, dict)]
    if data.get("entries") or data.get("ids") or data.get("titles"):
        print(f"  [seen] 检测到旧版格式 v{ver}，已丢弃"
              f"（语义已变更：只记录已发表选题）")
    return []


def record_published(topics: list[dict], today: str | None = None,
                     cap: int = 2000) -> None:
    """
    记录【已发表】的选题，作为跨天去重的基准。

    ⚠️ 只记录真正写成稿的，不要记录抓取到的全部条目。
    记录全部会把"没写过的高分条目"永久埋掉——见 filter_history 的坑二。

    topics: [{"lead_id":..., "title":...}, ...]
    """
    today = today or today_cst()
    data = read_json(SEEN_FILE, {})
    entries = _load_entries(data)

    known = {e.get("id") for e in entries}
    added = 0
    for t in topics:
        tid = t.get("lead_id") or t.get("id")
        title = t.get("title") or ""
        if not tid or tid in known:
            continue
        entries.append({"id": tid, "title": title, "date": today})
        known.add(tid)
        added += 1

    entries = entries[-cap:]
    write_json(SEEN_FILE, {"version": 3, "entries": entries})
    print(f"  [seen] 记录已发表 {added} 篇（库中共 {len(entries)} 篇）")


def run(date: str | None = None) -> list[RawItem]:
    date = date or today_cst()
    cfg = load_yaml("sources.yaml")
    thr = cfg.get("dedupe", {}).get("threshold", 82)

    raw = read_json(RAW_DIR / f"{date}.json", [])
    if not raw:
        print(f"[dedupe] 没有原始数据: {RAW_DIR}/{date}.json")
        return []

    items = [RawItem.from_dict(d) for d in raw]
    print(f"[dedupe] 输入 {len(items)} 条")

    items = filter_history(items, today=date)
    before = len(items)
    items = dedupe(items, threshold=thr)
    print(f"[dedupe] 去重后 {len(items)} 条（合并 {before - len(items)} 条）")

    # 注意：这里不写 seen.json。
    # seen 记录的是【已发表】选题，由 compose 成功后调用 record_published。
    write_json(RAW_DIR / f"{date}.deduped.json", [i.to_dict() for i in items])
    return items


if __name__ == "__main__":
    # Windows 控制台默认 GBK，输出特殊符号会崩
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    run(sys.argv[1] if len(sys.argv) > 1 else None)