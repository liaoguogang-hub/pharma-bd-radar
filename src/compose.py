"""
文章生成模块（内容层）。

架构（实测得出的分工）：
  LLM  → 只写内容和轻量标记，不写任何 HTML 标签
  render → 负责所有标签、CSS、转义

为什么这样分：
让 LLM 一次写"内容 + HTML + CSS"，成功率只有 ~1/3，
失败全在格式层（标签不配平、CSS 丢失、输出截断、返回空）。
拆开后格式错误从架构上不可能发生，模型只需专注内容。
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
from pathlib import Path

from common import (ROOT, SCORED_DIR, RAW_DIR, DATA_DIR, load_yaml,
                    read_json, write_json, today_cst)
from render import (parse_content, render_html, body_chars,
                    structure_report, content_text)
from score import _client, _call_llm, _strip_think

POSTS_DIR = ROOT / "posts"


# ============================================================
# 素材组装
# ============================================================
def build_material(date: str, plan_item: dict) -> str:
    scored = read_json(SCORED_DIR / f"{date}.json", [])
    raw = read_json(RAW_DIR / f"{date}.json", [])
    by_id = {s["id"]: s for s in scored}
    raw_by_id = {r["id"]: r for r in raw}

    ids = plan_item.get("member_ids") or [plan_item["lead_id"]]
    blocks: list[str] = []

    for i, sid in enumerate(ids, 1):
        s = by_id.get(sid, {})
        r = raw_by_id.get(sid, {})
        blocks.append(f"--- 素材 {i} ---")
        if s:
            blocks.append(f"标题：{s.get('title','')}")
            blocks.append(f"来源：{', '.join(s.get('sources',[]) or [])}")
            blocks.append(f"链接：{s.get('url','')}")
            blocks.append(f"评分：{s.get('score','')}/10")
            if s.get("reasoning"):
                blocks.append(f"评分理由：{s['reasoning']}")
            if s.get("flags"):
                blocks.append(f"标记：{', '.join(s['flags'])}")
            if s.get("verify_todo"):
                blocks.append(f"待核查：{'; '.join(s['verify_todo'])}")
        content = (s.get("content") or r.get("content") or "")
        if len(content) > 200:
            blocks.append(f"正文摘录：\n{content[:7000]}")
        else:
            summ = (s.get("summary") or r.get("summary") or "")
            if summ:
                blocks.append(f"摘要：{summ[:1200]}")
        blocks.append("")

    ins = read_json(DATA_DIR / "tracker_insights.json", {})
    if ins:
        blocks.append("--- 行业背景数据 ---")
        for k, v in ins.items():
            for kk, vv in v.items():
                if isinstance(vv, list):
                    for x in vv:
                        blocks.append(f"· {x}")
                elif kk in ("jefferies", "published", "updated"):
                    blocks.append(f"· {vv}")
        blocks.append("")

    return "\n".join(blocks)


# ============================================================
# Prompt —— 只要求轻量标记，不要求 HTML
# ============================================================
SYSTEM_TPL = """你是医药行业资深撰稿人，为中文读者写行业分析。**写内容，不写 HTML。**

# 输出格式（严格遵守）

按下面的标记逐行输出。除了这些标记，**不要输出任何 HTML 标签**，
不要 markdown 代码块包裹，不要解释文字。

```
KICKER: 栏目名，例如 Event Report｜创新药授权
TITLE: 主标题（吸引人但不要标题党）
SUB: 一句话副标题
HOOK: 钩子，一段话。放最反常识的数字或矛盾对比
## 01 小节标题
正文段落，可以有多段
### 子标题（可选）
更多段落
TABLE: 列1 | 列2 | 列3
行1列1 | 行1列2 | 行1列3
行2列1 | 行2列2 | 行2列3
ENDTABLE
BOX w 标签： | 提示框内容（w=琥珀 g=绿 r=红）
## 02 下一节
……
ENDCARD 结语标题 | 结语正文
FN 1. 来源说明，注明来源性质
FN 2. 来源说明
```

规则：
- `## ` 小节必须有编号，如 `## 01 交易拆解`
- 表格第一行是表头，`|` 分隔，**每行列数要一致**
- `BOX` 三个变体：`w` 琥珀（口径提示）/ `g` 绿（正面）/ `r` 红（风险）
- `ENDCARD` 和 `FN` 各只能出现一次/在末尾，FN 可以多行
- 正文里可用 `**加粗**` 强调关键数字

# 内容规范

- **正文 2800-3200 字**（不含表格和脚注）。这是硬指标，宁可写满
- 开头 HOOK 必须给出反常识数字或矛盾对比
- **金额必须分层**：首付 / 里程碑上限 / 总额，三者不可混为一谈。
  如果只披露了合并口径，要写明"首付款与近期里程碑合计"，不能简写成"首付"
- 查不到的数据写"未找到公开数据"，**绝对禁止编造**
- **已知信息要写足**：不要把"未披露"当成段落主体。
  有的信息就展开分析；确实没有的，一句话交代后转到能分析的部分
- 每个小节至少 2-3 段，表格后要有解读，不要甩个表就走
- 结尾 ENDCARD 给判断，不要复述前文

# 文风（大白话，去 AI 味）

- 短句。不要长定语堆叠
- 禁止词：赋能、底层逻辑、抓手、值得注意的是、需要注意的是、
  综上所述、总而言之、不言而喻、至关重要、具有里程碑意义
- 禁止句式："不是…而是…"排比
- 禁止："本文将""本系列将""让我们"
- 直接抛数字或场景，不要铺垫

# 文章结构
{structure}

只输出上面的标记文本，第一个字符应该是 KICKER 或 TITLE。"""


def build_prompt(plan_item: dict, material: str, style_cfg: dict) -> tuple[str, str]:
    atype = plan_item.get("type", "event_report")
    struct = style_cfg.get("article_types", {}).get(atype, {})
    system = SYSTEM_TPL.replace("{structure}", struct.get("structure", ""))
    target = style_cfg.get("generation", {}).get("target_words", 3000)

    user = f"""# 今日选题
类型：{atype}（{struct.get('name','')}）
标题参考：{plan_item.get('title','')}
评分：{plan_item.get('score','')}/10
选题理由：{plan_item.get('reasoning','')}

# 可用素材
{material}

# 要求
标题要吸引人点击，但不要标题党。
正文 {target} 字左右（不含表格和脚注）。
素材里没有的数字不要写。素材里标了"待核查"的点，要么核实，
要么在文中写明"未找到公开数据"。

现在按标记格式输出。"""
    return system, user


# ============================================================
# 内容层校验
# ============================================================
# 中文金额："5亿美元" "50亿" "1.25亿美元"
MONEY_CN = re.compile(r"(\d+(?:\.\d+)?)\s*亿(?:美元|美金|元)?")
# 金额限定词
MONEY_QUAL = ("首付款", "首付", "总对价", "总额", "里程碑")


def _title_money_pairs(text: str) -> list[tuple[str, str]]:
    """提取「金额 + 限定词」组合。限定词取金额后 8 字内出现的那个。"""
    out = []
    for m in MONEY_CN.finditer(text):
        tail = text[m.end():m.end() + 8]
        qual = next((q for q in MONEY_QUAL if q in tail), "")
        out.append((m.group(1), qual))
    return out


def _check_title_numbers(doc: dict, body: str) -> list[str]:
    """
    标题里的金额与正文是否自洽。
    只检查带限定词的金额——不带限定词的（如"60亿押注"）噪声太大，
    误报成本高于收益，不做强制。
    """
    errs: list[str] = []
    title = doc.get("title", "")
    if not title:
        return errs

    body_pairs = _title_money_pairs(body)
    body_nums = {n for n, _ in body_pairs}
    body_qual_nums = {(q, n) for n, q in body_pairs if q}

    for num, qual in _title_money_pairs(title):
        if not qual:
            continue
        # 正文里必须有同限定词同数字的组合（限定的就近匹配）
        if (qual, num) in body_qual_nums:
            continue
        # 该限定词在正文中出现的所有数字
        same_qual = sorted({n for q, n in body_qual_nums
                            if q == qual or q.startswith(qual[:2])})
        if same_qual:
            errs.append(
                f"标题金额与正文不符：标题称 {num}亿{qual}，"
                f"正文中{qual}为 {'/'.join(same_qual)}亿")
        elif num not in body_nums:
            errs.append(f"标题金额 {num}亿{qual} 未在正文出现")
    return errs


def validate_content(doc: dict, text: str, style_cfg: dict) -> list[str]:
    """校验结构化内容（不涉及 HTML，因为 HTML 由代码保证）"""
    errs: list[str] = []
    gen = style_cfg.get("generation", {})
    target = gen.get("target_words", 3000)
    tol = gen.get("word_tolerance", 0.75)
    wmax = gen.get("max_words", 3900)

    # 1. 必需元素
    if not doc.get("title"):
        errs.append("缺少 TITLE")
    if not doc.get("hook"):
        errs.append("缺少 HOOK")
    if len(doc.get("sections") or []) < 3:
        errs.append(f"小节太少（{len(doc.get('sections') or [])} 个，至少 3 个）")
    if not doc.get("footnotes"):
        errs.append("缺少 FN 来源脚注")

    # 2. 字数
    n = body_chars(doc)
    lo, hi = int(target * tol), int(wmax)
    if not (lo <= n <= hi):
        errs.append(f"正文字数 {n} 超出范围（目标 {lo}-{hi}）")

    # 3. AI 味（分档）
    body = content_text(doc)
    tolerate = gen.get("ai_word_tolerate", 2)
    hard = gen.get("ai_word_hard_limit", 5)
    counts = {w: body.count(w) for w in style_cfg.get("banned_words", [])}
    total = sum(counts.values())
    if total > hard:
        top = sorted(counts.items(), key=lambda x: -x[1])[:5]
        errs.append(f"AI味词过多({total}处): "
                    + ", ".join(f"{w}x{c}" for w, c in top))
    elif total > tolerate:
        top = sorted(counts.items(), key=lambda x: -x[1])[:3]
        errs.append("AI味词: " + ", ".join(f"{w}x{c}" for w, c in top))

    for p in style_cfg.get("banned_patterns", []):
        if re.search(p, body):
            errs.append(f"AI味句式: {p}")

    # 4. 标题金额 vs 正文金额（实测抓到的真错误）
    #
    # 案例：CI 生成的稿件标题写「GSK豪掷50亿美元首付」，
    # 正文却写「GSK向恒瑞支付了5亿美元首付」——差 10 倍。
    # 正文是对的，标题错了。而标题是读者第一眼看到的数字，必须拦。
    #
    # 规则：标题里「数字+限定词」（首付/总额/里程碑）的组合，
    #       必须在正文中找到同一组合。限定词不去管的话，
    #       「50亿」会误配到表格里康方的「50亿美元总额」而放行。
    errs.extend(_check_title_numbers(doc, body))

    # 4. 表格列数一致
    for sec in doc.get("sections") or []:
        for b in sec.get("blocks") or []:
            if b.get("type") == "table":
                head = len(b.get("head") or [])
                for r in b.get("rows") or []:
                    if head and len(r) != head:
                        errs.append(
                            f"表格列数不一致：表头 {head} 列，"
                            f"有一行 {len(r)} 列")
                        break

    # 5. 标记残留（模型误把标记写进正文）
    if re.search(r"^\s*(TABLE|BOX|FN|KICKER|TITLE|SUB|HOOK)\s*[:：]",
                 body, re.M):
        pass  # 已被解析器消费，不算错
    if "<p>" in text or "<div" in text:
        errs.append("正文里混入了 HTML 标签（应使用标记格式）")

    return errs


# ============================================================
# 生成
# ============================================================
LENGTH_HINT = """

# 篇幅不足，必须加长

上一版正文只有 {n} 字，目标是 {target} 字。请按下面的方式扩写：

1. 每个 `## ` 小节至少 3 段（当前普遍只有 1-2 段）
2. 交易拆解加一张"条款逐项对照"表格（首付/里程碑/总额/权益范围/开发责任）
3. 增加"同类交易历史对照"段落，用素材里的数据库数据做纵向比较
4. 增加"买方动机"和"卖方视角"两段独立分析
5. 行业背景数据（授权支出占比、领域分布、阶段分布）展开成表格并解读
6. 结尾加"未来 12-18 个月时间表"

**注意：已知信息要展开写透，不要把多个"未找到公开数据"堆成段落。**
"""

STYLE_HINT = """

# 文风有问题

上一版出现了 AI 味表达，必须替换：

- "值得注意的是" → 直接给结论
- "综上所述"/"总而言之" → 直接给结论
- "不言而喻"/"至关重要" → 删掉或换成具体描述
- "赋能" → "支撑""帮上"
- "底层逻辑" → "基本逻辑""实际原因"
- "本文将"/"让我们" → 直接开始讲

篇幅保持不变，只替换词。
"""

STRUCT_HINT = """

# 结构有问题

请检查并修正：

1. 必须有 `TITLE:`、`HOOK:`、至少 3 个 `## ` 小节、以及 `FN ` 脚注
2. `TABLE:` 之后每行的 `|` 数量必须与表头一致
3. 不要输出任何 HTML 标签，只用标记
4. 结尾用 `ENDCARD 标题 | 正文`
"""


async def generate_one(client, eff: dict, plan_item: dict, material: str,
                       style_cfg: dict, out_dir: Path) -> dict:
    gen_cfg = style_cfg.get("generation", {})
    retries = gen_cfg.get("max_retries", 3)
    target = gen_cfg.get("target_words", 3000)

    system, user = build_prompt(plan_item, material, style_cfg)
    feedback = ""
    last_doc = None

    for attempt in range(retries + 1):
        msg = user
        if feedback:
            msg += f"\n\n# 上次的问题（必须修正）\n{feedback}\n"
            if "字数" in feedback:
                n = body_chars(last_doc) if last_doc else 0
                msg += LENGTH_HINT.replace("{n}", str(n)).replace(
                    "{target}", str(target))
            if "AI味" in feedback:
                msg += STYLE_HINT
            if any(k in feedback for k in ("缺少", "小节太少", "列数", "HTML")):
                msg += STRUCT_HINT

        try:
            txt = _strip_think(_call_llm(client, eff, system, msg, retries=1))
        except Exception as e:
            print(f"    尝试{attempt+1}: LLM 异常 {type(e).__name__}")
            continue

        if not txt or len(txt) < 200:
            print(f"    尝试{attempt+1}: 返回为空/过短（{len(txt or '')} 字符）")
            # 存下原始输出，便于诊断"为什么模型输出骤降"
            dbg = DATA_DIR / "debug"
            dbg.mkdir(parents=True, exist_ok=True)
            (dbg / f"short_{plan_item.get('lead_id','')[:8]}_{attempt+1}.txt"
             ).write_text(txt or "", encoding="utf-8")
            feedback = ("上次返回为空或过短。请输出完整内容："
                        "TITLE、HOOK、至少 3 个 ## 小节、表格、FN 脚注，"
                        "正文 2800-3200 字。")
            continue

        doc = parse_content(txt)
        last_doc = doc
        errs = validate_content(doc, txt, style_cfg)
        st = structure_report(doc)

        print(f"    尝试{attempt+1}: 正文{st['body_chars']}字 "
              f"小节{st['sections']} 表{st['tables']} "
              f"框{st['boxes']} 注{st['footnotes']} "
              f"{'✅通过' if not errs else '❌' + str(len(errs)) + '个问题'}")

        if not errs:
            html = render_html(doc)
            idx = plan_item.get("_idx", 1)
            atype = plan_item.get("type", "event_report")
            fname = f"{idx:02d}-{atype}-{plan_item.get('lead_id','')[:8]}.html"
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / fname).write_text(html, encoding="utf-8")
            # 同时存一份标记原文，便于人工比对
            (out_dir / (fname.replace(".html", ".txt"))).write_text(
                txt, encoding="utf-8")
            return {"ok": True, "file": fname, "stats": st,
                    "html_len": len(html)}

        feedback = "\n".join(f"- {e}" for e in errs)
        print(f"      问题: {feedback[:180]}")

    return {"ok": False, "error": feedback[:400],
            "stats": structure_report(last_doc) if last_doc else {}}


# ============================================================
# 主流程
# ============================================================
def run(date: str | None = None) -> list[dict]:
    date = date or today_cst()
    plan = read_json(SCORED_DIR / f"{date}.plan.json", {})
    if not plan or not plan.get("plan"):
        print("[compose] 没有选题计划")
        return []

    style_cfg = load_yaml("style_guide.yaml")
    cfg = load_yaml("scoring.yaml")
    out_dir = POSTS_DIR / date[:4] / date[5:7] / date[8:10]

    print(f"\n=== 生成 {len(plan['plan'])} 篇 -> {out_dir} ===")
    results: list[dict] = []

    async def _all():
        gcfg = cfg.get("gen_llm") or cfg["llm"]
        client, eff = _client(gcfg, cfg.get("providers", {}))
        print(f"       model={eff['model']} max_tokens={eff['max_tokens']}")
        for i, item in enumerate(plan["plan"], 1):
            item["_idx"] = i
            # 同日幂等：已经成稿过的 lead_id 不重复生成。
            # 配合 dedupe 的"只对往日去重"，保证一天跑两次时：
            # 晚班只写新增的，早班写过的直接跳过（不浪费 LLM 调用）。
            lead = (item.get("lead_id") or "")[:8]
            if lead and out_dir.exists() and any(
                    lead in p.name for p in out_dir.glob("*.html")):
                print(f"\n  [{i}] 已存在成稿，跳过 {item.get('title','')[:50]}")
                results.append({"ok": True, "skipped": True,
                                "title": item.get("title", ""),
                                "score": item.get("score", 0),
                                "type": item.get("type", ""),
                                "stats": {}})
                continue
            material = build_material(date, item)
            print(f"\n  [{i}] {item.get('type')} | {item.get('score')}分")
            print(f"      {item.get('title','')[:60]}")
            r = await generate_one(client, eff, item, material,
                                    style_cfg, out_dir)
            r["title"] = item.get("title", "")
            r["score"] = item.get("score", 0)
            r["type"] = item.get("type", "")
            results.append(r)

    asyncio.run(_all())

    ok = sum(1 for r in results if r.get("ok"))
    new = sum(1 for r in results if r.get("ok") and not r.get("skipped"))
    print(f"\n=== 完成 {ok}/{len(results)} 篇"
          f"（新生成 {new}，跳过已存在 {ok - new}）===")
    for r in results:
        mark = "✅" if r.get("ok") else "❌"
        st = r.get("stats") or {}
        print(f"  {mark} {r.get('type','')[:16]:16} "
              f"{st.get('body_chars',0):>5}字  {r.get('title','')[:36]}")
        if not r.get("ok"):
            print(f"      {r.get('error','')[:140]}")

    write_json(DATA_DIR / f"compose_{date}.json", results)
    return results


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    run(sys.argv[1] if len(sys.argv) > 1 else None)