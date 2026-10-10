"""
发布层：生成站点首页 + 飞书通知。

产出：
  site/            静态站点（GitHub Pages 直接发布）
  site/index.html  当日简报汇总页
  site/archive.html 历史归档
"""
from __future__ import annotations

import html as _html
import json
import os
import re
import sys
from pathlib import Path

import httpx

from common import (ROOT, DATA_DIR, SCORED_DIR, load_yaml, read_json,
                    write_json, today_cst)
from render import CSS, body_chars

POSTS_DIR = ROOT / "posts"
SITE_DIR = ROOT / "site"


def _iter_posts() -> list[dict]:
    """扫描 posts/ 目录，返回按日期倒序的文章列表"""
    out: list[dict] = []
    for p in sorted(POSTS_DIR.rglob("*.html"), reverse=True):
        # posts/YYYY/MM/DD/NN-type-id.html
        parts = p.relative_to(POSTS_DIR).parts
        if len(parts) < 4:
            continue
        date = f"{parts[0]}-{parts[1]}-{parts[2]}"
        body = p.read_text(encoding="utf-8")
        m = re.search(r"(?is)<title>(.*?)</title>", body)
        h1 = re.search(r"(?is)<h1>(.*?)</h1>", body)
        kicker = re.search(r'(?is)<div class="kicker">(.*?)</div>', body)
        sub = re.search(r'(?is)<p class="sub">(.*?)</p>', body)
        hook = re.search(r'(?is)<div class="hook">(.*?)</div>', body)
        cn = len(re.findall(r"[\u4e00-\u9fff]",
                            re.sub(r"(?is)<(style|script)[^>]*>.*?</\1>", " ", body)))
        out.append({
            "path": p,
            "rel": str(p.relative_to(POSTS_DIR)).replace("\\", "/"),
            "date": date,
            "title": (h1.group(1) if h1 else (m.group(1) if m else p.stem)),
            "kicker": kicker.group(1) if kicker else "",
            "sub": _strip_tags(sub.group(1)) if sub else "",
            "hook": _strip_tags(hook.group(1)) if hook else "",
            "chars": cn,
            "site_rel": str(p.relative_to(POSTS_DIR.parent)).replace("\\", "/"),
        })
    return out


def _strip_tags(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s or "")
    return _html.unescape(s).strip()


def build_index(posts: list[dict], date: str) -> str:
    today = [p for p in posts if p["date"] == date]
    rows = []
    for p in today:
        rows.append(f"""
    <article class="card">
      <div class="meta">{_html.escape(p['kicker'] or 'Analysis')} · {p['chars']} 字</div>
      <h2><a href="{_html.escape(p['rel'])}">{_html.escape(p['title'])}</a></h2>
      <p class="sub">{_html.escape(p['sub'][:160])}</p>
      <div class="hook">{_html.escape(p['hook'][:220])}</div>
      <a class="more" href="{_html.escape(p['rel'])}">阅读全文 →</a>
    </article>""")

    by_date: dict[str, list] = {}
    for p in posts:
        by_date.setdefault(p["date"], []).append(p)
    archive = "\n".join(
        f'<li><span class="d">{d}</span>'
        f'<a href="{_html.escape(v[0]["rel"])}">{_html.escape(v[0]["title"][:70])}</a>'
        f'<span class="c">{len(v)} 篇</span></li>'
        for d, v in sorted(by_date.items(), reverse=True)[:60]
    )

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>医药 BD 雷达 · {date}</title>
<style>
{CSS}
.index{{max-width:900px;margin:0 auto;padding:56px 24px 90px}}
.index h1{{font-size:clamp(26px,4vw,36px);color:var(--red);margin-bottom:8px}}
.tagline{{color:var(--ink2);font-size:15px;margin-bottom:40px}}
.card{{border:1px solid var(--rule);border-left:3px solid var(--red);padding:22px 26px;margin-bottom:22px;background:#fff}}
.card .meta{{font-size:11.5px;letter-spacing:.14em;text-transform:uppercase;color:var(--bronze);font-weight:700;margin-bottom:9px}}
.card h2{{font-size:20px;margin:0 0 8px;border:0;padding:0;line-height:1.45}}
.card h2 a{{color:var(--deep);text-decoration:none}}
.card h2 a:hover{{color:var(--red)}}
.card .sub{{font-size:13.5px;color:var(--ink2);margin:0 0 12px;font-style:normal}}
.card .hook{{background:var(--soft);color:var(--ink2);border-left:3px solid var(--red);padding:12px 15px;margin:0 0 13px;font-size:13.5px}}
.more{{font-size:13px;color:var(--red);text-decoration:none;font-weight:700}}
.ar{{margin-top:56px;padding-top:26px;border-top:2px solid var(--rule)}}
.ar h3{{font-size:17px;color:var(--deep);margin-bottom:14px}}
.ar ul{{list-style:none;margin:0}}
.ar li{{display:flex;gap:14px;align-items:baseline;padding:9px 0;border-bottom:1px solid var(--rule);font-size:14px}}
.ar .d{{font-family:var(--mono);font-size:12px;color:var(--faint);flex:0 0 88px}}
.ar li a{{color:var(--ink);text-decoration:none;flex:1}}
.ar li a:hover{{color:var(--red)}}
.ar .c{{font-size:11.5px;color:var(--faint)}}
.empty{{color:var(--ink2);padding:40px 0}}
</style>
</head>
<body>
<div class="index">
  <h1>医药 BD 雷达</h1>
  <p class="tagline">每日自动抓取全球医药 BD 热点 · 评分 · 成稿 · {date}</p>
  {''.join(rows) if rows else '<p class="empty">今日无稿件。</p>'}
  <div class="ar">
    <h3>历史归档</h3>
    <ul>{archive}</ul>
  </div>
</div>
</body>
</html>
"""


def build_site(date: str | None = None) -> Path:
    date = date or today_cst()
    posts = _iter_posts()
    SITE_DIR.mkdir(parents=True, exist_ok=True)

    # 文章复制到 site（保持相对路径）
    for p in posts:
        dest = SITE_DIR / p["rel"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(p["path"].read_text(encoding="utf-8"), encoding="utf-8")

    # 首页
    (SITE_DIR / "index.html").write_text(build_index(posts, date), encoding="utf-8")

    # 当日简报 JSON（供外部消费）
    today = [p for p in posts if p["date"] == date]
    write_json(SITE_DIR / "feed.json", {
        "date": date,
        "count": len(today),
        "posts": [{k: v for k, v in p.items() if k not in ("path",)} for p in today],
        "all_count": len(posts),
    })

    print(f"[publish] 站点 -> {SITE_DIR}（{len(posts)} 篇，今日 {len(today)} 篇）")
    return SITE_DIR


# ============================================================
# 通知
# ============================================================
def build_card(posts: list[dict], date: str, base_url: str = "") -> dict:
    """飞书交互卡片"""
    lines = []
    for p in posts:
        t = _html.escape(p["title"][:60])
        if base_url:
            lines.append(f"**[{t}]({base_url}/{p['rel']})**")
        else:
            lines.append(f"**{t}**")
        if p["sub"]:
            lines.append(p["sub"][:90])
        lines.append("")
    body = "\n".join(lines) or "今日无稿件。"

    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text",
                          "content": f"医药 BD 雷达 · {date}"},
                "template": "red",
            },
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": body}},
                {"tag": "hr"},
                {"tag": "note", "elements": [
                    {"tag": "plain_text",
                     "content": f"共 {len(posts)} 篇 · 自动生成"},
                ]},
            ],
        },
    }


def notify_feishu(posts: list[dict], date: str,
                  webhook: str | None = None,
                  base_url: str = "") -> bool:
    url = webhook or os.getenv("FEISHU_WEBHOOK")
    if not url:
        print("[notify] 未配置 FEISHU_WEBHOOK，跳过")
        return False
    if not posts:
        print("[notify] 无稿件，不推送")
        return False
    payload = build_card(posts, date, base_url)
    try:
        r = httpx.post(url, json=payload, timeout=20)
        ok = r.status_code == 200 and '"code":0' in r.text.replace(" ", "")
        print(f"[notify] 飞书 {'成功' if ok else '失败'} ({r.status_code})")
        return ok
    except Exception as e:
        print(f"[notify] 飞书异常: {type(e).__name__}")
        return False


def run(date: str | None = None, do_notify: bool = True) -> None:
    date = date or today_cst()
    build_site(date)
    posts = [p for p in _iter_posts() if p["date"] == date]
    if do_notify:
        notify_feishu(posts, date,
                      base_url=os.getenv("SITE_BASE_URL", ""))


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    run(sys.argv[1] if len(sys.argv) > 1 else None,
        do_notify="--no-notify" not in sys.argv)