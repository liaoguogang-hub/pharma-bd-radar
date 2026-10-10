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


def filter_history(items: list[RawItem], days: int = 30) -> list[RawItem]:
    """过滤掉 seen.json 里已报道过的（跨天去重）"""
    seen = read_json(SEEN_FILE, {"ids": [], "titles": []})
    seen_ids = set(seen.get("ids", []))
    seen_titles = [normalize_title(t) for t in seen.get("titles", [])]

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
        print(f"  历史去重：丢弃 {dropped} 条已报道")
    return out


def update_seen(items: list[RawItem], cap: int = 3000) -> None:
    seen = read_json(SEEN_FILE, {"ids": [], "titles": []})
    ids = list(seen.get("ids", []))
    titles = list(seen.get("titles", []))
    for it in items:
        ids.append(it.id)
        titles.append(it.title)
    write_json(SEEN_FILE, {"ids": ids[-cap:], "titles": titles[-cap:]})


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

    items = filter_history(items)
    before = len(items)
    items = dedupe(items, threshold=thr)
    print(f"[dedupe] 去重后 {len(items)} 条（合并 {before - len(items)} 条）")

    update_seen(items)
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