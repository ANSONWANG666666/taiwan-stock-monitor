#!/usr/bin/env python3
"""
觀察名單每週覆盤（掛在「台股成效週報」之後，同一週只推一次）

1. 本週觸發的提醒，以及提醒之後到今天的走勢（至今漲跌、期間最差）
2. 推翻條件檢查：程式能判斷的直接列出是否觸發；文字條件（指引下修等）逐條問你本週是否發生
3. 今年以來報酬：觀察名單（有填持股就用持股市值加權，否則等權）vs 0050，直接寫出差距
4. 市場溫度與加碼閘門（收盤後算好、存在 data 分支）

不呼叫 Jev，不產生費用。
"""

from __future__ import annotations

import csv
import html
import json
import logging
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List, Optional, Tuple
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

import history
import watch_alert as wa
from journal import journal_dir
from notifier import send_telegram

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

TZ = ZoneInfo("Asia/Taipei")
STATE_FILE = Path("watch_review_state.json")


def ytd(series: pd.Series, year: int) -> float:
    """今年以來報酬：以去年最後一個交易日收盤為基準"""
    s = series.dropna()
    prev = s[s.index.year < year]
    cur = s[s.index.year == year]
    if cur.empty:
        return float("nan")
    base = prev.iloc[-1] if len(prev) else cur.iloc[0]
    return float((cur.iloc[-1] / base - 1) * 100)


def portfolio_ytd(items: List[dict], closes: dict, year: int) -> Tuple[float, str, List[dict]]:
    held = [w for w in items if (w.get("holding") or {}).get("shares")]
    basis = "實際持股市值加權（假設今年股數不變）"
    if not held:
        held, basis = items, "觀察名單等權（未填持股，僅供參考）"
    v0 = v1 = 0.0
    detail = []
    for w in held:
        s = closes.get(w["code"])
        if s is None or s.empty:
            continue
        r = ytd(s, year)
        if np.isnan(r):
            continue
        sh = float((w.get("holding") or {}).get("shares") or 0)
        prev = s[s.index.year < year]
        p0 = float(prev.iloc[-1]) if len(prev) else float(s[s.index.year == year].iloc[0])
        if sh:
            v0, v1 = v0 + p0 * sh, v1 + float(s.iloc[-1]) * sh
        else:
            v0, v1 = v0 + 1.0, v1 + 1 + r / 100
        detail.append({"code": w["code"], "name": w.get("name", ""), "ytd": r})
    return ((v1 / v0 - 1) * 100 if v0 else float("nan")), basis, detail


def week_alerts(week_start: str, raw: dict) -> List[dict]:
    p = journal_dir() / "signals_watch.csv"
    if not p.exists():
        return []
    with p.open(encoding="utf-8-sig", newline="") as f:
        rows = [r for r in csv.DictReader(f) if r.get("date", "") >= week_start]
    out = []
    for r in rows:
        s = raw.get(r["code"])
        try:
            price = float(r["price"])
        except (TypeError, ValueError):
            continue
        if s is None or s.empty or not price:
            continue
        after = s[s.index >= pd.Timestamp(r["date"])]
        if after.empty:
            continue
        out.append({**r, "now": float(after.iloc[-1]),
                    "since": (after.iloc[-1] / price - 1) * 100,
                    "worst": (after.min() / price - 1) * 100})
    return out


def build(items, df, bench: pd.Series, bench_code: str, today: date, temp: Optional[dict],
          foreign=wa.finmind_foreign_net) -> str:
    e = html.escape
    year = today.year
    adj, raw, series = {}, {}, {}
    for w in items:
        s = wa.series_of(df, w["code"])
        if s.empty:
            continue
        series[w["code"]] = wa.add_indicators(s)
        adj[w["code"]] = s["Close"]
        raw[w["code"]] = s["Raw"]

    L = [f"📬 <b>觀察名單每週覆盤｜{today:%Y-%m-%d}</b>"]

    # 1. 績效
    tot, basis, detail = portfolio_ytd(items, adj, year)
    b = ytd(bench, year) if not bench.empty else float("nan")
    L.append("\n<b>① 今年以來報酬（含息還原）</b>")
    if np.isnan(tot):
        L.append("資料不足")
    else:
        L.append(f"組合 {tot:+.2f}%｜{bench_code} {wa._fmt(b, '{:+.2f}%')}"
                 + ("" if np.isnan(b) else f"｜差距 {tot - b:+.2f} 個百分點"))
        L.append(f"<i>計算基礎：{basis}</i>")
        for d in sorted(detail, key=lambda d: -d["ytd"]):
            L.append(f"• {e(str(d['name']))} {d['code']}：{d['ytd']:+.2f}%")
        if not np.isnan(b) and tot < b:
            L.append(f"→ <b>選股目前沒有帶來超額報酬</b>，直接持有 {bench_code} 的結果更好。")

    # 2. 本週提醒
    week_start = (today - timedelta(days=today.weekday())).isoformat()
    wk = week_alerts(week_start, raw)
    L.append("\n<b>② 本週提醒與後續走勢</b>")
    if not wk:
        L.append("本週沒有發出提醒。")
    for r in wk:
        L.append(f"• {e(r['name'])} {r['code']}【{r['slot']}】{r['date'][5:]} "
                 f"{float(r['price']):,.2f} → {r['now']:,.2f}（{r['since']:+.1f}%，期間最差 {r['worst']:+.1f}%）")

    # 3. 推翻條件
    L.append("\n<b>③ 推翻條件檢查</b>")
    order = sorted(items, key=lambda w: not (w.get("holding") or {}).get("shares"))
    any_line = False
    for w in order:
        tag = "持股" if (w.get("holding") or {}).get("shares") else "觀察"
        s = series.get(w["code"])
        hits = wa.check_invalidation(w, s, foreign) if s is not None and len(s) else []
        name = e(str(w.get("name") or w["code"]))
        if hits:
            L.append(f"⛔ {name} {w['code']}（{tag}）已觸發：" + "；".join(e(h) for h in hits))
            any_line = True
        for q in (w.get("invalidation") or {}).get("text") or []:
            L.append(f"❓ {name}：「{e(str(q))}」本週是否發生？")
            any_line = True
        if not w.get("invalidation"):
            L.append(f"▫️ {name}：沒有填推翻條件，無法覆盤，請補上")
            any_line = True
    if not any_line:
        L.append("沒有任何推翻條件被觸發。")

    # 4. 溫度
    if temp and temp.get("asof"):
        L += ["\n<b>④ 市場溫度</b>", wa.temp_line(temp)]
    L.append("\n<i>本信只整理事實與規則結論，不構成投資建議。有加減碼請更新 watchlist.yaml 的 holding.shares。</i>")
    return "\n".join(L)


def week_key(t: datetime) -> str:
    y, w, _ = t.isocalendar()
    return f"{y}-W{w:02d}"


def main():
    now = datetime.now(TZ)
    week = week_key(now)
    if wa._read_json(STATE_FILE).get("last_week") == week and os.environ.get("FORCE_REPORT") != "1":
        logger.info("本週觀察名單覆盤已推播過，略過")
        return
    cfg = wa.load_watchlist()
    if not cfg["watchlist"]:
        logger.info("watchlist.yaml 沒有觀察標的，不推覆盤")
        return
    start = date(now.year - 1, 12, 1)
    df = history.load(start=start.isoformat(), end=now.date().isoformat())
    if df.empty:
        logger.error("沒有歷史資料（data 分支）")
        return
    df = history.adjust(df)
    bench_code = cfg["benchmark"]
    bench = wa.yf_close(f"{bench_code}.TW", period="2y")
    msg = build(cfg["watchlist"], df, bench, bench_code, now.date(), wa._read_json(wa.TEMP_FILE))
    logger.info("\n%s", msg)
    if send_telegram(msg):
        wa._write_json(STATE_FILE, {"last_week": week})


if __name__ == "__main__":
    main()
