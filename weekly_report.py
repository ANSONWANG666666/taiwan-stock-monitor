#!/usr/bin/env python3
"""
每週成效摘要：每週五收盤後，讀訊號紀錄簿（journal 分支的 CSV），
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


def group_lines(rows: List[dict], key, order: List[str], labels: Dict[str, str]) -> List[str]:
    groups = defaultdict(list)
    for r in rows:
        groups[key(r)].append(r)
    lines = []
    for g in sorted(groups, key=lambda g: order.index(g) if g in order else len(order)):
        s5, s20 = stats(groups[g], "excess_5d"), stats(groups[g], "excess_20d")
        if not s5 and not s20:
            continue
        lines.append(f"<b>{labels.get(g, html.escape(g))}</b>")
        lines.append(f"  5 日：{fmt_stats(s5)}")
        if s20:
            lines.append(f"  20 日：{fmt_stats(s20)}")
    return lines


def build_report(rows: List[dict], today: datetime) -> str:
    week_start = (today - timedelta(days=today.weekday())).strftime("%Y-%m-%d")
    this_week = [r for r in rows if r["date"] >= week_start]
    n5 = sum(_f(r.get("excess_5d")) is not None for r in rows)
    n20 = sum(_f(r.get("excess_20d")) is not None for r in rows)

    by_src = defaultdict(int)
    for r in this_week:
        by_src[r["source"]] += 1
    week_txt = "、".join(f"{SOURCE_LABEL.get(k, k)} {v}" for k, v in sorted(by_src.items())) or "無"

    lines = [f"📈 <b>訊號成效週報｜{today.strftime('%Y-%m-%d')}</b>",
             f"本週新增訊號：{week_txt}",
             f"累積 {len(rows)} 筆，已有 5 日結果 {n5} 筆、20 日結果 {n20} 筆",
             "<i>數字為超額報酬（個股 − 加權指數）平均／勝率／筆數</i>"]

    if not n5:
        lines.append("\n還沒有訊號滿 5 個交易日，下週起會開始有成效數字。")
        return "\n".join(lines)

    market = [r for r in rows if r["source"] == "market"]
    is_checked = lambda r: r.get("news_verdict") not in ("unchecked", "budget", "error", "")

    lines.append("\n📰 <b>全市場掃描｜依 Jev 新聞查證結論</b>")
    lines += group_lines(market, lambda r: r.get("news_verdict") or "unchecked", VERDICT_ORDER, VERDICT_LABEL)

    lines.append("\n🔍 <b>全市場掃描｜有查證 vs 未查證</b>")
    lines += group_lines(market, lambda r: "查證" if is_checked(r) else "未查證",
                         ["查證", "未查證"], {"查證": "有查新聞（前 10 名）", "未查證": "未查新聞"})

    lines.append("\n📊 <b>全市場掃描｜依起漲評分</b>")
    lines += group_lines(market, lambda r: str(r.get("score") or "?"), ["3", "2"],
                         {"3": "3/3 分", "2": "2/3 分"})

    lines.append("\n📡 <b>依訊號來源</b>")
    lines += group_lines(rows, lambda r: r["source"], ["market", "screener", "monitor", "pattern_tri", "pattern_chuaner", "pattern_hfw"],
                         SOURCE_LABEL)

    recent = [r for r in rows if _f(r.get("excess_5d")) is not None]
    recent.sort(key=lambda r: r["date"], reverse=True)
    recent = [r for r in recent if r["date"] >= recent[0]["date"][:8] + "01"] if recent else []
    if len(recent) >= 3:
        recent.sort(key=lambda r: _f(r["excess_5d"]), reverse=True)
        fmt = lambda r: (f"  {html.escape(r['name'])} {r['code']}（{r['date'][5:]}）"
                         f" {_f(r['excess_5d']):+.1f}%　{VERDICT_LABEL.get(r.get('news_verdict'), '')}")
        lines.append("\n🏆 <b>本月 5 日超額報酬最佳</b>")
        lines += [fmt(r) for r in recent[:3]]
        lines.append("🥶 <b>本月 5 日超額報酬最差</b>")
        lines += [fmt(r) for r in recent[-3:][::-1]]

    lines.append(f"\n<i>任何分組少於 {MIN_SAMPLE} 筆前，差異很可能只是運氣。這是統計資訊，不構成投資建議。</i>")
    return "\n".join(lines)


def main():
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    state = {}
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            state = {}
    if state.get("last_sent") == today and os.environ.get("FORCE_REPORT") != "1":
        logger.info("今天已推播過週報，略過（手動重跑請填 force=1）")
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
        STATE_FILE.write_text(json.dumps({"last_sent": today}), encoding="utf-8")


if __name__ == "__main__":
    main()
