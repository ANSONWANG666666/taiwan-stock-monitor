#!/usr/bin/env python3
"""
觀察名單提醒門檻回測（10 年日K，yfinance 含息還原價）

data 分支只有 2 年，不夠評估回檔門檻，所以這支用 yfinance 抓長期資料。

情境
  a. 自 20 日高點／一年高點回檔 5～30%
  b. 正／負乖離 200 日均線 10～30%
  c. 爆量 > 20 日均量 2／3 倍（分上漲日、下跌日）
  d. 布林通道寬度落在一年最低 5%／10%（極端盤整收斂）

每個規則統計：10 年內觸發次數（同一波回檔只算一次）、觸發後持有 20／60／120 日的
勝率與平均報酬、相對「任意一天進場」的超額報酬、60 日內平均最大不利波動。
另外算每檔對大盤、對參考股（預設 2330）的波動倍數，給出依波動度校正後的建議門檻。

用法：
  python watch_backtest.py                       觀察名單全部
  python watch_backtest.py 2330 3017 6488 --ref 2330 --years 10
結果：終端機、GitHub 執行摘要頁、watch_backtest_report.md、Telegram 摘要
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

import history
import watch_alert as wa
from notifier import send_telegram

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

HORIZONS = (20, 60, 120)
DD_LEVELS = (5, 10, 15, 20, 25, 30)
DEV_LEVELS = (10, 15, 20, 25, 30)
VOL_MULTS = (2.0, 3.0)
SQUEEZE_PCTS = (5, 10)
MIN_GAP = 10           # 同一規則兩次觸發至少間隔幾個交易日
MIN_EVENTS = 6         # 少於此次數不列入「實證最佳」
RELIABLE_EVENTS = 15   # 少於此次數標「樣本少」
REPORT = Path(os.environ.get("WATCH_BT_REPORT", "watch_backtest_report.md"))


def yahoo_symbol(code: str, markets: Dict[str, str]) -> str:
    return f"{code}.TWO" if markets.get(code) == "OTC" else f"{code}.TW"


def market_markets() -> Dict[str, str]:
    p = history.DATA_DIR / "meta" / "industry.json"
    if p.exists():
        try:
            return {c: v.get("market", "") for c, v in json.loads(p.read_text(encoding="utf-8"))["stocks"].items()}
        except Exception:
            pass
    return {}


def load_yf(symbol: str, years: int) -> pd.DataFrame:
    import yfinance as yf
    d = yf.download(symbol, period=f"{years}y", interval="1d", auto_adjust=True, progress=False, threads=False)
    if d is None or d.empty:
        return pd.DataFrame()
    if isinstance(d.columns, pd.MultiIndex):
        d.columns = d.columns.get_level_values(0)
    d.index = pd.to_datetime(d.index).tz_localize(None)
    return d[["Close", "Volume"]].dropna(subset=["Close"])


# ── 事件 ─────────────────────────────────────────────────────────
def cross_events(signal: pd.Series, trigger, rearm) -> list:
    """條件成立時觸發一次，之後要先「回復」（rearm）才會再計數，避免同一波回檔每天都算"""
    events, armed, last_i = [], True, -10 ** 9
    for i, (ts, x) in enumerate(signal.items()):
        if pd.isna(x):
            continue
        if armed and trigger(x) and i - last_i >= MIN_GAP:
            events.append(ts)
            armed, last_i = False, i
        elif not armed and rearm(x):
            armed = True
    return events


def detect(df: pd.DataFrame) -> Dict[str, list]:
    rules: Dict[str, list] = {}
    for x in DD_LEVELS:
        rules[f"a1 20日高點回檔≥{x}%"] = cross_events(df["dd20"], lambda v, x=x: v <= -x, lambda v, x=x: v >= -x / 2)
        rules[f"a2 一年高點回檔≥{x}%"] = cross_events(df["dd252"], lambda v, x=x: v <= -x, lambda v, x=x: v >= -x / 2)
    for x in DEV_LEVELS:
        rules[f"b1 負乖離200MA≥{x}%"] = cross_events(df["dev200"], lambda v, x=x: v <= -x, lambda v, x=x: v >= -x / 2)
        rules[f"b2 正乖離200MA≥{x}%"] = cross_events(df["dev200"], lambda v, x=x: v >= x, lambda v, x=x: v <= x / 2)
    up = df["ret"] > 0
    for k in VOL_MULTS:
        for side, mask in (("上漲", up), ("下跌", ~up)):
            rules[f"c 爆量≥{k:g}倍({side}日)"] = cross_events(df["vol_mult"].where(mask), lambda v, k=k: v >= k,
                                                         lambda v: True)
    for p in SQUEEZE_PCTS:
        rules[f"d 布林寬度≤{p}分位"] = cross_events(df["bbw_pct"], lambda v, p=p: v <= p, lambda v: v > 30)
    return rules


def summarise(df: pd.DataFrame, rules: Dict[str, list]) -> pd.DataFrame:
    c = df["Close"]
    fwd = pd.DataFrame({h: (c.shift(-h) / c - 1) * 100 for h in HORIZONS})
    fut_min = c[::-1].rolling(60, min_periods=1).min()[::-1].shift(-1)
    mae60 = (fut_min / c - 1) * 100
    base = {h: fwd[h].dropna() for h in HORIZONS}
    rows = [{"規則": "基準：任意一天", "次數": len(df),
             **{f"勝率{h}": (base[h] > 0).mean() * 100 for h in HORIZONS},
             **{f"平均{h}": base[h].mean() for h in HORIZONS},
             "超額60": 0.0, "60日最大不利": mae60.mean(), "樣本": "基準"}]
    for name, ev in rules.items():
        r = {"規則": name, "次數": len(ev)}
        for h in HORIZONS:
            v = fwd.loc[ev, h].dropna() if ev else pd.Series(dtype=float)
            r[f"勝率{h}"] = (v > 0).mean() * 100 if len(v) else np.nan
            r[f"平均{h}"] = v.mean() if len(v) else np.nan
        r["超額60"] = r["平均60"] - base[60].mean() if not np.isnan(r["平均60"]) else np.nan
        r["60日最大不利"] = mae60.loc[ev].mean() if ev else np.nan
        r["樣本"] = "OK" if len(ev) >= RELIABLE_EVENTS else ("樣本少" if len(ev) >= MIN_EVENTS else "不足")
        rows.append(r)
    return pd.DataFrame(rows)


def best_threshold(stats: pd.DataFrame, prefix: str) -> Optional[str]:
    """同一情境中：次數夠、60 日勝率不低於基準、60 日超額報酬最高的門檻"""
    base_win = stats.iloc[0]["勝率60"]
    sub = stats[stats["規則"].str.startswith(prefix) & (stats["次數"] >= MIN_EVENTS) & (stats["勝率60"] >= base_win)]
    if sub.empty:
        return None
    return sub.sort_values("超額60", ascending=False).iloc[0]["規則"].split("≥")[1]


def md_table(df: pd.DataFrame) -> str:
    cols = list(df.columns)
    fmt = lambda v: "—" if isinstance(v, float) and np.isnan(v) else (f"{v:.1f}" if isinstance(v, float) else str(v))
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    lines += ["| " + " | ".join(fmt(v) for v in row) + " |" for row in df.itertuples(index=False)]
    return "\n".join(lines)


def run(codes: List[str], ref: str, years: int, ladder: tuple, loader=load_yf, markets=None) -> dict:
    markets = markets if markets is not None else market_markets()
    sym = {c: yahoo_symbol(c, markets) for c in dict.fromkeys(codes + [ref])}
    idx_of = lambda c: "^TWOII" if sym[c].endswith(".TWO") else "^TWII"
    data = {}
    for key, s in list(sym.items()) + [(i, i) for i in {idx_of(c) for c in codes}]:
        try:
            d = loader(s, years)
        except Exception as e:
            logger.warning("%s 下載失敗：%s", s, e)
            continue
        if len(d) >= 260:
            data[key] = wa.add_indicators(d.assign(Volume=d["Volume"].astype(float)))
        else:
            logger.warning("%s 資料不足（%d 根）", s, len(d))
    vol = {k: float(v["ret"].std() * np.sqrt(252) * 100) for k, v in data.items()}
    ref_vol = vol.get(ref)

    md = [f"# 觀察名單提醒門檻回測（{date.today()}，近 {years} 年日K）",
          "> 建議數值是「提醒門檻」：觸發只代表該去深入檢視現況，**不是買進訊號**。歷史統計不保證未來。",
          f"> 同一波回檔只算一次；觸發少於 {MIN_EVENTS} 次不列入實證最佳，少於 {RELIABLE_EVENTS} 次標「樣本少」。",
          "", "## 波動率倍數", "", "| 股票 | 年化波動 | ÷ 所屬大盤 | ÷ 參考股 " + ref + " |", "|---|---|---|---|"]
    for c in codes:
        if c in data:
            md.append(f"| {c} | {vol[c]:.1f}% | {vol[c] / vol.get(idx_of(c), np.nan):.2f}× | "
                      f"{(vol[c] / ref_vol) if ref_vol else float('nan'):.2f}× |")
    sugg = []
    for c in codes:
        if c not in data:
            continue
        stats = summarise(data[c], detect(data[c]))
        mult = vol[c] / ref_vol if ref_vol else 1.0
        m = float(np.clip(mult, 0.7, 2.5))
        sugg.append({"股票": c, "波動倍數": round(mult, 2), "L1 20日回檔": round(ladder[0] * m, 1),
                     "L2 20日回檔": round(ladder[1] * m, 1), "L3 一年回檔": round(ladder[2] * m, 1),
                     "實證最佳(20日)": best_threshold(stats, "a1") or "無顯著",
                     "實證最佳(一年)": best_threshold(stats, "a2") or "無顯著"})
        d = data[c]
        md += ["", f"## {c}（{d.index[0].date()} ～ {d.index[-1].date()}，{len(d)} 根）", "",
               md_table(stats[["規則", "次數", "勝率20", "平均20", "勝率60", "平均60", "超額60",
                               "勝率120", "平均120", "60日最大不利", "樣本"]])]
    md += ["", "## 建議提醒數值", "",
           f"基準 L1／L2／L3 = {ladder[0]}%／{ladder[1]}%／{ladder[2]}%，乘上「個股波動 ÷ {ref}」（限制 0.7～2.5 倍）。"
           "watch_alert.py 會自動做這個換算，不用逐檔設定。「實證最佳」供交叉比對：60 日超額報酬最高、勝率不低於該股基準的回檔門檻。",
           "", md_table(pd.DataFrame(sugg)) if sugg else "（沒有可回測的股票）",
           "", "**提醒原則**：觸發時只推播「請檢視」並附上當初的核心理由與推翻條件，不是買進訊號。"]
    text = "\n".join(md)
    return {"markdown": text, "suggestions": sugg}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("codes", nargs="*")
    ap.add_argument("--ref", default=None)
    ap.add_argument("--years", type=int, default=int(os.environ.get("WATCH_BT_YEARS", "10")))
    a = ap.parse_args()
    cfg = wa.load_watchlist()
    codes = [c.replace(".TWO", "").replace(".TW", "") for c in (a.codes or [w["code"] for w in cfg["watchlist"]])]
    if not codes:
        logger.error("沒有股票可回測：請在 watchlist.yaml 加入觀察標的，或在執行時填代號")
        return
    lad = cfg["settings"]["ladder"]
    ref = a.ref or str(cfg["settings"]["vol_scale"].get("reference") or "2330")
    res = run(codes, ref, a.years, (lad["l1_dd20"], lad["l2_dd20"], lad["l3_dd252"]))
    print(res["markdown"])
    REPORT.write_text(res["markdown"], encoding="utf-8")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(res["markdown"])
    if res["suggestions"]:
        lines = [f"📐 <b>觀察名單門檻回測（{a.years} 年）</b>", "股票｜波動倍數｜L1／L2／L3｜實證最佳(20日／一年)"]
        lines += [f"• {s['股票']}｜×{s['波動倍數']}｜{s['L1 20日回檔']}／{s['L2 20日回檔']}／{s['L3 一年回檔']}%"
                  f"｜{s['實證最佳(20日)']}／{s['實證最佳(一年)']}" for s in res["suggestions"]]
        lines.append("<i>完整表格在 GitHub Actions 該次執行的摘要頁。提醒只代表該去檢視，不是買進訊號。</i>")
        send_telegram("\n".join(lines))


if __name__ == "__main__":
    main()
