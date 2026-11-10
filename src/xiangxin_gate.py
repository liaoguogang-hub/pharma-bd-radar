"""
象信 AI 前置闸门（Xiangxin pre-filter）。

作用：在昂贵的 LLM 打分之前，用象信先判断「这条到底是不是一笔 BD 交易」，
把明确不是交易的条目拦掉。只做一件事，不做别的。

为什么这么设计（2026-11-10 实测，详见 docs 或对照报告）：
  1. score.py 的 score 把「是不是交易」「值不值得报」「素材够不够」揉在一起，
     导致下游想单独知道「是不是交易」时只能靠 score<=3.0 反推，而这是错的。
  2. 象信只回答一个原子问题：有/没有交易对手方。答案干净、可验证。
  3. 实测拦掉 86.7% 的条目，漏杀 0 条。

边界（重要）：
  - 象信【不】替代 LLM 打分。它抽不出金额、不生成 reasoning。
  - 闸门只拦「高置信度非交易」。低置信度一律放行给 LLM。
  - 象信服务不可用时，闸门整体旁路，绝不阻断生产。
"""
from __future__ import annotations

import os
import sys
from typing import Any

# 置信度门限：只有象信非常确定「不是交易」时才拦。
# 宁可多花钱给 LLM，也不要漏杀一条值得成稿的交易。
BLOCK_CONFIDENCE = 0.80

# 送给象信的摘要最大字符数。象信对长 state 效果下降（SKILL.md §6），
# 且只放判断需要的内容——无关的长文本会降低准确率。
MAX_SUMMARY_CHARS = 800

# 题目定义（口径对齐 score.py 的规则⑧）
Q_DEAL = {
    "type": "choice",
    "instructions": (
        "`标题` 和 `摘要` 描述的事件是否属于一笔 BD 交易，即存在交易对手方"
        "（授权/许可、收购/并购、合作开发、期权）？"
    ),
    "criteria": {
        "是交易": "存在授权、收购、合作或选择权关系，有买方卖方或授权方受让方",
        "非交易": (
            "单纯的临床数据读出、监管获批、学术发表、股价波动、人事变动、"
            "行业综述，或 IPO/定增/Series A/B 等纯融资，无交易对手方"
        ),
    },
}

# 首付披露判定。用来给 LLM 提示「这里到底有没有明确的首付款金额」。
# 2026-11-10 实测：LLM 会把「up to $X 总对价」当成首付
# （EQT 108亿欧元收购 Qiagen 被抽成首付 $11,252,000,000）。
# 象信在这件事上比 LLM 稳，但只能判断「有没有」，抽不出数字——那是 LLM 的活。
Q_UPFRONT = {
    "type": "noul",
    "instructions": (
        "`标题` 和 `摘要` 中是否明确披露了首付款（upfront / 首付款 / 预付款）"
        "的具体金额？"
        "「潜在总额」「里程碑上限」「up to $X」「总交易价值」「企业价值」"
        "都不算首付款。"
    ),
}


def _state(item: Any) -> dict[str, str]:
    """构造象信 state。只放判断需要的内容。"""
    if isinstance(item, dict):
        title = item.get("title") or ""
        body = item.get("summary") or item.get("content") or ""
    else:  # RawItem
        title = getattr(item, "title", "") or ""
        body = getattr(item, "summary", "") or getattr(item, "content", "") or ""
    return {
        "标题": title,
        "摘要": (body or "")[:MAX_SUMMARY_CHARS],
    }


def _has_key() -> bool:
    return bool(os.getenv("XIANGXIN_API_KEY"))


def filter_items(items: list[Any]) -> tuple[list[Any], dict[str, Any]]:
    """
    象信前置闸门。

    返回 (放行的条目, 统计信息)。
    任何异常都会退化为「全部放行」，绝不阻断生产。

    统计信息里的 upfront_hints: {item_id: bool}
    供 score.py 喂给 LLM 当提示，降低「up to $X 被当成首付」的错误。
    """
    stats: dict[str, Any] = {
        "enabled": False,
        "total": len(items),
        "blocked": 0,
        "passed": len(items),
        "error": None,
        "blocked_ids": [],
        "upfront_hints": {},
    }

    if not _has_key():
        stats["error"] = "XIANGXIN_API_KEY 未设置，闸门旁路"
        return items, stats

    try:
        from xiangxin import Choice, XiangxinClient
    except ImportError:
        stats["error"] = "xiangxin-sdk 未安装，闸门旁路"
        return items, stats

    question = Choice(
        instructions=Q_DEAL["instructions"], criteria=Q_DEAL["criteria"]
    )
    upfront_q = {
        "type": "noul",
        "instructions": Q_UPFRONT["instructions"],
    }

    def _item_id(it: Any) -> str:
        if isinstance(it, dict):
            return it.get("id") or ""
        return getattr(it, "id", "") or ""

    try:
        with XiangxinClient() as client:
            passed = []
            for it in items:
                iid = _item_id(it)
                try:
                    resp = client.system_one(
                        state=_state(it),
                        questions={"deal": question, "upfront": upfront_q},
                    )
                    ans = resp.answers["deal"]
                    # 首付提示：只在象信明确说「有」时才给 True，
                    # 置信度不足时不给提示（宁可没提示，不要误导 LLM）
                    up = resp.answers.get("upfront")
                    if up is not None and up.noul >= 0.60:
                        stats["upfront_hints"][iid] = bool(up.noul >= 0.5)
                    if ans.choice == "非交易" and ans.confidence >= BLOCK_CONFIDENCE:
                        stats["blocked"] += 1
                        stats["blocked_ids"].append(iid)
                        continue
                except Exception as e:  # 单条失败不阻断整批
                    # 鉴权失败是全局性的，继续打只会白费 401 请求 —— 整体旁路
                    if type(e).__name__ == "AuthenticationError":
                        stats["error"] = f"AuthenticationError: {e}"
                        print(f"[xiangxin-gate] 鉴权失败，全部放行: {stats['error']}")
                        return items, stats
                    print(f"[xiangxin-gate] 单条失败，放行: {type(e).__name__}: {e}")
                passed.append(it)
    except Exception as e:
        stats["error"] = f"{type(e).__name__}: {e}"
        print(f"[xiangxin-gate] 初始化失败，全部放行: {stats['error']}")
        return items, stats

    stats["enabled"] = True
    stats["passed"] = len(passed)
    return passed, stats


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    demo = [
        {"id": "a", "title": "礼来以 2 亿美元首付款获得诺诚健华 BTK 降解剂全球权益"},
        {"id": "b", "title": "全球首个双周给药 GLP-1R/GIPR 三期临床主要终点达标"},
        {"id": "c", "title": "某生物科技公司完成 Series B 轮融资 8000 万美元"},
    ]
    kept, st = filter_items(demo)
    print(f"总 {st['total']} → 放行 {st['passed']}，拦掉 {st['blocked']}")
    for k in kept:
        print(f"  放行: {k['id']} {k['title'][:40]}")
