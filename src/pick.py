"""
选题模块：从评分结果里挑出当天要写的 2-3 篇。

规则（来自实战判断）：
1. >= must_publish 必选
2. 同一 cluster_key（同靶点）超过 N 条 → 合并成一篇综述，不重复写
3. 每天至少 1 篇非纯新闻（综述或历史对比），否则全是快报太单薄
4. 分数接近时，优先选有正文素材的（full > rss > headline）
5. 置信度过低的不选，宁缺毋滥
"""
from __future__ import annotations

import sys
from collections import defaultdict

from common import (ROOT, SCORED_DIR, load_yaml, read_json, write_json,
                    today_cst)

POSTS_DIR = ROOT / "posts"


def _has_body(s: dict) -> bool:
    return len(s.get("content") or "") > 500


def _published_leads(date: str) -> set[str]:
    """今天已经成稿的 lead_id 前 8 位（compose 的文件名里带）"""
    import re
    d = POSTS_DIR / date[:4] / date[5:7] / date[8:10]
    if not d.exists():
        return set()
    out = set()
    for p in d.glob("*.html"):
        m = re.search(r"-([0-9a-f]{8})\.html$", p.name)
        if m:
            out.add(m.group(1))
    return out


def _evidence(s: dict) -> int:
    """素材完整度打分，用于同分时的 tie-break"""
    n = 0
    if _has_body(s):
        n += 3
    if len(s.get("sources") or []) > 1:
        n += 2          # 多家媒体交叉印证
    if s.get("access") == "full":
        n += 1
    return n


def run(date: str | None = None) -> dict:
    date = date or today_cst()
    cfg = load_yaml("scoring.yaml")["score"]

    scored = read_json(SCORED_DIR / f"{date}.json", [])
    if not scored:
        print(f"[select] 没有评分数据: {SCORED_DIR}/{date}.json")
        return {}

    keep_thr = cfg.get("keep", 7.5)
    pub_thr = cfg.get("must_publish", 8.5)
    min_conf = cfg.get("min_confidence", 0.5)
    dmax = cfg.get("daily_max", 3)
    cluster_max = cfg.get("same_target_cluster_max", 3)

    pool = [s for s in scored
            if s.get("score", 0) >= keep_thr
            and s.get("confidence", 0) >= min_conf]

    # 排除【今天已经成稿】的条目。
    # 否则它们会白占 daily_max 的名额：compose 会跳过（文件已存在），
    # 结果本该出 3 篇的一天只出 2 篇。daily_max 的含义是"3 篇新稿"，
    # 不是"3 个名额（含重复）"。
    posted = _published_leads(date)
    if posted:
        before = len(pool)
        pool = [s for s in pool if s.get("id", "")[:8] not in posted]
        if before != len(pool):
            print(f"[select] 剔除今日已成稿 {before - len(pool)} 条")

    print(f"[select] 候选池 {len(pool)} 条（>= {keep_thr} 分且置信度 >= {min_conf}）")

    must = [s for s in pool if s.get("score", 0) >= pub_thr]

    # ---- 按 cluster 分组 ----
    clusters: dict[str, list[dict]] = defaultdict(list)
    singles: list[dict] = []
    for s in pool:
        ck = (s.get("cluster_key") or "").strip()
        if ck:
            clusters[ck].append(s)
        else:
            singles.append(s)

    plan: list[dict] = []

    # 1) 多条同 cluster → 合并成综述；单条 cluster 按单篇处理
    consumed_singles: set[str] = set()
    for ck, group in clusters.items():
        if len(group) >= 2:
            group.sort(key=lambda x: x.get("score", 0), reverse=True)
            lead = group[0]
            plan.append({
                "type": "landscape_review",
                "cluster_key": ck,
                "lead_id": lead["id"],
                "title": lead.get("title", ""),
                "score": lead.get("score", 0),
                "member_ids": [g["id"] for g in group[:cluster_max]],
                "members": [
                    {"title": g.get("title"), "url": g.get("url"),
                     "score": g.get("score"), "reasoning": g.get("reasoning")}
                    for g in group[:cluster_max]
                ],
                "reasoning": lead.get("reasoning", ""),
            })
        else:
            # 单条簇：进单篇池，但保留 cluster_key 供后续参考
            s = group[0]
            consumed_singles.add(s["id"])
            singles.append(s)

    # 2) 单条高分（含无簇与单簇）
    singles.sort(key=lambda x: (x.get("score", 0), _evidence(x)), reverse=True)
    for s in singles:
        if len(plan) >= dmax:
            break
        plan.append({
            "type": s.get("article_type", "event_report"),
            "cluster_key": None,
            "lead_id": s["id"],
            "title": s.get("title", ""),
            "score": s.get("score", 0),
            "member_ids": [s["id"]],
            "members": [{"title": s.get("title"), "url": s.get("url"),
                         "score": s.get("score"), "reasoning": s.get("reasoning")}],
            "reasoning": s.get("reasoning", ""),
            "verify_todo": s.get("verify_todo", []),
            "flags": s.get("flags", []),
        })

    # 3) 保证至少 1 篇非纯新闻
    if plan and not any(p["type"] != "event_report" for p in plan):
        # 从备选池里找一个历史对比型的
        alt = next((s for s in pool
                    if s.get("article_type") == "historical_compare"), None)
        if alt and len(plan) < dmax:
            plan.append({
                "type": "historical_compare",
                "cluster_key": None,
                "lead_id": alt["id"],
                "title": alt.get("title", ""),
                "score": alt.get("score", 0),
                "member_ids": [alt["id"]],
                "members": [{"title": alt.get("title"), "url": alt.get("url"),
                             "score": alt.get("score")}],
                "reasoning": alt.get("reasoning", ""),
            })

    plan = plan[:dmax]

    out = {"date": date, "count": len(plan), "plan": plan}
    write_json(SCORED_DIR / f"{date}.plan.json", out)

    print(f"\n[select] 选定 {len(plan)} 篇")
    for i, p in enumerate(plan, 1):
        print(f"  {i}. [{p['type']}] {p['score']}分  {p['title'][:56]}")
        if len(p["member_ids"]) > 1:
            print(f"     └ 合并 {len(p['member_ids'])} 条同簇交易")
    return out


if __name__ == "__main__":
    # Windows 控制台默认 GBK，输出特殊符号会崩
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    run(sys.argv[1] if len(sys.argv) > 1 else None)