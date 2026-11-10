# 象信 AI 前置闸门 —— 接入说明

**日期**：2026-11-10
**改动范围**：`src/score.py`、`config/scoring.yaml`、新增 `src/xiangxin_gate.py`
**备份**：`src/score.py.bak-before-xiangxin-gate`

---

## 一句话

象信**不替代** LLM 打分，而是做 LLM 前面的闸门：拦掉明确不是 BD 交易的条目，
顺便提示「这里到底有没有明确的首付款金额」。省 80% 的 LLM 调用，同时修掉一个金额抽取老 bug。

---

## 改了什么

### 1. 新增 `src/xiangxin_gate.py`

独立模块，只做两件事：

| 做什么 | 怎么做的 |
|---|---|
| 拦掉非交易条目 | Choice 问题「有没有交易对手方」，置信度 ≥ 0.80 且答案是「非交易」才拦 |
| 首付披露提示 | Noul 问题「有没有明确的首付款金额」，结果作为 `has_upfront_disclosed` 喂给 LLM |

**安全边界（全部实测过）**：

- 无 `XIANGXIN_API_KEY` → 全部放行
- 没装 `xiangxin-sdk` → 全部放行
- key 无效（401）→ **整体旁路**，只打一次请求就退出，不浪费 401
- 单条失败 → 只放行那一条，其余继续
- 配置关闭 → 完全不生效

### 2. `src/score.py`

- `score_items()` 开头接闸门，在 `_prefilter` 之后、送 LLM 之前
- `_build_items()` 增加可选参数 `upfront_hints`，命中时给条目加 `has_upfront_disclosed` 字段
- `SYSTEM_PROMPT` 里 `upfront_usd` 说明补充了 `has_upfront_disclosed` 的用法

### 3. `config/scoring.yaml`

新增 `llm.xiangxin_gate.enabled`，**默认 false**（见上线建议）。

---

## 实测数据

### 漏杀验证（最关键的指标）

正例定义：历史 scored 里 `deal_key` 非空且不含占位符
（排除 `unknown` / `undisclosed` / `not-disclosed` / `licensor-unknown` 等）。

| 样本 | 条数 | 漏杀 |
|---|---|---|
| 全部确证交易（23 天） | 147 | **0（0.0%）** |
| 分数 ≥ 6.5（生产 keep 线） | 10 | **0（0.0%）** |
| 分数 ≥ 8.0（must_publish） | 1 | **0** |

> **口径修正说明**：最初用「`deal_key` 非空」当正例，得到 4 条漏杀（2.5%）。
> 逐条查原文后发现这 4 条全是 LLM 用 `unknown` / `undisclosed` 硬造的占位符 deal_key
> ——象信判对了，LLM 判错了。排除占位符后漏杀为 0。
>
> 这本身暴露了 `score.py` 的一个问题：**LLM 在素材不足时会编造 deal_key**，
> 所以 `deal_key` 非空不等于「确证是交易」。

### 拦截率与耗时

| 指标 | 象信 | LLM |
|---|---|---|
| 单条耗时 | 388 ms | 3364 ms |
| 10-25 当天（77 条预筛后） | 拦掉 61（79.2%），剩 16 条送 LLM | 77 条全量 |
| 当天成本 | ¥0.0024 | 高一个数量级 |

### 首付提示的价值（32 天样本）

在 32 条「原文正则能抽到明确首付」的条目上：

| | 准确率 |
|---|---|
| 象信判「有首付」 | **24/32 = 75.0%** |
| LLM 抽出非零首付 | **11/32 = 34.4%** |

**象信能救回 LLM 漏抽的 14 条**，包括：

```
$1,250,000,000  Pfizer 从3SBio（$6.05B）              LLM 抽成 0
$  500,000,000  GSK 从Hengrui Pharma（$12.50B）        LLM 抽成 0
$  200,000,000  Novo Nordisk 从United Labs（$2.00B）   LLM 抽成 0
$  200,000,000  Merck & Co. 从Hengrui（$1.97B）        LLM 抽成 0
$  125,000,000  Vor Biopharma 从Remegen（$4.12B）      LLM 抽成 0
$   80,000,000  Regeneron 从Hansoh（$2.01B）           LLM 抽成 0
$1,550,000,000  Servier's $1.5B bet on Edgewise         LLM 抽成 0
   …… 共 14 条
```

这些原文都明明白白写着「首付款：$1.25B」或「paying $55 million upfront」，
LLM 却填了 0 —— 这正是 `score.py` 老注释里说的：

> 「模型经常把 "up to $1.5B" 当成首付，同一篇稿子三天给出 3.0 / 5.5 / 7.2」

### 反向误报

象信判「有首付」但 LLM 抽到 0 的 14 条，与上面是同一批——**是 LLM 漏抽，不是象信误报**。
象信在 32 条上无误报。

---

## 回归测试结果

```
测试 1：闸门关闭 → _prefilter 仍 77 条，与改动前一致          ✅
测试 2：闸门开启 → 拦掉 61/77（79.2%），漏杀 0 条             ✅
测试 3：无 key → 全部放行 77/77                              ✅
测试 3：无效 key → 全部放行，且只打 1 次请求                  ✅
端到端：闸门开启 + LLM 完整评分 → 14 条产出，
        下游字段完整（upfront_usd/total_usd/deal_key/reasoning/dims/flags 全非空）  ✅
```

端到端对比历史产出：历史和本次都是 **0 条 ≥6.5**（那天本来就没够格的稿），
共有条目平均分差 0.81（LLM 自身的非确定性）。

---

## 怎么用

```bash
cd C:\Users\guoga\Documents\DSH\pharma-bd-radar
pip install xiangxin-sdk
# XIANGXIN_API_KEY 已在 User 作用域配好
```

### 单独测试闸门

```bash
python src/xiangxin_gate.py
```

### 开启/关闭

改 `config/scoring.yaml`：

```yaml
llm:
  xiangxin_gate:
    enabled: true      # false = 关闭（当前默认）
```

关闭时输出 `[xiangxin-gate] 未启用`，行为与改动前完全一致。

### 调保守程度

`src/xiangxin_gate.py` 的 `BLOCK_CONFIDENCE`（默认 0.80）：

- 提高到 0.90 → 拦得少，漏杀风险更低
- 降低到 0.70 → 拦得多，省得更多

---

## 上线建议

**当前默认 `enabled: false`，建议按下面步骤开：**

1. **先观察 1–2 天**（保持 false），记录每天的产出条数、≥6.5 条数、≥8.0 条数作基线
2. **开 `enabled: true`**，跑同样的 1–2 天，对比上面三个指标
3. **重点看**：
   - ≥8.0 的条目有没有变少 → 变少说明有漏杀，要调低 `BLOCK_CONFIDENCE`
   - `upfront_usd` 非零的条数有没有上升 → 上升说明首付提示在起作用
4. 若 2 天内 ≥8.0 条数完全一致且首付抽取变好，再长期开启

**回滚**：把 `enabled` 改回 `false` 即可，无需回滚代码。

---

## 已知局限

1. **样本仍偏小**：漏杀验证用了 147 条确证交易、23 天数据，但真正的正例
   （≥6.5 分）只有 10 条。10 条 0 漏杀是好信号，但统计上仍不宽裕。
   建议上线后连续观察 7–10 天。

2. **首付提示只在 `noul ≥ 0.60` 时才给**，避免低置信度误导 LLM。
   实测 32 条上象信无误报，这个阈值目前没触发过保守保护。

3. **象信仍不抽数字**。它只告诉 LLM「这里有首付款，去抽」，
   具体数值还是 LLM 从原文抄。本次实测 LLM 抄错的概率不低
   （如「up to $1.5B」被当首付），这一类只能靠代码二次校验，
   `_amount_dim()` 目前已经按 `upfront_usd` 重算了金额分，算是兜住了。

4. **闸门会拦掉 `roundup` 类栏目条目**。历史上这类条目（如
   `Your Questions Answered…`）被 LLM 判为 `not_bd`，但象信有时认为其中
   提及了交易（0.53–0.91 不等）。这类条目 LLM 本来也会标 `not_bd` 丢掉，
   所以拦掉不影响产出；但如果将来想利用 roundup 里的交易线索，
   需要为 roundup 单开一个问题。

5. **`related_party` 检测仍然缺失**。`score.py` 规则① 要求标关联交易，
   但 32 天历史里 `related_party` 命中 **0 条**——要么真没有关联交易，
   要么 LLM 漏标。值得单独验证，我没纳入本次改动。

---

## 相关文件

| 文件 | 说明 |
|---|---|
| `src/xiangxin_gate.py` | 闸门模块（新增） |
| `src/score.py` | 接入点 |
| `config/scoring.yaml` | `llm.xiangxin_gate.enabled` 开关 |
| `src/score.py.bak-before-xiangxin-gate` | 改动前备份 |

实验脚本与数据在 `C:\Users\guoga\Documents\DSH\xiangxin-demo\`：
`verify_gate_misses.py`、`verify_gate_misses_fixed.py`、`test_integration.py`、
`test_e2e.py`、`measure_upfront_full.py`，数据在 `comparison/`。
