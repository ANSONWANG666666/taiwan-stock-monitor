#!/usr/bin/env python3
"""
每週成效摘要：每週五收盤後（同一週只推一次），讀訊號紀錄簿（journal 分支的 CSV），
統計各類訊號之後 5／20 個交易日的超額報酬，推播到 Telegram。

只讀 CSV、不呼叫 Jev，不產生任何費用。

統計方式：
  - 同一來源、同一天、同一檔只取第一筆（大單監控一天可能推同一檔很多次）
  - 超額報酬 = 個股漲跌 − 同期加權指數漲跌
  - 勝率 = 超額報酬 > 0 的比例
  - 筆數少於 MIN_SAMPLE 的分組標示「樣本不足」
"""

import csv
import html
import json
import logging
import os
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from statistics import mean, median
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

from journal import journal_dir
from stock_screener import send_telegram

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

TZ = ZoneInfo("Asia/Taipei")
MIN_SAMPLE = int(os.environ.get("MIN_SAMPLE", "30"))
STATE_FILE = Path("weekly_state.json")   # 防止備援排程重複推播

SOURCE_LABEL = {"market": "全市場掃描", "screener": "盤中選股", "monitor": "大單監控",
                "pattern_tri": "型態：三角收斂", "pattern_chuaner": "型態：穿山二龍（觀察）",
                "pattern_hfw": "型態：黑飛舞（觀察）"}
VERDICT_LABEL = {
    "confirmed_positive": "✅ 證實利多",
    "confirmed_negative": "🔻 證實利空",
    "rumor_driven": "⚠️ 題材帶動",
    "rumor_only": "⚠️ 只有傳聞",
    "neutral": "➖ 無明確方向",
    "none": "❓ 查無新聞",
    "unchecked": "▫️ 未查證",
    "budget": "▫️ 額度用完",
    "error": "▫️ 查證失敗",
}
VERDICT_ORDER = list(VERDICT_LABEL)


def _f(v) -> Optional[float]:
    try:
        return float(v) if v not in (None, "") else None
    except ValueError:
        return None


def load_signals(d: Path) -> List[dict]:
    """讀所有 CSV；同一來源同一天同一檔只留第一筆"""
    rows, seen = [], set()
    for path in sorted(d.glob("signals_*.csv")):
        with path.open(encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                key = (r["source"], r["date"], r["code"])
                if key in seen:
                    continue
                seen.add(key)
                rows.append(r)
    return rows


def stats(rows: List[dict], field: str) -> Optional[dict]:
    vals = [v for v in (_f(r.get(field)) for r in rows) if v is not None]
    if not vals:
        return None
    return {"n": len(vals), "avg": mean(vals), "med": median(vals),
            "win": sum(v > 0 for v in vals) / len(vals)}


def fmt_stats(s: Optional[dict]) -> str:
    if not s:
        return "—"
    warn = "（樣本不足）" if s["n"] < MIN_SAMPLE else ""
    return f"{s['avg']:+.1f}%／勝率 {s['win']:.0%}／{s['n']} 筆{warn}"


def one_line(label: str, rows: List[dict]) -> Optional[str]:
    """一組訊號一行：5 日、20 日超額報酬"""
    s5, s20 = stats(rows, "excess_5d"), stats(rows, "excess_20d")
    if not s5 and not s20:
        return None
    parts = [f"5日 {fmt_stats(s5)}"]
    if s20:
        parts.append(f"20日 {fmt_stats(s20)}")
    return f"• {label}：" + "｜".join(parts)


def group_lines(rows: List[dict], key, order: List[str], labels: Dict[str, str]) -> List[str]:
    groups = defaultdict(list)
    for r in rows:
        groups[key(r)].append(r)
    lines = []
    for g in sorted(groups, key=lambda g: order.index(g) if g in order else len(order)):
        line = one_line(labels.get(g, html.escape(g)), groups[g])
        if line:
            lines.append(line)
    return lines


ACTIVE = "pattern_tri"                                   # 目前有推播進場的訊號
PATTERN_SOURCES = ["pattern_tri", "pattern_chuaner", "pattern_hfw"]
STOPPED = {"market": "全市場掃描（量價齊揚）", "screener": "盤中選股", "monitor": "權值股大單監控"}


def build_report(rows: List[dict], today: datetime) -> str:
    week_start = (today - timedelta(days=today.weekday())).strftime("%Y-%m-%d")
    this_week = [r for r in rows if r["date"] >= week_start]
    n5 = sum(_f(r.get("excess_5d")) is not None for r in rows)
    n20 = sum(_f(r.get("excess_20d")) is not None for r in rows)

    by_src = defaultdict(int)
    for r in this_week:
        by_src[r["source"]] += 1
    order = PATTERN_SOURCES + list(STOPPED)
    week_txt = "、".join(f"{SOURCE_LABEL.get(k, k)} {by_src[k]}"
                        for k in sorted(by_src, key=lambda k: order.index(k) if k in order else 99)) or "無"

    lines = [f"📈 <b>訊號成效週報｜{today.strftime('%Y-%m-%d')}</b>",
             f"本週新增：{week_txt}",
             f"累積 {len(rows)} 筆；已有 5 日結果 {n5} 筆、20 日結果 {n20} 筆",
             "<i>數字為超額報酬（個股 − 加權指數）平均／勝率／筆數</i>"]

    # ① 目前有推播的：型態選股
    pat = [r for r in rows if r["source"] in PATTERN_SOURCES]
    lines.append("\n🎯 <b>型態選股（目前推播）</b>")
    pl = []
    tri = [r for r in pat if r["source"] == ACTIVE]
    pl += [x for x in (one_line("三角收斂・列進場", [r for r in tri if r.get("slot") != "觀察"]),
                       one_line("穿山二龍・觀察", [r for r in pat if r["source"] == "pattern_chuaner"]),
                       one_line("黑飛舞・觀察", [r for r in pat if r["source"] == "pattern_hfw"])) if x]
    if pl:
        lines += pl
    else:
        first = min((r["date"] for r in pat), default=None)
        lines.append(f"  還沒有滿 5 個交易日的結果（{first[5:] if first else '—'} 起記錄），"
                     "滿 5 個交易日後開始統計。")

    # ② 已停推、仍在追蹤：驗證停推是否正確
    stopped = [r for r in rows if r["source"] in STOPPED]
    sl = []
    market = [r for r in stopped if r["source"] == "market"]
    for sc in ("3", "2"):
        x = one_line(f"量價齊揚 {sc}/3 分", [r for r in market if str(r.get("score")) == sc])
        if x:
            sl.append(x)
    for src in ("screener", "monitor"):
        x = one_line(STOPPED[src], [r for r in stopped if r["source"] == src])
        if x:
            sl.append(x)
    if sl:
        lines.append("\n📴 <b>已停推、仍在追蹤</b>（若轉為穩定正報酬再考慮恢復）")
        lines += sl
    checked = [r for r in market if r.get("news_verdict") not in ("unchecked", "budget", "error", "", None)]
    if stats(checked, "excess_5d"):
        x = one_line("Jev 查過新聞的（已停用）", checked)
        if x:
            lines.append(x)

    # ③ 本月最佳／最差：優先列目前推播的訊號
    def best_worst(pool: List[dict], title: str) -> List[str]:
        pool = [r for r in pool if _f(r.get("excess_5d")) is not None]
        if not pool:
            return []
        month = max(r["date"] for r in pool)[:8] + "01"
        pool = [r for r in pool if r["date"] >= month]
        if len(pool) < 3:
            return []
        pool.sort(key=lambda r: _f(r["excess_5d"]), reverse=True)
        fmt = lambda r: (f"  {html.escape(r['name'])} {r['code']}（{r['date'][5:]}）"
                         f" {_f(r['excess_5d']):+.1f}%　{SOURCE_LABEL.get(r['source'], r['source'])}")
        k = min(3, len(pool) // 2) or 1
        return [f"\n🏆 <b>本月最佳（5 日，{title}）</b>"] + [fmt(r) for r in pool[:k]] + \
               [f"🥶 <b>本月最差（5 日，{title}）</b>"] + [fmt(r) for r in pool[-k:][::-1]]

    bw = best_worst([r for r in tri if r.get("slot") != "觀察"], "三角收斂進場")
    lines += bw or best_worst(rows, "全部訊號")

    if not n5:
        lines.append("\n還沒有訊號滿 5 個交易日，下週起會開始有成效數字。")
    lines.append(f"\n<i>任何分組少於 {MIN_SAMPLE} 筆前，差異很可能只是運氣。這是統計資訊，不構成投資建議。</i>")
    return "\n".join(lines)


def week_key(t: datetime) -> str:
    y, w, _ = t.isocalendar()
    return f"{y}-W{w:02d}"


def main():
    now = datetime.now(TZ)
    today = now.strftime("%Y-%m-%d")
    week = week_key(now)
    state = {}
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            state = {}
    # 同一週只推一次：備援排程常延遲到週六凌晨，用「日期」判斷會重複推播
    sent_week = state.get("last_week") or (week_key(datetime.fromisoformat(state["last_sent"]))
                                           if state.get("last_sent") else None)
    if sent_week == week and os.environ.get("FORCE_REPORT") != "1":
        logger.info("本週（%s）已推播過週報，略過（手動重跑請填 force=1）", week)
        return
    d = journal_dir()
    if not d.is_dir():
        logger.error("找不到訊號紀錄簿資料夾 %s", d)
        return
    rows = load_signals(d)
    logger.info("讀取訊號 %d 筆（去重後）", len(rows))
    msg = build_report(rows, datetime.now(TZ))
    logger.info("\n%s", msg)
    if len(msg) > 4000:   # Telegram 單則上限 4096 字
        msg = msg[:3990] + "\n…"
    if send_telegram(msg):
        STATE_FILE.write_text(json.dumps({"last_sent": today, "last_week": week}), encoding="utf-8")


if __name__ == "__main__":
    main()
