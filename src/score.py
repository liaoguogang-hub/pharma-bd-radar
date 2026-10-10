"""
LLM 评分模块。

核心价值：把人工分析的判断规则编码进 prompt。
这些规则来自实战踩过的坑：

1. 【口径分层】潜在总额 ≠ 首付 ≠ 到账。185 亿美元里首付只有 12 亿。
   评分必须基于首付，不是总额。
2. 【关联交易检测】买卖双方有股权关系时，溢价不代表市场共识。
   迪哲×AZ 首付占比 40% 看着漂亮，但 AZ 持股 23.42%，是体系内变现。
3. 【"未找到"≠"不存在"】查不到就填 verify_todo，不要下否定结论。
   早期把"PDF 读不了"当"不存在"，导致漏掉几十项 III 期资产。
4. 【信息增量】中文媒体已充分报道、英文媒体未跟进的，降权。
5. 【排除纯融资】IPO/定增不是 BD。
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from typing import Any

from openai import OpenAI

from common import (RawItem, SCORED_DIR, RAW_DIR, load_yaml, read_json,
                    write_json, today_cst)


SYSTEM_PROMPT = """你是医药 BD（商务拓展/授权交易）领域的资深分析师。
你的任务是给新闻条目打分，识别出真正值得深度报道的 BD 热点。

# 评分维度（满分 10 分）

## 1. 金额量级 (0-2.5)
- 首付 >5 亿美元 = 2.5
- 首付 >1 亿美元 = 2.0
- 首付 >1000 万美元 = 1.2
- 首付 <500 万美元 = 0.3
⚠️ **必须基于【首付】评分，不是潜在总额。** 185 亿美元的总额里首付往往只有 12 亿。
   里程碑和销售提成是附带的，不是买方的即时承诺。

## 2. 交易阶段 (0-2.0)
- III 期数据读出 / NDA 受理 = 2.0
- II 期阳性数据 = 1.5
- I 期 = 0.8
- 临床前 = 0.5

## 3. 首创性 (0-2.0)
- 全球首个靶点或机制 = 2.0
- 全球首个适应症 = 1.6
- 国产首个 = 1.2
- me-better / 跟随 = 0.4

## 4. 公司层级 (0-1.5)
- MNC 主导 = 1.5
- 大型药企对大型药企 = 1.2
- 二线 biotech = 0.7

## 5. 外溢影响 (0-2.0)
- 改变赛道竞争格局 = 2.0
- 单一公司重大进展 = 1.3
- 常规交易 = 0.6

# 必须执行的核查（历史教训，务必遵守）

**① 关联交易检测**
若买卖双方存在股权关系（一方持有另一方股份，或有共同大股东），
必须在 flags 里标 "related_party"，并在 reasoning 里说明。
这类交易的溢价不代表市场共识，不能按普通交易打分。

**② 口径分层**
涉及金额时，reasoning 里必须写明是"首付""里程碑上限""总额"还是"传闻"。

**③ "未找到" ≠ "不存在"**
如果某项信息（如 NDA 状态、临床阶段）无法从素材确认，
放进 verify_todo，**不要下否定结论**。

**④ 信息增量**
若该交易中文媒体已充分报道而英文媒体未跟进，降低外溢影响分并说明。
反之亦然——英文媒体先报而中文未跟进的，适度加权。

**⑤ 排除纯融资**
IPO、定增、Series A/B 不是 BD，直接给 0 分。

**⑥ 识别综述型条目**
若一天内同靶点/同领域有多笔交易，标记 cluster_key 相同的条目，
score 略降但保留——这类应该合并成一篇 landscape_review 综述。

**⑦ 时效性（重要，容易踩坑）**
每条都带 `published` 和 `age_days` 字段：
- `age_days` 超过 7 天：这不是新闻，降外溢影响分，并在 reasoning 中说明"非近日"
- `age_days` 超过 14 天：直接降 2 分以上
- `age_days` 为「未知」：**不要假设它是新闻，也不要假设它旧**。
  按内容本身评分，但在 reasoning 中标注"发布日期未知"
- ⚠️ 媒体转载旧闻是常见现象。一篇讲「去年那笔交易」的文章，
  讲的是历史，不是今天发生的事。不要把旧交易当成新交易打分。

# 输出格式（严格 JSON，不要任何解释文字）

⚠️ **只输出一个 JSON 数组**。不要输出思考过程、不要解释、不要 markdown 代码块包裹。
第一个字符必须是 `[`，最后一个必须是 `]`。

[{
  "id": "条目id",
  "score": 8.7,
  "dims": {"amount": 2.0, "stage": 2.0, "first_mover": 1.6, "tier": 1.5, "impact": 1.6},
  "flags": ["related_party"],
  "cluster_key": "b7h3-adc",
  "confidence": 0.85,
  "reasoning": "首付12亿美元、全球首个口服GLP-1、诺和诺德亲自下场，60字以内",
  "article_type": "event_report",
  "verify_todo": ["需查港交所公告确认首付到账金额"]
}]

article_type 取值：
- event_report：单笔重大交易深挖
- landscape_review：同靶点/同领域多笔，适合做综述
- historical_compare：有明确历史参照系（如"继XX之后"）

score <7.5 的条目也要返回，但会被 select 阶段丢弃——
这样做是为了让 LLM 看到全量信息，避免它误判某条低分条目其实重要。
"""


def _client(cfg: dict, all_providers: dict) -> tuple[OpenAI, dict]:
    """
    按配置创建客户端，自动选择 provider。
    all_providers 来自 scoring.yaml 顶层的 providers 表。
    返回 (client, 生效参数)，生效参数可能被 providers 表覆盖。
    """
    name = cfg.get("provider", "deepseek")
    provs = all_providers or {}

    p = provs.get(name)
    if p:
        base_url = p.get("base_url")
        model = p.get("model")
        max_tokens = p.get("max_tokens", cfg.get("max_tokens", 8000))
        json_mode = p.get("json_mode", False)
        env_names = [p.get("env_key", ""), "LLM_API_KEY", "OPENAI_API_KEY"]
    else:
        base_url = cfg.get("base_url")
        model = cfg.get("model")
        max_tokens = cfg.get("max_tokens", 8000)
        json_mode = cfg.get("json_mode", False)
        env_names = ["LLM_API_KEY", "DEEPSEEK_API_KEY", "MINIMAX_API_KEY",
                     "OPENAI_API_KEY"]

    api_key = None
    for env in env_names:
        if env and os.getenv(env):
            api_key = os.getenv(env)
            break
    if not api_key:
        raise RuntimeError(
            f"缺少 API key。请设置 LLM_API_KEY（provider={name}）")

    eff = {"model": model, "max_tokens": max_tokens, "json_mode": json_mode,
           "temperature": cfg.get("temperature", 0.3),
           "base_url": base_url, "provider": name}

    print(f"[llm] provider={name} model={model}")
    print(f"     base_url={base_url} json_mode={json_mode}")
    return OpenAI(api_key=api_key, base_url=base_url,
                  timeout=180.0, max_retries=2), eff


def _strip_think(text: str) -> str:
    """
    MiniMax 等模型默认开启思考，内容里会混入 <think>...</think>。
    必须剥掉，否则 JSON 解析失败。
    """
    if not text:
        return ""
    # 情况1：完整的 <think>...</think> 前缀
    text = re.sub(r"^\s*<think>.*?</think>\s*", "", text, flags=re.S)
    # 情况2：未闭合的截断式思考（max_tokens 用尽时常见）
    if "</think>" not in text and "<think>" in text:
        text = text.split("<think>", 1)[1]
        if "</think>" in text:
            text = text.split("</think>", 1)[1]
    # 情况3：残留的空标签
    text = text.replace("<think>", "").replace("</think>", "")
    return text.strip()


def _build_items(items: list[RawItem]) -> list[dict[str, Any]]:
    out = []
    for it in items:
        out.append({
            "id": it.id,
            "title": it.title,
            "source": it.source,
            "lang": it.lang,
            "url": it.url,
            "published": it.published or "未知",
            "age_days": (round(it.age_hours / 24, 1)
                         if it.age_hours >= 0 else "未知"),
            "access": it.access,
            "summary": (it.summary or it.content)[:600],
            "multi_source": [t for t in it.topics if t.startswith("also:")],
        })
    return out


def _parse(text: str) -> list[dict]:
    """LLM 返回可能带 markdown 包裹或前后废话，做健壮解析"""
    if not text:
        return []
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)

    # 优先整段 JSON 数组
    i, j = text.find("["), text.rfind("]")
    if i >= 0 and j > i:
        try:
            return json.loads(text[i:j + 1])
        except Exception:
            pass

    # 退化：逐个捞对象（支持嵌套括号）
    objs, depth, start, in_str, esc = [], 0, -1, False, False
    for k, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = k
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                try:
                    o = json.loads(text[start:k + 1])
                    if isinstance(o, dict) and "id" in o:
                        objs.append(o)
                except Exception:
                    pass
                start = -1
    return objs


def _prefilter(items: list[RawItem], cfg: dict) -> list[RawItem]:
    """规则层预筛：纯融资直接丢弃，过期的降权"""
    excl = cfg.get("exclude_keywords", {}) or {}
    bad = [w.lower() for w in excl.get("pure_financing", [])]
    out = []
    for it in items:
        t = it.title.lower()
        if any(w in t for w in bad):
            continue
        out.append(it)
    return out


def _call_llm(client: OpenAI, eff: dict, system: str, user: str,
              retries: int = 2) -> str:
    """统一调用入口，自动处理 json_mode 降级和 think 标签"""
    kwargs = {
        "model": eff["model"],
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": eff["temperature"],
        "max_tokens": eff["max_tokens"],
    }
    if eff.get("json_mode"):
        kwargs["response_format"] = {"type": "json_object"}

    last_err = None
    for attempt in range(retries + 1):
        try:
            r = client.chat.completions.create(**kwargs)
            content = r.choices[0].message.content or ""
            # 若 json_mode 生效但内容被 think 污染，退回普通模式
            if not content.strip() and eff.get("json_mode"):
                kwargs.pop("response_format", None)
                eff["json_mode"] = False
                continue
            return _strip_think(content)
        except Exception as e:
            msg = str(e)
            # 不支持 response_format 就降级
            if "response_format" in msg or "json_object" in msg.lower():
                kwargs.pop("response_format", None)
                eff["json_mode"] = False
                last_err = e
                continue
            last_err = e
            if attempt < retries:
                time.sleep(2 ** attempt)
    if last_err:
        raise last_err
    return ""


def score_items(items: list[RawItem], cfg: dict, client: OpenAI,
                eff: dict) -> list[dict]:
    batch = cfg.get("batch_size", 30)
    retries = cfg.get("max_retries", 2)
    results: list[dict] = []

    pre = _prefilter(items, cfg)
    print(f"[score] 预筛后 {len(pre)} 条（原始 {len(items)}）")

    for i in range(0, len(pre), batch):
        chunk = pre[i:i + batch]
        user = f"请为以下 {len(chunk)} 条医药 BD 新闻打分。\n" \
               f"只输出 JSON 数组，不要任何其他文字。\n\n" + \
               json.dumps(_build_items(chunk), ensure_ascii=False, indent=1)
        try:
            txt = _call_llm(client, eff, SYSTEM_PROMPT, user, retries)
            parsed = _parse(txt)
            results.extend(parsed)
            print(f"  [batch {i//batch + 1}] 返回 {len(txt)} 字符，"
                  f"解析 {len(parsed)}/{len(chunk)}")
            time.sleep(1.0)
        except Exception as e:
            print(f"  [batch {i//batch + 1}] 失败: {type(e).__name__}: {e}")
    return results


def run(date: str | None = None) -> list[dict]:
    date = date or today_cst()
    cfg = load_yaml("scoring.yaml")

    path = RAW_DIR / f"{date}.deduped.json"
    raw_items = read_json(path, [])
    if not raw_items:
        raw_items = read_json(RAW_DIR / f"{date}.json", [])
    if not raw_items:
        print(f"[score] 没有待评分数据: {path}")
        return []

    items = [RawItem.from_dict(d) for d in raw_items]
    client, eff = _client(cfg["llm"], cfg.get("providers", {}))
    scored = score_items(items, cfg, client, eff)

    # 回填原始信息
    by_id = {i.id: i for i in items}
    for s in scored:
        src = by_id.get(s.get("id", ""))
        if src:
            s["title"] = src.title
            s["url"] = src.url
            s["sources"] = [src.source] + [
                t.split(":", 1)[1] for t in src.topics if t.startswith("also:")]
            s["content"] = src.content
            s["lang"] = src.lang
            s["scored_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    scored.sort(key=lambda x: x.get("score", 0), reverse=True)
    write_json(SCORED_DIR / f"{date}.json", scored)

    print(f"\n[score] 完成 {len(scored)} 条 -> data/scored/{date}.json")
    if scored:
        thr = cfg["score"]["must_publish"]
        hot = [s for s in scored if s.get("score", 0) >= thr]
        print(f"  >= {thr} 分：{len(hot)} 条")
        for s in scored[:8]:
            print(f"    {s.get('score', 0):>4} | {s.get('title', '')[:62]}")
    return scored


if __name__ == "__main__":
    # Windows 控制台默认 GBK，输出特殊符号会崩
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    run(sys.argv[1] if len(sys.argv) > 1 else None)